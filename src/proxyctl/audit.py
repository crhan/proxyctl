"""proxyctl audit — 审计走代理但可能应直连的域名

通过扫描代理日志，找出那些走了代理但实际是国内 IP 的域名，
建议添加到直连规则，优化分流效果。
"""

import json
import os
import re
import socket
import subprocess
from collections import defaultdict
from typing import Optional


RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
BOLD = "\033[1m"
NC = "\033[0m"

HOME = os.path.expanduser("~")
DEFAULT_CONFIG_DIR = os.path.join(HOME, ".config", "proxyctl")

# 日志和配置路径（可通过配置覆盖）
SB_LOG = os.path.join(HOME, ".config", "sing-box", "sing-box.err")
MH_LOG = os.path.join(HOME, ".config", "mihomo", "mihomo.log")
SB_CONFIG = os.path.join(HOME, ".config", "sing-box", "config.json")
MH_CONFIG = os.path.join(HOME, ".config", "mihomo", "config.yaml")

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

# sing-box 日志：outbound/tuic[节点]: outbound connection to domain:port
SB_PROXY_RE = re.compile(
    r'outbound/(?:tuic|shadowsocks|hysteria2?)\[[^\]]+\]: outbound connection to ([^:]+):\d+'
)
# mihomo 日志：[TCP] ... --> domain:port match Rule using GROUP[NODE]
MH_PROXY_OK_RE = re.compile(
    r'\[TCP\].*?-->\s+([^:]+):\d+\s+match\s+\S+.*?using\s+(?!DIRECT|REJECT)(\S+)'
)
MH_PROXY_ERR_RE = re.compile(
    r'\[TCP\]\s+dial\s+proxy.*?-->\s+([^:]+):\d+\s+error:'
)

SKIP_HOSTS = {'www.gstatic.com', 'cp.cloudflare.com'}

# 已知需要代理的海外服务关键词
KNOWN_PROXY_KW = [
    'google', 'github', 'discord', 'telegram', 'twitter',
    'youtube', 'reddit', 'openai', 'anthropic', 'cloudflare',
    'apple.com', 'icloud', 'amazonaws', 'microsoft', 'azure',
    'intercom', 'datadog', 'datadoghq', 'sentry',
    'adblockplus', '1password', 'notion', 'docker', 'npmjs',
    'pypi', 'crates.io', 'huggingface', 'wikipedia', 'medium',
    'stackoverflow', 'x.com', 'twitch', 'netflix', 'spotify',
    'whatsapp', 'signal', 'mozilla', 'firefox', 'brave',
    'grammarly', 'linear.app', 'figma', 'vercel', 'netlify',
    'heroku', 'digitalocean', 'linode', 'vultr', 'hetzner',
    'shields.io', 'gravatar',
]

IPGEO_CACHE_FILE = os.path.join(DEFAULT_CONFIG_DIR, ".ipgeo-audit-cache")


def _is_valid_domain(host: str) -> bool:
    """过滤纯 IP、无点假域名、非域名格式。"""
    try:
        socket.inet_aton(host)
        return False
    except socket.error:
        pass
    if "." not in host:
        return False
    if not any(c.isalpha() for c in host.split(".")[-1]):
        return False
    return host not in SKIP_HOSTS


def _scan_log(log_path: str, engine_type: str, audit_days: int) -> dict:
    """扫描日志文件，返回 {domain: count}。"""
    domains: dict = defaultdict(int)
    if not os.path.exists(log_path):
        return domains

    log_size = os.path.getsize(log_path)
    read_bytes = max(10 * 1024 * 1024, min(log_size, audit_days * 50 * 1024 * 1024))

    with open(log_path, "rb") as f:
        f.seek(max(0, log_size - read_bytes))
        if f.tell() > 0:
            f.readline()  # 跳过可能被截断的不完整行
        for raw in f:
            try:
                line = ANSI_RE.sub("", raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
            host = None
            if engine_type == "singbox":
                m = SB_PROXY_RE.search(line)
                if m:
                    host = m.group(1)
            else:
                m = MH_PROXY_OK_RE.search(line) or MH_PROXY_ERR_RE.search(line)
                if m:
                    host = m.group(1)
            if host and _is_valid_domain(host):
                domains[host] += 1
    return domains


def _load_geo_cache() -> dict:
    """加载 IP → country 缓存。"""
    try:
        with open(IPGEO_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_geo_cache(cache: dict):
    try:
        with open(IPGEO_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


_geo_cache: dict = {}


def _ip_country(ip: str) -> str:
    """查询 IP 所属国家代码，带本地文件缓存。"""
    global _geo_cache
    if not _geo_cache:
        _geo_cache = _load_geo_cache()
    if ip in _geo_cache:
        return _geo_cache[ip]
    try:
        r = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "--max-time", "3",
             f"https://ipinfo.io/{ip}/country"],
            capture_output=True, text=True, timeout=5
        )
        country = r.stdout.strip().upper()
        if len(country) == 2 and country.isalpha():
            _geo_cache[ip] = country
            return country
    except Exception:
        pass
    return ""


def _is_cn_ip(ip: str) -> bool:
    return _ip_country(ip) == "CN"


def _resolve_direct(domain: str) -> str:
    """用阿里 DoH 反查域名的真实 IP。"""
    try:
        r = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "--max-time", "3",
             f"https://223.5.5.5/resolve?name={domain}&type=A"],
            capture_output=True, text=True, timeout=5
        )
        data = json.loads(r.stdout)
        for a in data.get("Answer", []):
            if a.get("type") == 1:
                return a["data"]
    except Exception:
        pass
    return ""


