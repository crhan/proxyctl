"""proxyctl trace — 域名链路诊断 (DNS → 规则预测 → 连通性 → 实际连接)"""

import ipaddress
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request


RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[0;33m"
CYAN   = "\033[0;36m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
NC     = "\033[0m"


def _detect_mode() -> dict:
    """
    读取 mihomo 配置文件，返回当前代理模式信息。
    返回: {"tun_enabled": bool, "enhanced_mode": str, "mixed_port": int}
    """
    import os, re
    config_path = os.path.expanduser("~/.config/mihomo/config.yaml")
    result = {"tun_enabled": False, "enhanced_mode": "redir-host", "mixed_port": 7890}
    try:
        text = open(config_path).read()
        m = re.search(r'tun:\s*\n(?:\s*#[^\n]*\n)*\s*enable:\s*(true|false)', text)
        if m:
            result["tun_enabled"] = (m.group(1) == "true")
        m = re.search(r'enhanced-mode:\s*(\S+)', text)
        if m:
            result["enhanced_mode"] = m.group(1)
        m = re.search(r'mixed-port:\s*(\d+)', text)
        if m:
            result["mixed_port"] = int(m.group(1))
    except Exception:
        pass
    return result


def _api_get(api: str, path: str, secret: str) -> dict:
    """通过 Clash API 获取数据（绕过系统代理）。"""
    req = urllib.request.Request(f"{api}{path}")
    req.add_header("Authorization", f"Bearer {secret}")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def _parse_input(raw: str) -> tuple:
    """
    从输入解析 (scheme, domain, port, path)。
    支持: domain / domain:port / scheme://domain:port/path
    """
    scheme = "https"
    port   = None
    path   = "/"

    # ws/wss 与 http/https 同端口同 TLS，统一映射
    _SCHEME_MAP = {"ws": "http", "wss": "https"}
    m = re.match(r'^(https?|wss?)://(.*)', raw)
    if m:
        scheme = _SCHEME_MAP.get(m.group(1), m.group(1))
        raw    = m.group(2)
    if "/" in raw:
        raw, rest = raw.split("/", 1)
        path = "/" + rest
    if ":" in raw:
        raw, port_str = raw.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            pass
    return scheme, raw, port, path


def _is_ip(s: str) -> bool:
    """判断字符串是否为 IP 地址（IPv4 或 IPv6）。"""
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _section_dns(domain: str, api: str, secret: str,
                 fakeip_active: bool = False, corp_dns: dict = None) -> list:
    """
    [1/4] DNS 解析 — 通过 Clash API 查询域名 A 记录。

    Args:
        fakeip_active: 当前是否处于 fake-ip 模式，影响 IP 标签显示。
    返回已解析的 IP 列表。
    """
    print(f"{BOLD}[1/4] DNS 解析{NC}  {domain}")
    resolved_ips = []

    data = _api_get(api, f"/dns/query?name={urllib.parse.quote(domain)}&type=A", secret)
    if data and data.get("Answer"):
        for a in data["Answer"]:
            if a.get("type") == 5:   # CNAME
                print(f"  CNAME → {a['data']}")
        for a in data["Answer"]:
            if a.get("type") == 1:   # A
                ip = a["data"]
                resolved_ips.append(ip)
                # fakeip 标签只在 fake-ip 模式下才有意义，redir-host 返回的是真实 IP
                is_fake = fakeip_active and (ip.startswith("198.18.") or ip.startswith("198.19."))
                tag = f"{RED}fakeip{NC}" if is_fake else f"{GREEN}real{NC}"
                print(f"  A → {ip}  [{tag}]  TTL={a.get('TTL', '?')}")
    elif data and data.get("message"):
        print(f"  {YELLOW}API 超时 (可能是 fakeip 域名)，尝试系统 DNS...{NC}")
        try:
            r = subprocess.run(["nslookup", domain, "127.0.0.1"],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if "Address:" in line and "127.0.0.1" not in line:
                    ip = line.split("Address:")[-1].strip()
                    resolved_ips.append(ip)
                    is_fake = ip.startswith("198.18.") or ip.startswith("198.19.")
                    tag = f"{RED}fakeip{NC}" if is_fake else f"{GREEN}real{NC}"
                    print(f"  A → {ip}  [{tag}]")
        except Exception:
            print(f"  {RED}DNS 查询失败{NC}")
    elif data is not None:
        # Clash API /dns/query 无结果，尝试其他 DNS 解析路径
        fallback_ips = []
        fallback_src = ""

        # 1. 尝试 mihomo DNS listener（仅当 53 端口在监听时）
        import socket as _sock
        dns_listening = False
        try:
            with _sock.create_connection(("127.0.0.1", 53), timeout=0.3):
                dns_listening = True
        except OSError:
            pass

        if dns_listening:
            try:
                r = subprocess.run(
                    ["dig", "@127.0.0.1", "+short", "+timeout=5", domain, "A"],
                    capture_output=True, text=True, timeout=7
                )
                fallback_ips = [l.strip() for l in r.stdout.splitlines()
                                if l.strip() and not l.strip().endswith(".")]
                if fallback_ips:
                    fallback_src = "mihomo DNS"
            except Exception:
                pass

        # 2. mihomo DNS 不可用或无结果，用系统 DNS
        if not fallback_ips:
            try:
                r = subprocess.run(
                    ["dig", "+short", "+timeout=3", domain, "A"],
                    capture_output=True, text=True, timeout=5
                )
                fallback_ips = [l.strip() for l in r.stdout.splitlines()
                                if l.strip() and not l.strip().endswith(".")]
                if fallback_ips:
                    fallback_src = "系统 DNS"
            except Exception:
                pass

        if fallback_ips:
            print(f"  {DIM}(Clash API 无结果，{fallback_src} 解析到:){NC}")
            for ip in fallback_ips:
                resolved_ips.append(ip)
                is_fake = ip.startswith("198.18.") or ip.startswith("198.19.")
                tag = f"{RED}fakeip{NC}" if is_fake else f"{GREEN}real{NC}"
                print(f"  A → {ip}  [{tag}]")
        else:
            print(f"  {YELLOW}无 A 记录{NC}")
            # 探 corp-dns，判断是否需要内网/VPN（仅当配置了企业 DNS 时）
            _corp = corp_dns or {}
            _corp_server = _corp.get("server", "")
            if _corp_server:
                try:
                    r = subprocess.run(
                        ["dig", f"@{_corp_server}", "+short", "+timeout=2", domain],
                        capture_output=True, text=True, timeout=4
                    )
                    corp_ips = [l.strip() for l in r.stdout.splitlines()
                                if l.strip() and not l.strip().endswith(".")]
                    if corp_ips:
                        print(f"  {YELLOW}⚠ 内网域名{NC}：corp-dns 可解析 → "
                              f"{', '.join(corp_ips[:2])}")
                        print(f"  {YELLOW}需连接内网 / VPN 才可访问{NC}")
                        resolved_ips.extend(corp_ips)
                    else:
                        print(f"  corp-dns 也无记录 (域名不存在或 DNS 故障)")
                except Exception:
                    pass
    else:
        print(f"  {RED}DNS 查询失败 (API 不可达){NC}")

    return resolved_ips


def _section_rules(domain: str, resolved_ips: list, api: str, secret: str) -> tuple:
    """
    [2/4] 规则匹配 — 按引擎规则顺序逐条预测。
    返回 (predicted_rule, predicted_proxy)。
    """
    print(f"\n{BOLD}[2/4] 规则匹配{NC}")
    rules_data = _api_get(api, "/rules", secret)
    if not rules_data:
        print(f"  {RED}无法获取规则列表{NC}")
        return None, None

    predicted_rule = predicted_proxy = None
    for rule in rules_data.get("rules", []):
        rtype   = rule.get("type", "")
        payload = rule.get("payload", "")
        proxy   = rule.get("proxy", "")

        matched = False
        detail  = ""

        if rtype == "DomainSuffix":
            if domain == payload or domain.endswith("." + payload):
                matched = True
                detail  = f"域名后缀 {payload}"
        elif rtype == "DomainKeyword":
            if payload in domain:
                matched = True
                detail  = f"域名关键词 {payload}"
        elif rtype == "Domain":
            if domain == payload:
                matched = True
                detail  = f"精确域名 {payload}"
        elif rtype == "IPCIDR" and resolved_ips:
            try:
                net = ipaddress.ip_network(payload, strict=False)
                for ip in resolved_ips:
                    if ipaddress.ip_address(ip) in net:
                        matched = True
                        detail  = f"IP {ip} ∈ {payload}"
                        break
            except ValueError:
                pass
        elif rtype == "Match":
            matched = True
            detail  = "兜底规则"
        # GeoSite / GeoIP 无法客户端精确匹配，跳过

        if matched:
            predicted_rule  = rule
            predicted_proxy = proxy
            idx       = rule.get("index", "?")
            hit_count = rule.get("extra", {}).get("hitCount", 0)
            print(f"  规则 #{idx}: {CYAN}{rtype}({payload}){NC} → {BOLD}{proxy}{NC}")
            print(f"  匹配原因: {detail}")
            print(f"  历史命中: {hit_count} 次")

            # 提示跳过了哪些不确定的 GeoSite/GeoIP 规则
            uncertain = [
                r2 for r2 in rules_data["rules"][:rule.get("index", 0)]
                if r2.get("type") in ("GeoSite", "GeoIP") and r2.get("proxy") != proxy
            ]
            if uncertain:
                types_str = ", ".join(
                    f"{r2['type']}({r2['payload']})→{r2['proxy']}"
                    for r2 in uncertain[-3:]
                )
                print(f"  {DIM}注: 跳过了 {len(uncertain)} 条 GeoSite/GeoIP 规则 "
                      f"(无法客户端匹配): {types_str}{NC}")
            break

    if not predicted_rule:
        print(f"  {YELLOW}未匹配任何规则{NC}")

    return predicted_rule, predicted_proxy


def _section_connectivity(scheme: str, domain: str, port, path: str,
                          mode: dict | None = None) -> tuple:
    """[3/4] 连通性测试 — 走真实路径测试（经系统代理或 TUN）。

    Args:
        mode: _detect_mode() 的返回值，用于标注流量实际走向。
    返回 (lines, remote_ip)，由调用方负责打印，支持后台并发执行。
    """
    if port:
        test_url = f"{scheme}://{domain}:{port}{path}"
    else:
        test_url = f"{scheme}://{domain}{path}"
    lines = [f"\n{BOLD}[3/4] 连通性测试{NC}  {test_url}"]

    cmd = ["curl", "-sS", "-o", "/dev/null", "-w",
           "%{http_code} %{time_connect} %{time_total} %{remote_ip}",
           "--max-time", "8", "--connect-timeout", "5"]
    if scheme == "https":
        cmd.append("-k")   # 内网自签证书常见
    cmd.append(test_url)

    remote_ip = ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        parts = r.stdout.strip().split()
        if len(parts) >= 4:
            http_code, t_conn, t_total, remote_ip = parts
            cc = GREEN if http_code[0] in "23" else RED

            # remote_ip == 127.0.0.1 说明 curl 连到了本地代理/TUN，不是目标服务器
            # 根据当前模式标注实际走向，避免误导
            if remote_ip == "127.0.0.1" and mode:
                if mode["tun_enabled"]:
                    via = f"{DIM}via TUN{NC}"
                else:
                    via = f"{DIM}via HTTP 代理 :{mode['mixed_port']}{NC}"
                lines.append(f"  HTTP {cc}{http_code}{NC}  "
                             f"连接 {float(t_conn)*1000:.0f}ms  "
                             f"总计 {float(t_total)*1000:.0f}ms  "
                             f"[{via}]")
            else:
                lines.append(f"  HTTP {cc}{http_code}{NC}  "
                             f"连接 {float(t_conn)*1000:.0f}ms  "
                             f"总计 {float(t_total)*1000:.0f}ms  "
                             f"目标IP {remote_ip}")
        elif r.returncode != 0:
            lines.append(f"  {RED}✗ 连接失败{NC} (exit {r.returncode})")
            for line in r.stderr.strip().splitlines():
                if line.strip():
                    lines.append(f"  {RED}{line.strip()}{NC}")
        else:
            lines.append(f"  {YELLOW}? 无输出 (curl exit {r.returncode}){NC}")
    except subprocess.TimeoutExpired:
        lines.append(f"  {RED}✗ 超时 (>8s){NC}")
    except Exception as e:
        lines.append(f"  {RED}✗ {e}{NC}")

    return lines, remote_ip


def _grep_log_connections(domain: str, max_entries: int = 5) -> list:
    """从 mihomo/sing-box 日志中 grep 域名的最近连接记录。

    解析格式：[TCP] src --> domain:port match Rule(payload) using group[node]

    Args:
        domain: 目标域名
        max_entries: 最多返回几条（去重后）

    Returns:
        [{"time": "07:42:13", "proto": "TCP", "rule": "DomainSuffix(github.com)",
          "chain": "proxy[日本4(IP)(直连)]"}, ...]
    """
    import os as _os
    home = _os.path.expanduser("~")
    log_candidates = [
        f"{home}/.config/mihomo/mihomo.log",
        f"{home}/.config/sing-box/sing-box.log",
    ]
    # 尝试从日志文件 grep
    lines = []
    for f in log_candidates:
        if not _os.path.isfile(f):
            continue
        try:
            r = subprocess.run(
                ["grep", "--text", "--", f"--> {domain}:", f],
                capture_output=True, text=True, timeout=5
            )
            if r.stdout.strip():
                lines = r.stdout.strip().splitlines()
                break
        except Exception:
            pass

    # 日志文件没结果（可能被 systemd journal 截走），尝试 journalctl
    if not lines:
        import platform as _plat
        if _plat.system() == "Linux":
            try:
                r = subprocess.run(
                    ["journalctl", "--user", "--no-pager", "-u", "mihomo.service",
                     "-u", "sing-box.service", "--since", "24 hours ago",
                     "--grep", f"-- {domain}:"],
                    capture_output=True, text=True, timeout=5
                )
                if r.stdout.strip():
                    lines = r.stdout.strip().splitlines()
            except Exception:
                pass

    if not lines:
        return []

    if not lines:
        return []

    # 解析并去重（按 rule+chain 去重，保留最近的）
    results = []
    seen = set()
    for line in reversed(lines):
        # time="2026-04-25T07:42:13..." ... [TCP] ... match Rule using chain
        t_match = re.search(r'T(\d{2}:\d{2}:\d{2})', line)
        p_match = re.search(r'\[(TCP|UDP)\]', line)
        r_match = re.search(r'match\s+(\S+)', line)
        c_match = re.search(r'using\s+(.+?)(?:\s*$|")', line)

        if r_match and c_match:
            entry_time = t_match.group(1) if t_match else "?"
            proto = p_match.group(1) if p_match else "?"
            rule = r_match.group(1)
            chain = c_match.group(1)
            key = f"{rule}|{chain}"
            if key not in seen:
                seen.add(key)
                results.append({
                    "time": entry_time, "proto": proto,
                    "rule": rule, "chain": chain,
                })
            if len(results) >= max_entries:
                break

    results.reverse()  # 时间正序
    return results


def _section_connections(domain: str, resolved_ips: list,
                         predicted_proxy: str, api: str, secret: str):
    """[4/4] 实际连接验证 — 从 Clash API 活跃连接 + 日志历史中取路由信息。"""
    print(f"\n{BOLD}[4/4] 实际连接{NC}")

    domain_conns = []
    for _ in range(3):
        data = _api_get(api, "/connections", secret)
        if data and isinstance(data, dict):
            for c in (data.get("connections") or []):
                m  = c.get("metadata", {})
                h  = m.get("host", "")
                di = m.get("destinationIP", "")
                if (h == domain or h.endswith("." + domain)
                        or (di and di in resolved_ips)):
                    domain_conns.append(c)
        if domain_conns:
            break
        time.sleep(0.3)

    if not domain_conns:
        # 没有活跃连接，从引擎日志中捞历史记录
        log_conns = _grep_log_connections(domain)
        if log_conns:
            print(f"  {DIM}(无活跃连接，以下为日志中最近的记录){NC}")
            for entry in log_conns:
                print(f"  {entry['time']}  {entry['proto']} → "
                      f"{CYAN}{entry['rule']}{NC} using "
                      f"{GREEN}{entry['chain']}{NC}")
        else:
            print(f"  {DIM}无活跃连接，日志中也无记录{NC}")
            if predicted_proxy:
                print(f"  {DIM}基于规则预测，该域名应走: {BOLD}{predicted_proxy}{NC}")
        return

    # 去重：按 rule+chains
    seen: set = set()
    deduped = []
    for c in domain_conns:
        rule   = c.get("rule", "?")
        rp     = c.get("rulePayload", "")
        chains = c.get("chains", [])
        key    = f"{rule}|{rp}|{','.join(chains)}"
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    for c in deduped:
        rule       = c.get("rule", "?")
        rp         = c.get("rulePayload", "")
        chains     = c.get("chains", [])
        chain_str  = " → ".join(reversed(chains)) if chains else "?"
        rp_str     = f"({rp})" if rp else ""
        print(f"  规则: {CYAN}{rule}{rp_str}{NC}")
        print(f"  链路: {chain_str}")

    # 与预测对比
    if predicted_proxy:
        actual_chains  = domain_conns[0].get("chains", [])
        actual_outbound = actual_chains[-1] if actual_chains else "?"
        if actual_outbound.lower() != predicted_proxy.lower():
            print(f"  {YELLOW}⚠ 预测出口 {predicted_proxy}，实际出口 {actual_outbound}{NC}")
        else:
            print(f"  {GREEN}✓ 与规则预测一致{NC}")

    # 连接详情
    print(f"  {DIM}---{NC}")
    domain_conns.sort(key=lambda x: x.get("start", ""), reverse=True)
    for c in domain_conns[:5]:
        m      = c.get("metadata", {})
        host   = m.get("host", "?")
        dport  = m.get("destinationPort", "?")
        up     = c.get("upload", 0)
        down   = c.get("download", 0)
        chains = c.get("chains", [])
        outbound = chains[0] if chains else "?"

        def fmt_bytes(b: int) -> str:
            if b < 1024:         return f"{b}B"
            if b < 1024 * 1024:  return f"{b/1024:.1f}K"
            return f"{b/1024/1024:.1f}M"

        print(f"  {host}:{dport}  {outbound}  ↑{fmt_bytes(up)} ↓{fmt_bytes(down)}")


def cmd_trace(raw_input: str, api: str, secret: str, config: dict = None):
    """proxyctl trace — 诊断域名的完整访问链路。

    Args:
        raw_input: 用户输入的域名或 URL
        api: Clash API 基础 URL
        secret: Clash API Bearer token
        config: 全局配置字典（可选，用于 corp_dns 探测）
    """
    import threading
    scheme, domain, port, path = _parse_input(raw_input)

    # 读取当前代理模式，影响 DNS 标签和连通性测试的输出注释
    mode = _detect_mode()
    fakeip_active = mode["enhanced_mode"] == "fake-ip"
    tun_label = f"{GREEN}TUN on{NC}" if mode["tun_enabled"] else f"{DIM}TUN off{NC}"
    dns_label  = f"{CYAN}{mode['enhanced_mode']}{NC}"
    print(f"{DIM}模式: {tun_label}  DNS: {dns_label}  代理端口: {mode['mixed_port']}{NC}\n")

    # [3/4] 连通性与 [1/4][2/4] 完全无依赖，提前并发发出
    connectivity_result = [None]

    def _run_connectivity():
        connectivity_result[0] = _section_connectivity(scheme, domain, port, path, mode)

    t = threading.Thread(target=_run_connectivity)
    t.start()

    # 输入直接是 IP 地址时跳过 DNS，直接进规则匹配
    if _is_ip(domain):
        print(f"{BOLD}[1/4] DNS 解析{NC}  {domain}  {DIM}(IP 地址，跳过){NC}")
        resolved_ips = [domain]
    else:
        corp_dns = (config or {}).get("corp_dns", {}) or {}
        resolved_ips = _section_dns(domain, api, secret, fakeip_active, corp_dns)
    predicted_rule, predicted_proxy = _section_rules(domain, resolved_ips, api, secret)

    # 等连通性测试结束（此时大概率已完成），按顺序打印
    t.join()
    lines, _ = connectivity_result[0]
    for line in lines:
        print(line)

    _section_connections(domain, resolved_ips, predicted_proxy, api, secret)
