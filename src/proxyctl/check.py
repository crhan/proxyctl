"""proxyctl check — 全面健康检查 (4 个阶段并发执行)"""

import json
import os
import platform
import socket
import subprocess
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

IS_MACOS = platform.system() == "Darwin"


RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[0;33m"
CYAN   = "\033[0;36m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
NC     = "\033[0m"

HOME = os.path.expanduser("~")


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _test_url(url: str, desc: str, mode: str = "proxy", timeout: int = 8) -> tuple:
    """
    测试 URL 可达性。
    mode=proxy: 走 socks5h://127.0.0.1:7890
    mode=direct: 绕过所有代理 (--noproxy '*')
    返回 (ok: bool, line: str)，调用方负责 print。
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("http_proxy", "https_proxy", "all_proxy",
                        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
           "--max-time", str(timeout)]
    if mode == "proxy":
        cmd += ["--proxy", "socks5h://127.0.0.1:7890"]
    else:
        cmd += ["--noproxy", "*"]
    cmd.append(url)

    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    code = r.stdout.strip()

    if code == "000" or not code:
        return False, f"  {RED}✗{NC} {desc:<18s} {url:<44s} {RED}timeout{NC}"
    elif code.startswith(("2", "3", "4")):
        return True,  f"  {GREEN}✓{NC} {desc:<18s} {url:<44s} {GREEN}{code}{NC}"
    elif code.startswith("5"):
        # 5xx = 链路通了，服务端报错，不算代理故障
        return True,  f"  {YELLOW}✓{NC} {desc:<18s} {url:<44s} {YELLOW}{code} (server error){NC}"
    else:
        return False, f"  {YELLOW}?{NC} {desc:<18s} {url:<44s} {YELLOW}{code}{NC}"


def _test_corp_dns(desc: str, corp_server: str, test_domain: str) -> tuple:
    """测试企业 DNS 可达性。

    Args:
        desc: 测试项描述文本
        corp_server: 企业 DNS 服务器 IP
        test_domain: 用于测试的企业内部域名

    Returns:
        (ok: bool, line: str)，调用方负责 print。
    """
    r = subprocess.run(
        ["dig", f"@{corp_server}", "+short", "+timeout=3", test_domain],
        capture_output=True, text=True, timeout=5
    )
    ok = r.returncode == 0 and r.stdout.strip()
    if ok:
        return True,  f"  {GREEN}✓{NC} {desc:<18s} {corp_server:<44s} {GREEN}ok{NC}"
    else:
        return False, f"  {RED}✗{NC} {desc:<18s} {corp_server:<44s} {RED}timeout{NC}"


def _test_tcp(host: str, port: int, desc: str) -> tuple:
    """返回 (ok: bool, line: str)，调用方负责 print。"""
    addr = f"{host}:{port}"
    try:
        with socket.create_connection((host, port), timeout=3):
            return True,  f"  {GREEN}✓{NC} {desc:<18s} {addr:<44s} {GREEN}ok{NC}"
    except OSError:
        return False, f"  {RED}✗{NC} {desc:<18s} {addr:<44s} {RED}unreachable{NC}"


def _proxy_groups_section(api_base: str, api_secret: str) -> bool:
    """检查并自动修复代理组，打印节点明细。返回是否全部正常。"""
    r = subprocess.run(
        ["curl", "-s", "--noproxy", "*",
         "-H", f"Authorization: Bearer {api_secret}",
         f"{api_base}/proxies"],
        capture_output=True, text=True, timeout=5
    )
    if not r.stdout.strip():
        print(f"  {YELLOW}—{NC} Clash API 不可达")
        return False

    try:
        data = json.loads(r.stdout)
    except Exception:
        print(f"  {YELLOW}—{NC} API 响应解析失败")
        return False

    proxies = data.get("proxies", {})

    from datetime import datetime, timezone

    def get_delay(name: str, src: dict = None) -> int:
        """取节点延迟；若 name 是组则穿透到它的 now 节点。"""
        p = (src or proxies).get(name, {})
        # 如果是组（有 all 字段），穿透到 now 节点
        if p.get("all") and p.get("now"):
            p = proxies.get(p["now"], {})
        h = p.get("history", [])
        return h[-1].get("delay", 0) if h else -1

    def ds(d: int) -> str:
        if d < 0: return f"{YELLOW}—{NC}"
        if d == 0: return f"{RED}✗{NC}"
        if d < 200: return f"{GREEN}{d}{NC}"
        if d < 500: return f"{YELLOW}{d}{NC}"
        return f"{RED}{d}{NC}"

    def group_tested_ago(members: list) -> str:
        """返回组内最近一次测试时间，格式如 '3m ago'；无数据返回空字符串。"""
        latest = None
        for m in members:
            h = proxies.get(m, {}).get("history", [])
            if not h:
                continue
            t_str = h[-1].get("time", "")
            if not t_str:
                continue
            try:
                t = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                if latest is None or t > latest:
                    latest = t
            except ValueError:
                pass
        if latest is None:
            return ""
        diff = int((datetime.now(timezone.utc) - latest).total_seconds())
        if diff < 60:
            return f"{diff}s ago"
        if diff < 3600:
            return f"{diff // 60}m ago"
        return f"{diff // 3600}h ago"

    # 输出所有组
    def dw(s: str) -> int:
        """计算终端显示宽度（中文双宽）。"""
        return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)

    def print_members(members: list, now: str):
        """打印组成员列表（4 列表格）。"""
        if not members:
            return
        max_w = max(dw(m) for m in members)
        col_w = max_w + 8
        line = "   "
        col = 0
        for m in members:
            d = get_delay(m)
            marker = "→" if m == now else " "
            raw_d = str(d) if d > 0 else ("✗" if d == 0 else "—")
            raw = f"{marker}{m}:{raw_d}"
            pad = max(1, col_w - dw(raw))
            line += f"{marker}{m}:{ds(d)}{' ' * pad}"
            col += 1
            if col >= 4:
                print(line.rstrip())
                line = "   "
                col = 0
        if col > 0:
            print(line.rstrip())

    def print_group(gname: str):
        """打印单个组的摘要 + 成员；若 selector 成员也是组则递归展开。"""
        g = proxies.get(gname)
        if not g:
            return
        _type_map = {"URLTest": "url", "Selector": "sel", "Fallback": "fb", "LoadBalance": "lb"}
        gtype = _type_map.get(g.get("type"), g.get("type", "?")[:3].lower())
        gnow  = g.get("now", "?")
        gnow_d = get_delay(gnow)
        gmembers = g.get("all", [])

        # selector/fallback 成员如果是子组，用子组内部的节点做 alive/dead 统计
        if g.get("type") in ("Selector", "Fallback"):
            alive = dead = nodata = total = 0
            for m in gmembers:
                sub = proxies.get(m, {})
                if sub.get("all"):
                    # 子组：统计它里面的叶子节点
                    for leaf in sub["all"]:
                        d = get_delay(leaf)
                        total += 1
                        if d > 0: alive += 1
                        elif d == 0: dead += 1
                        else: nodata += 1
                else:
                    # 普通节点
                    d = get_delay(m)
                    total += 1
                    if d > 0: alive += 1
                    elif d == 0: dead += 1
                    else: nodata += 1
        else:
            alive  = sum(1 for m in gmembers if get_delay(m) > 0)
            dead   = sum(1 for m in gmembers if get_delay(m) == 0)
            nodata = sum(1 for m in gmembers if get_delay(m) < 0)
            total  = len(gmembers)
        counts = []
        if alive:  counts.append(f"{GREEN}{alive}✓{NC}")
        if dead:   counts.append(f"{RED}{dead}✗{NC}")
        if nodata: counts.append(f"{YELLOW}{nodata}—{NC}")
        count_str = "/".join(counts)
        tested = group_tested_ago(gmembers)
        tested_str = f"  {DIM}{tested}{NC}" if tested else ""
        print(f"  {CYAN}{gname}{NC}({gtype}) → {BOLD}{gnow}{NC} "
              f"{ds(gnow_d)}ms  [{count_str} of {total}]{tested_str}")

        # selector/fallback 的成员如果也是组，展开子组详情
        if g.get("type") in ("Selector", "Fallback"):
            print_members(gmembers, gnow)
            for m in gmembers:
                sub = proxies.get(m)
                if sub and sub.get("all"):
                    sub_type = "url" if sub.get("type") == "URLTest" else "sel"
                    sub_now = sub.get("now", "?")
                    sub_now_d = get_delay(sub_now)
                    sub_members = sub.get("all", [])
                    s_alive = sum(1 for x in sub_members if get_delay(x) > 0)
                    s_dead  = sum(1 for x in sub_members if get_delay(x) == 0)
                    s_nodata = sum(1 for x in sub_members if get_delay(x) < 0)
                    sc = []
                    if s_alive:  sc.append(f"{GREEN}{s_alive}✓{NC}")
                    if s_dead:   sc.append(f"{RED}{s_dead}✗{NC}")
                    if s_nodata: sc.append(f"{YELLOW}{s_nodata}—{NC}")
                    st = group_tested_ago(sub_members)
                    st_str = f"  {DIM}{st}{NC}" if st else ""
                    active = " ←" if m == gnow else ""
                    print(f"    {CYAN}{m}{NC}({sub_type}) → {BOLD}{sub_now}{NC} "
                          f"{ds(sub_now_d)}ms  [{'/'.join(sc)} of {len(sub_members)}]{st_str}{active}")
                    print_members(sub_members, sub_now)
        else:
            print_members(gmembers, gnow)

    # 跟踪哪些组已经作为 selector 子组展开过，避免重复
    shown = set()
    for gname in ["proxy", "claude", "residential-us", "residential-sg"]:
        if gname in shown:
            continue
        g = proxies.get(gname)
        if not g:
            continue
        print_group(gname)
        # selector/fallback 展开的子组标记为已显示
        if g.get("type") in ("Selector", "Fallback"):
            for m in g.get("all", []):
                if proxies.get(m, {}).get("all"):
                    shown.add(m)


    return True


def _ipgeo(ip: str, cache_file: str, api_secret: str) -> str:
    """查询 IP 归属地，带文件缓存。返回 'city,country|org' 格式。"""
    if not ip:
        return ""
    if os.path.isfile(cache_file):
        for line in open(cache_file):
            if line.startswith(f"{ip}|"):
                return line.split("|", 1)[1].strip()
    env = {k: v for k, v in os.environ.items()
           if k not in ("http_proxy", "https_proxy", "all_proxy",
                        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    r = subprocess.run(
        ["curl", "-s", "--max-time", "6", "--proxy", "socks5h://127.0.0.1:7890",
         f"https://ipinfo.io/{ip}/json"],
        capture_output=True, text=True, env=env, timeout=10
    )
    if not r.stdout:
        return ""
    try:
        d = json.loads(r.stdout)
        city    = d.get("city", "")
        country = d.get("country", "")
        org     = d.get("org", "")
        if org:
            parts = org.split(" ", 1)
            org = parts[1] if len(parts) > 1 else org
        loc = ",".join(filter(None, [city, country]))
        result = f"{loc}|{org}"
        with open(cache_file, "a") as f:
            f.write(f"{ip}|{result}\n")
        return result
    except Exception:
        return ""


def _fmt_ip(ip: str, geo: str) -> str:
    if not ip:
        return f"{YELLOW}{'failed':<15s}{NC}"
    loc = geo.split("|")[0] if geo else ""
    org = geo.split("|")[1] if geo and "|" in geo else ""
    detail = ", ".join(filter(None, [loc, org]))
    if detail:
        return f"{ip:<15s} {CYAN}{detail}{NC}"
    return f"{ip:<15s}"


def cmd_bench(api: str, api_secret: str, groups: list = None):
    """
    sb bench [group...] — 对指定代理组的全部节点发起测速，默认测所有组。

    参数:
        api        -- Clash API base URL (e.g. http://127.0.0.1:9090)
        api_secret -- Clash API Bearer token
        groups     -- 要测速的组名列表；None 表示测全部默认组
    """
    DEFAULT_GROUPS = ["proxy", "claude", "residential-us", "residential-sg"]
    TEST_URL       = "https://www.gstatic.com/generate_204"
    TIMEOUT_MS     = 5000   # 每个节点的测速超时（毫秒）
    MAX_WORKERS    = 16     # 并发测速线程数

    target_groups = groups if groups else DEFAULT_GROUPS

    # ── 拉取代理列表 ─────────────────────────────────────────────────────────
    r = subprocess.run(
        ["curl", "-s", "--noproxy", "*",
         "-H", f"Authorization: Bearer {api_secret}",
         f"{api}/proxies"],
        capture_output=True, text=True, timeout=5
    )
    if not r.stdout.strip():
        print(f"  {YELLOW}—{NC} Clash API 不可达")
        return

    try:
        proxies = json.loads(r.stdout).get("proxies", {})
    except Exception:
        print(f"  {YELLOW}—{NC} API 响应解析失败")
        return

    # ── 收集待测节点（多组间去重，保持顺序） ────────────────────────────────
    group_members: dict = {}
    for gname in target_groups:
        g = proxies.get(gname)
        if not g:
            print(f"  {YELLOW}—{NC} 组 {BOLD}{gname}{NC} 不存在，跳过")
            continue
        members = g.get("all", [])
        if not members:
            print(f"  {YELLOW}—{NC} 组 {BOLD}{gname}{NC} 无成员")
            continue
        group_members[gname] = members

    if not group_members:
        print(f"  {RED}✗{NC} 无可测组")
        return

    # 去重合并，保留首次出现顺序
    seen: set = set()
    all_nodes: list = []
    for members in group_members.values():
        for m in members:
            if m not in seen:
                all_nodes.append(m)
                seen.add(m)

    total = len(all_nodes)
    group_names = ", ".join(group_members.keys())
    print(f"{BOLD}测速{NC}  组: {CYAN}{group_names}{NC}  节点: {BOLD}{total}{NC}")

    # ── 并发测速，实时进度条 ──────────────────────────────────────────────────
    import threading
    done_count = [0]
    lock = threading.Lock()

    def _test_node(name: str):
        """调用 Clash API 测单节点延迟，结果写回引擎 history。"""
        encoded_name = urllib.parse.quote(name, safe="")
        encoded_url  = urllib.parse.quote(TEST_URL, safe="")
        endpoint = (f"{api}/proxies/{encoded_name}/delay"
                    f"?url={encoded_url}&timeout={TIMEOUT_MS}")
        subprocess.run(
            ["curl", "-s", "--noproxy", "*", "--max-time",
             str(TIMEOUT_MS // 1000 + 3),
             "-H", f"Authorization: Bearer {api_secret}",
             endpoint],
            capture_output=True, text=True, timeout=TIMEOUT_MS // 1000 + 5
        )
        with lock:
            done_count[0] += 1
            n = done_count[0]
            bar_len = 24
            filled  = int(bar_len * n / total)
            bar     = f"{GREEN}{'█' * filled}{NC}{'░' * (bar_len - filled)}"
            print(f"\r  [{bar}] {n}/{total}", end="", flush=True)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        list(pool.map(_test_node, all_nodes))

    print()  # 结束进度行

    # ── 重新拉取并展示最新延迟 ────────────────────────────────────────────────
    print()
    _proxy_groups_section(api, api_secret)


def cmd_check(engine, api: str, api_secret: str,
              config: dict, mode_str: str = ""):
    """proxyctl check — 4 阶段全面健康检查。

    Args:
        engine: Backend 实例
        api: Clash API 基础 URL
        api_secret: Clash API Bearer token
        config: 全局配置字典
        mode_str: 代理模式字符串（tun/proxy/mixed）
    """
    dns_lock_label = config.get("dns_lock_label", "com.proxyctl.dns-lock")
    claude_proxy_label = config.get("claude_proxy_label", "com.proxyctl.claude-proxy")
    sb_dir = config.get("config_dir", f"{HOME}/.config") + "/proxyctl"
    corp_dns = config.get("corp_dns", {}) or {}
    fail = False

    # [4/4] IP 请求完全独立，进入 cmd_check 就立刻发出
    import threading, re as _re
    cache_file = f"{HOME}/.config/sing-box/.ipgeo-cache"
    env_clean  = {k: v for k, v in os.environ.items()
                  if k not in ("http_proxy", "https_proxy", "all_proxy",
                               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    proxy_ip = direct_ip = claude_ip = ""

    def _fetch_proxy_ip():
        nonlocal proxy_ip
        r = subprocess.run(
            ["curl", "-s", "--max-time", "6", "--proxy", "socks5h://127.0.0.1:7890",
             "https://ifconfig.me"],
            capture_output=True, text=True, env=env_clean, timeout=10)
        proxy_ip = r.stdout.strip()

    def _fetch_direct_ip():
        nonlocal direct_ip
        r = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "--max-time", "4",
             "https://myip.ipip.net"],
            capture_output=True, text=True, env=env_clean, timeout=6)
        m = _re.search(r'(\d+\.\d+\.\d+\.\d+)', r.stdout)
        direct_ip = m.group(1) if m else ""

    def _fetch_claude_ip():
        nonlocal claude_ip
        # 通过 7890 代理访问 ipinfo.io（规则路由到 claude 组），反映真实出口
        r = subprocess.run(
            ["curl", "-s", "--max-time", "8", "--proxy", "socks5h://127.0.0.1:7890",
             "https://ipinfo.io/ip"],
            capture_output=True, text=True, env=env_clean, timeout=12)
        claude_ip = r.stdout.strip()

    ip_threads = [threading.Thread(target=fn)
                  for fn in (_fetch_proxy_ip, _fetch_direct_ip, _fetch_claude_ip)]
    for t in ip_threads:
        t.start()

    # ── 1. 基础状态 ──────────────────────────────────────────────────────────
    mode = mode_str  # 由调用方传入
    if mode == "tun":
        mode_tag = f"{GREEN}tun{NC}"
    elif mode == "proxy":
        mode_tag = f"{CYAN}proxy{NC}"
    else:
        mode_tag = f"{YELLOW}{mode}{NC}"
    print(f"{BOLD}[1/4] 基础状态{NC}  {BOLD}{GREEN}{engine.name}{NC} · {mode_tag}")

    # daemon
    r = subprocess.run(["launchctl", "print", engine.label],
                       capture_output=True, text=True)
    pid = next((l.split()[-1] for l in r.stdout.splitlines() if "pid =" in l), "")
    daemon_up = bool(pid and pid != "0")
    if daemon_up:
        r2 = subprocess.run(["ps", "-o", "etime=", "-p", pid],
                             capture_output=True, text=True)
        etime = r2.stdout.strip()
        print(f"  {GREEN}✓{NC} daemon PID {pid}, uptime {etime or '?'}")
    else:
        print(f"  {RED}✗{NC} daemon not running — 执行 sb start")
        return

    # 端口
    ok_ports, fail_ports = [], []
    for port, desc in [(53, "dns"), (7890, "proxy"), (9090, "api")]:
        (ok_ports if _port_listening(port) else fail_ports).append(desc)
        if not _port_listening(port):
            fail = True

    cp_label = f"system/{claude_proxy_label}"
    r = subprocess.run(["sudo", "launchctl", "print", cp_label], capture_output=True)
    if r.returncode == 0:
        cp_status = (f"{GREEN}claude-proxy✓{NC}" if _port_listening(7891)
                     else f"{YELLOW}claude-proxy(no-port){NC}")
    else:
        cp_status = f"{YELLOW}claude-proxy✗{NC}"

    if ok_ports:
        print(f"  {GREEN}✓{NC} ports: {' '.join(ok_ports)}  {cp_status}")
    if fail_ports:
        print(f"  {RED}✗{NC} missing: {' '.join(fail_ports)}  {cp_status}")

    # 网络/DNS/watchdog 状态行
    en0_ip = subprocess.run(["ifconfig", "en0"], capture_output=True, text=True).stdout
    en0_addr = next((l.split()[1] for l in en0_ip.splitlines()
                     if l.strip().startswith("inet ") and len(l.split()) >= 2), "")
    # 企业网络检测：仅当配置了 corp_dns.server 时启用
    corp_net = False
    corp_via = ""
    corp_server = corp_dns.get("server", "")
    corp_test_domain = corp_dns.get("test_domain", "")
    corp_prefix = corp_dns.get("ip_prefix", "")  # 如 "30." "10.0."

    if corp_server and corp_prefix:
        # 检测是否在企业网络中（通过 IP 前缀匹配）
        if en0_addr.startswith(corp_prefix):
            corp_net = True
            corp_via = f"直连({en0_addr})"
        else:
            r2 = subprocess.run(["ifconfig", "-l"], capture_output=True, text=True)
            for iface in r2.stdout.split():
                if not iface.startswith("utun"):
                    continue
                ri = subprocess.run(["ifconfig", iface], capture_output=True, text=True)
                for line in ri.stdout.splitlines():
                    if f"inet {corp_prefix}" in line:
                        parts = line.split()
                        if len(parts) >= 2:
                            corp_net = True
                            corp_via = f"VPN({iface},{parts[1]})"
                            break
                if corp_net:
                    break

    infra = corp_via if corp_net else f"{YELLOW}no-corp{NC}"

    # DNS：仅在 tun/mixed 模式下检查系统 DNS 是否指向 127.0.0.1
    dns_hijack = mode in ("tun", "mixed")
    dns_bad = False

    if dns_hijack and IS_MACOS:
        r3 = subprocess.run(["scutil", "--dns"], capture_output=True, text=True)
        sys_dns = ""
        for line in r3.stdout.splitlines():
            if "nameserver[0]" in line:
                sys_dns = line.split()[-1]
                break
        if sys_dns == "127.0.0.1":
            infra += "  DNS✓"
        else:
            infra += f"  {RED}DNS→{sys_dns or '?'}{NC}"
            dns_bad = True
            fail = True

        r4 = subprocess.run(["launchctl", "print", f"system/{dns_lock_label}"],
                            capture_output=True)
        infra += ("  lock✓" if r4.returncode == 0 else f"  {YELLOW}no-lock{NC}")
    elif dns_hijack:
        infra += f"  {YELLOW}DNS(需手动检查){NC}"
    else:
        infra += "  DNS(proxy模式)"

    watchdog_log = f"{sb_dir}/dns-watchdog.log"
    if os.path.isfile(watchdog_log):
        for line in reversed(open(watchdog_log).readlines()):
            if "[tuic-recover]" in line:
                ts = " ".join(line.split()[:2])
                infra += f"  {CYAN}last-recover:{ts}{NC}"
                break
    print(f"  {infra}")

    # ── 2. 代理组 ─────────────────────────────────────────────────────────────
    print(f"{BOLD}[2/4] 代理组{NC}")
    _proxy_groups_section(api, api_secret)

    # ── 3. 连通性 ─────────────────────────────────────────────────────────────
    tests = [
        ("https://www.google.com",    "google",    "proxy"),
        ("https://github.com",        "github",    "proxy"),
        ("https://discord.com",       "discord",   "proxy"),
        ("https://www.telegram.org",  "telegram",  "proxy"),
        ("https://api.anthropic.com", "anthropic", "proxy"),
        ("https://www.baidu.com",     "baidu",     "direct"),
        ("https://www.alipay.com",    "alipay",    "direct"),
    ]
    if corp_net:
        # 企业网络连通性测试（从 corp_dns 配置读取）
        corp_tests = config.get("corp_dns", {}).get("check_targets", [])
        if corp_server and corp_test_domain:
            tests.append(("corp-dns", "corp-dns", "corp-dns"))
        for target in corp_tests:
            # 格式: {"url": "tcp:10.0.0.1:22", "name": "server-1", "mode": "tcp"}
            tests.append((target["url"], target["name"], target.get("mode", "direct")))

    # [3/4] 连通性：每个测试完成立刻打印，不等其他测试
    print(f"{BOLD}[3/4] 连通性{NC}")

    results  = [None] * len(tests)
    ready    = [threading.Event() for _ in tests]

    def _run_test(idx, url, desc, mode_):
        try:
            if mode_ == "corp-dns":
                ok, line = _test_corp_dns(desc, corp_server, corp_test_domain)
            elif mode_ == "tcp":
                parts = url.removeprefix("tcp:").rsplit(":", 1)
                ok, line = _test_tcp(parts[0], int(parts[1]), desc)
            else:
                ok, line = _test_url(url, desc, mode_)
        except Exception as e:
            ok, line = False, f"  {RED}✗{NC} {desc}  error: {e}"
        results[idx] = (line, ok)
        ready[idx].set()

    with ThreadPoolExecutor(max_workers=8) as pool:
        for idx, (url, desc, mode_) in enumerate(tests):
            pool.submit(_run_test, idx, url, desc, mode_)

        # 按顺序等待并打印，每个 ready 时立刻输出
        for idx in range(len(tests)):
            ready[idx].wait()
            line, ok = results[idx]
            print(line)
            if not ok:
                fail = True

    # [4/4] 出口 IP：等 IP 线程结束，此时大概率已经跑完了
    print(f"{BOLD}[4/4] 出口 IP{NC}")
    for t in ip_threads:
        t.join()

    proxy_geo  = _ipgeo(proxy_ip,  cache_file, api_secret)
    claude_geo = _ipgeo(claude_ip, cache_file, api_secret)
    direct_geo = _ipgeo(direct_ip, cache_file, api_secret)

    print(f"  proxy  {_fmt_ip(proxy_ip, proxy_geo)}")
    print(f"  claude {_fmt_ip(claude_ip, claude_geo)}")
    print(f"  direct {_fmt_ip(direct_ip, direct_geo)}")
    if proxy_ip and direct_ip:
        if proxy_ip != direct_ip:
            print(f"  {GREEN}✓{NC} 分流正常")
        else:
            print(f"  {YELLOW}!{NC} 出口相同 — 检查分流规则")

    # ── 结果 ──────────────────────────────────────────────────────────────────
    print()
    if not fail:
        print(f"{GREEN}{BOLD}All checks passed.{NC}")
    else:
        print(f"{YELLOW}{BOLD}Some checks failed.{NC}")
        if dns_bad:
            print(f"{CYAN}DNS 异常，执行 {BOLD}sb fix{NC}{CYAN} 修复。{NC}")