def _is_covered(host: str, suffix_set: set) -> bool:
    """判断域名是否已被规则集覆盖。"""
    parts = host.split(".")
    return any(".".join(parts[i:]) in suffix_set for i in range(len(parts)))


def _load_rules() -> tuple:
    """从双 config 读取 direct/proxy 域名后缀规则集。"""
    direct_suffixes: set = set()
    proxy_suffixes: set = set()

    # sing-box JSON
    try:
        cfg = json.load(open(SB_CONFIG))
        for rule in cfg.get("route", {}).get("rules", []):
            ob = rule.get("outbound", "")
            for s in rule.get("domain_suffix", []):
                s = s.lstrip(".")
                if ob == "direct":
                    direct_suffixes.add(s)
                elif ob in ("proxy", "claude"):
                    proxy_suffixes.add(s)
    except Exception:
        pass

    # mihomo YAML (文本扫描，无需解析完整 YAML)
    try:
        for line in open(MH_CONFIG):
            m = re.match(r'\s*-\s+DOMAIN-SUFFIX,([^,]+),(DIRECT|proxy|claude)', line, re.I)
            if m:
                dom, target = m.group(1), m.group(2)
                if target.upper() == "DIRECT":
                    direct_suffixes.add(dom)
                else:
                    proxy_suffixes.add(dom)
    except Exception:
        pass

    return direct_suffixes, proxy_suffixes


def _apply_to_configs(new_suffixes: list) -> list:
    """将建议直连的后缀写入双 config，返回操作摘要列表。"""
    applied = []

    # 1. sing-box config.json
    try:
        cfg = json.load(open(SB_CONFIG))
        target_rule = None
        for rule in cfg.get("route", {}).get("rules", []):
            if rule.get("outbound") == "direct" and "domain_suffix" in rule:
                target_rule = rule
        if target_rule:
            existing = {s.lstrip(".") for s in target_rule["domain_suffix"]}
            added = [s for s in new_suffixes if s not in existing]
            if added:
                target_rule["domain_suffix"].extend(added)
                with open(SB_CONFIG, "w") as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                applied.append(f"sing-box: +{len(added)} 条")
    except Exception as e:
        applied.append(f"sing-box: 失败 ({e})")

    # 2. mihomo config.yaml: 在 ".cn 后缀" 注释行前插入
    try:
        lines = open(MH_CONFIG).readlines()
        insert_idx = None
        existing_mh: set = set()
        for i, line in enumerate(lines):
            m = re.match(r'\s*-\s+DOMAIN-SUFFIX,([^,]+),', line)
            if m:
                existing_mh.add(m.group(1))
            if ".cn 后缀" in line or "DOMAIN-SUFFIX,cn,DIRECT" in line:
                insert_idx = i
        if insert_idx is not None:
            added_mh = []
            for s in new_suffixes:
                if s not in existing_mh:
                    lines.insert(insert_idx, f"  - DOMAIN-SUFFIX,{s},DIRECT\n")
                    insert_idx += 1
                    added_mh.append(s)
            if added_mh:
                open(MH_CONFIG, "w").writelines(lines)
                applied.append(f"mihomo: +{len(added_mh)} 条")
    except Exception as e:
        applied.append(f"mihomo: 失败 ({e})")

    return applied


def cmd_audit(audit_days: int, api_base: str, api_secret: str, do_apply: bool):
    """proxyctl audit — 扫描日志，找走代理但实际是国内 IP 的域名。"""
    print(f"{BOLD}代理链路审计{NC} (最近 {audit_days} 天，双引擎扫描)\n")

    # 步骤 1: 扫描双引擎日志
    proxy_domains: dict = defaultdict(int)
    scanned = []
    for log_path, etype, label in [
        (SB_LOG, "singbox", "sing-box"),
        (MH_LOG, "mihomo", "mihomo"),
    ]:
        if os.path.exists(log_path):
            d = _scan_log(log_path, etype, audit_days)
            for host, count in d.items():
                proxy_domains[host] += count
            sz = os.path.getsize(log_path)
            scanned.append(f"{label}({sz // 1024 // 1024}MB, {len(d)} 域名)")
    print(f"日志扫描：{', '.join(scanned) or '无日志文件'}")

    # 步骤 2: 合并当前活跃连接
    try:
        r = subprocess.run(
            ["curl", "-s", "--noproxy", "*",
             "-H", f"Authorization: Bearer {api_secret}",
             f"{api_base}/connections"],
            capture_output=True, text=True, timeout=5
        )
        data = json.loads(r.stdout)
        for c in data.get("connections", []):
            chain_str = str(c.get("chains", []))
            host = c.get("metadata", {}).get("host", "")
            if host and _is_valid_domain(host):
                if any(kw in chain_str for kw in
                       ["tuic", "shadowsocks", "auto", "proxy"]):
                    proxy_domains[host] += 1
    except Exception:
        pass

    if not proxy_domains:
        print("  没有发现走代理的域名流量。")
        return

    # 步骤 3: 读双 config 规则，过滤已有明确规则的域名
    direct_suffixes, proxy_suffixes = _load_rules()
    uncovered = {
        h: c for h, c in proxy_domains.items()
        if not _is_covered(h, direct_suffixes) and not _is_covered(h, proxy_suffixes)
    }

    if not uncovered:
        print("  所有走代理的域名都已被显式规则覆盖，无遗漏。")
        return

    # 步骤 4 & 5: 快速分类 + DoH 反查
    print(f"未覆盖域名：{len(uncovered)} 个，DoH 反查中...\n")
    candidates = []   # (host, count, ip, tag) — 疑似应直连
    proxy_ok = []     # 确认需要代理
    unknown = []      # 无法判断

    for host, count in sorted(uncovered.items(), key=lambda x: -x[1]):
        if any(kw in host for kw in KNOWN_PROXY_KW):
            proxy_ok.append((host, count, "", "known"))
            continue
        real_ip = _resolve_direct(host)
        if not real_ip:
            unknown.append((host, count, "", "no-A"))
            continue
        country = _ip_country(real_ip)
        if country == "CN":
            candidates.append((host, count, real_ip, "cn"))
        else:
            proxy_ok.append((host, count, real_ip, country or "?"))

    # 输出
    if candidates:
        print(f"{RED}■ 疑似应直连（国内 IP）:{NC}")
        for host, count, ip, _ in candidates:
            print(f"  {host:<45s} → {ip:<16s} x{count}")

        seen: set = set()
        for host, _, _, _ in candidates:
            parts = host.split(".")
            if len(parts) >= 2:
                seen.add(".".join(parts[-2:]))
        new_suffixes = sorted(seen)
        print(f"\n  建议添加到 direct 规则:")
        for s in new_suffixes:
            print(f"    .{s}")
        print()

        if do_apply and new_suffixes:
            applied = _apply_to_configs(new_suffixes)
            if applied:
                print(f"{GREEN}■ 已写入：{', '.join(applied)}{NC}")
                print("执行 proxyctl restart 生效\n")

    if unknown:
        print(f"{YELLOW}■ 无法判断 ({len(unknown)} 个):{NC}")
        for host, count, _, reason in unknown:
            print(f"  {host:<45s} x{count:5d}  ({reason})")
        print()

    if proxy_ok:
        show = proxy_ok[:20]
        more = len(proxy_ok) - len(show)
        print(f"{GREEN}■ 确认需要代理 ({len(proxy_ok)} 个):{NC}")
        for host, count, ip, _ in show:
            print(f"  {host:<45s} {ip:<16s} x{count:5d}")
        if more:
            print(f"  ... 另有 {more} 个")
        print()

    _save_geo_cache(_geo_cache)

    total = len(candidates) + len(proxy_ok) + len(unknown)
    print(f"共 {total} 个未覆盖域名："
          f"{RED}{len(candidates)} 疑似可直连{NC}, "
          f"{len(proxy_ok)} 确认代理，"
          f"{len(unknown)} 待定")
    if candidates and not do_apply:
        print(f"\n执行 {BOLD}proxyctl audit apply{NC} 自动写入双 config")