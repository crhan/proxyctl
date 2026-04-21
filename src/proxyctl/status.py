"""proxyctl status — 系统状态面板（并发采集数据，顺序打印）"""

import os
import platform
import re
import subprocess
import socket
from concurrent.futures import ThreadPoolExecutor

IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"


RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[0;33m"
CYAN   = "\033[0;36m"
BOLD   = "\033[1m"
NC     = "\033[0m"

HOME = os.path.expanduser("~")


# ── 基础工具 ──────────────────────────────────────────────────────────────────

def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _launchctl_pid(label: str, *, sudo: bool = False) -> str:
    cmd = (["sudo"] if sudo else []) + ["launchctl", "print", label]
    r = subprocess.run(cmd, capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "pid =" in line:
            return line.split()[-1]
    return ""


def _launchctl_runs(label: str) -> str:
    r = subprocess.run(["launchctl", "print", label], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "runs =" in line:
            return line.split()[-1]
    return ""


def _launchctl_running(label: str, *, sudo: bool = False) -> bool:
    cmd = (["sudo"] if sudo else []) + ["launchctl", "print", label]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def _ifconfig_ip(iface: str) -> str:
    r = subprocess.run(["ifconfig", iface], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == "inet" and len(parts) >= 2:
            return parts[1]
    return ""


# ── 数据采集函数（可并发） ────────────────────────────────────────────────────

def _gather_engine(engine) -> dict:
    """采集引擎进程信息：PID、运行次数、运行时间。"""
    if IS_LINUX:
        # systemd --user：用 systemctl show 获取 PID
        r = subprocess.run(
            ["systemctl", "--user", "show", engine.unit, "-p", "MainPID", "--value"],
            capture_output=True, text=True
        )
        pid = r.stdout.strip()
        daemon_up = bool(pid and pid != "0")
        runs = ""
        etime = ""
        if daemon_up:
            r2 = subprocess.run(["ps", "-o", "etime=", "-p", pid],
                                capture_output=True, text=True)
            etime = r2.stdout.strip()
        return {"pid": pid, "runs": runs, "daemon_up": daemon_up, "etime": etime}

    # macOS: launchctl
    pid   = _launchctl_pid(engine.label)
    runs  = _launchctl_runs(engine.label)
    daemon_up = bool(pid and pid != "0")
    etime = ""
    if daemon_up:
        r = subprocess.run(["ps", "-o", "etime=", "-p", pid],
                           capture_output=True, text=True)
        etime = r.stdout.strip()
    return {"pid": pid, "runs": runs, "daemon_up": daemon_up, "etime": etime}


def _gather_ports(claude_proxy_label: str) -> dict:
    """采集端口监听状态。"""
    ports = {desc: _port_listening(p)
             for p, desc in [(7890, "proxy"), (9090, "api")]}
    cp_running = False
    cp_pid = ""
    cp_port = False
    if IS_MACOS:
        cp_label = f"system/{claude_proxy_label}"
        cp_running = _launchctl_running(cp_label, sudo=True)
        # fallback：兼容 sb 遗留的 com.singbox.claude-proxy
        if not cp_running and claude_proxy_label != "com.singbox.claude-proxy":
            cp_label = "system/com.singbox.claude-proxy"
            cp_running = _launchctl_running(cp_label, sudo=True)
        cp_pid = _launchctl_pid(cp_label, sudo=True) if cp_running else ""
        cp_port = _port_listening(7891) if cp_running else False
    return {"ports": ports, "cp_running": cp_running,
            "cp_pid": cp_pid, "cp_port": cp_port}


def _gather_tun(engine, daemon_up: bool) -> dict:
    """采集 TUN 专属数据（Linux 最小集不启用 TUN，返回空数据）。"""
    tun_iface = addr = mtu = ""
    fakeip = hijack = ""
    excludes = []
    route_iface = ""

    if not IS_MACOS:
        # Linux 最小集：proxy-only mode，无 TUN
        return {"tun_iface": "", "addr": "", "mtu": "",
                "fakeip": "off", "hijack": "", "excludes": [],
                "route_iface": ""}

    if daemon_up:
        r = subprocess.run(["ifconfig", "-l"], capture_output=True, text=True)
        for iface in r.stdout.split():
            if not iface.startswith("utun"):
                continue
            ri = subprocess.run(["ifconfig", iface], capture_output=True, text=True)
            if "198.18." in ri.stdout or "fdfe:dcba" in ri.stdout:
                tun_iface = iface
                addr = _ifconfig_ip(iface)
                for line in ri.stdout.splitlines():
                    if "mtu" in line:
                        mtu = line.split()[-1]
                break

    # 引擎配置细节
    fakeip = hijack = ""
    excludes = []
    try:
        if engine.name == "mihomo":
            import yaml
            cfg = yaml.safe_load(open(engine.config))
            tun = cfg.get("tun", {})
            dns = cfg.get("dns", {})
            fakeip   = "on" if dns.get("enhanced-mode") == "fake-ip" else "off"
            hijack   = " ".join(tun.get("dns-hijack", [])) or "none"
            excludes = tun.get("route-exclude-address", [])
        else:
            import json
            cfg = json.load(open(engine.config))
            tun_cfg  = next((i for i in cfg.get("inbounds", [])
                             if i.get("type") == "tun"), {})
            fakeip   = "on" if any(r.get("server") == "fakeip-dns"
                                   for r in cfg.get("dns", {}).get("rules", [])) else "off"
            hijack   = "any:53" if tun_cfg.get("auto_redirect") else "via inbound"
            excludes = tun_cfg.get("route_exclude_address", [])
    except Exception:
        pass

    # 默认路由出口
    route_iface = ""
    if daemon_up:
        r = subprocess.run(["route", "-n", "get", "default"],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if "interface:" in line:
                route_iface = line.split()[-1]

    return {"tun_iface": tun_iface, "addr": addr, "mtu": mtu,
            "fakeip": fakeip, "hijack": hijack, "excludes": excludes,
            "route_iface": route_iface}


def _gather_proxy_settings() -> dict:
    """采集系统代理设置（仅 macOS，Linux 返回空）。"""
    if not IS_MACOS:
        return {"active_svc": "", "info": {}}

    # 找活跃网络服务
    active_svc = ""
    for svc in ["Wi-Fi", "USB 10/100/1000 LAN", "Thunderbolt Bridge", "Ethernet"]:
        r = subprocess.run(["networksetup", "-getinfo", svc],
                           capture_output=True, text=True)
        if "IP address: " in r.stdout and any(
                l.startswith("IP address:") and len(l.split()) > 2
                for l in r.stdout.splitlines()):
            active_svc = svc
            break
    if not active_svc:
        r = subprocess.run(["networksetup", "-listallnetworkservices"],
                           capture_output=True, text=True)
        svcs = [s for s in r.stdout.splitlines()[1:] if not s.startswith("*")]
        active_svc = svcs[0] if svcs else ""

    info = {}
    for proto, flag in [("http",  "-getwebproxy"),
                        ("https", "-getsecurewebproxy"),
                        ("socks", "-getsocksfirewallproxy")]:
        r = subprocess.run(["networksetup", flag, active_svc],
                           capture_output=True, text=True)
        on = port = ""
        for line in r.stdout.splitlines():
            if line.startswith("Enabled:"):
                on = line.split()[-1]
            elif line.startswith("Port:"):
                port = line.split()[-1]
        info[proto] = (on, port)

    return {"active_svc": active_svc, "info": info}


def _gather_dns(dns_lock_label: str) -> dict:
    """采集 DNS 状态（macOS: scutil --dns，Linux: 简化检测）。"""
    dns_up  = _port_listening(53)

    if not IS_MACOS:
        # Linux 最小集：不劫持 DNS，只检查 53 端口
        return {"dns_up": dns_up, "lock_up": False, "sys_dns": "",
                "resolvers": [], "overrides": []}

    lock_up = _launchctl_running(f"system/{dns_lock_label}")
    # fallback：兼容 sb 遗留的 com.singbox.dns-lock
    if not lock_up and dns_lock_label != "com.singbox.dns-lock":
        lock_up = _launchctl_running("system/com.singbox.dns-lock")

    r = subprocess.run(["scutil", "--dns"], capture_output=True, text=True)
    sys_dns = ""
    in_r1   = False
    for line in r.stdout.splitlines():
        if "resolver #1" in line:
            in_r1 = True
        if in_r1 and "nameserver[0]" in line:
            sys_dns = line.split()[-1]
            break

    overrides = []
    if os.path.isdir("/etc/resolver"):
        for rf in os.listdir("/etc/resolver"):
            full = f"/etc/resolver/{rf}"
            if os.path.isfile(full):
                with open(full) as f:
                    if any(line.startswith("nameserver") for line in f):
                        overrides.append(rf)

    return {"dns_up": dns_up, "lock_up": lock_up,
            "sys_dns": sys_dns, "overrides": overrides}


def _gather_network(engine) -> dict:
    """采集网络环境数据。

    核心能力（跨平台）：默认出口网卡和 IP。
    macOS 扩展：企业网检测、VPN 接口、Tailscale、TUIC relay。
    """
    # 核心：默认出口网卡和 IP
    default_iface = ""
    default_ip = ""

    if IS_MACOS:
        r = subprocess.run(["route", "-n", "get", "default"],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if "interface:" in line:
                default_iface = line.split()[-1]
        if default_iface:
            default_ip = _ifconfig_ip(default_iface)
    else:
        # Linux：从 ip route 获取默认出口
        r = subprocess.run(["ip", "route", "show", "default"],
                           capture_output=True, text=True)
        parts = r.stdout.split()
        for i, tok in enumerate(parts):
            if tok == "dev" and i + 1 < len(parts):
                default_iface = parts[i + 1]
                break
        if default_iface:
            r2 = subprocess.run(
                ["ip", "-4", "addr", "show", default_iface],
                capture_output=True, text=True)
            for line in r2.stdout.splitlines():
                if "inet " in line:
                    default_ip = line.split()[1].split("/")[0]
                    break

    result = {
        "default_iface": default_iface,
        "default_ip": default_ip,
    }

    if not IS_MACOS:
        return result

    # ── macOS 扩展：企业 VPN、Tailscale、Relay ──────────────────────────
    vpn_iface = vpn_ip = ""
    r = subprocess.run(["ifconfig", "-l"], capture_output=True, text=True)
    for iface in r.stdout.split():
        if not iface.startswith("utun"):
            continue
        ri = subprocess.run(["ifconfig", iface], capture_output=True, text=True)
        for line in ri.stdout.splitlines():
            if "inet 30." in line:
                parts = line.split()
                if len(parts) >= 2:
                    vpn_iface, vpn_ip = iface, parts[1]
                    break
        if vpn_iface:
            break
    result["vpn_iface"] = vpn_iface
    result["vpn_ip"] = vpn_ip

    # Tailscale
    ts_self = ts_peer_ip = ts_latency = ts_via = ""
    ts_state = "absent"
    if subprocess.run(["which", "tailscale"], capture_output=True).returncode == 0:
        ts_self = subprocess.run(["tailscale", "ip", "-4"],
                                  capture_output=True, text=True).stdout.strip()
        if ts_self:
            r = subprocess.run(["tailscale", "status"],
                                capture_output=True, text=True)
            for line in r.stdout.splitlines():
                if "home-ubuntu" in line:
                    ts_peer_ip = line.split()[0]
                    break
            if ts_peer_ip:
                rp = subprocess.run(
                    ["tailscale", "ping", "--c", "1", "--timeout", "2s", ts_peer_ip],
                    capture_output=True, text=True
                )
                ts_latency = next(
                    (w for w in rp.stdout.split() if w.endswith("ms")), "")
                ts_via = next(
                    (w for i, w in enumerate(rp.stdout.split())
                     if i > 0 and rp.stdout.split()[i - 1] == "via"), "")
                ts_state = "ok" if ts_latency else "unreachable"
            else:
                ts_state = "no-peer"
        else:
            ts_state = "no-login"
    result.update({
        "ts_self": ts_self, "ts_peer_ip": ts_peer_ip,
        "ts_latency": ts_latency, "ts_via": ts_via, "ts_state": ts_state,
    })

    # TUIC relay 解析路径
    relay_host = relay_ip = relay_path = ""
    try:
        import yaml
        cfg = yaml.safe_load(open(f"{HOME}/.config/mihomo/config.yaml"))
        relay_host = next((p.get("server", "") for p in cfg.get("proxies", [])
                           if p.get("type") == "tuic"), "")
        if relay_host:
            try:
                relay_ip   = socket.getaddrinfo(relay_host, 443,
                                                socket.AF_INET)[0][4][0]
                relay_path = "LAN" if relay_ip.startswith("192.168.") else "WAN"
            except Exception:
                pass
    except Exception:
        pass
    result.update({
        "relay_host": relay_host, "relay_ip": relay_ip, "relay_path": relay_path,
    })

    return result


# ── 打印函数（顺序执行，使用采集结果） ───────────────────────────────────────

def _print_engine(engine, mode: str, d_engine: dict, d_ports: dict):
    if mode == "tun":
        mode_tag = f"{GREEN}tun{NC}"
    elif mode == "proxy":
        mode_tag = f"{CYAN}proxy{NC}"
    else:
        mode_tag = f"{YELLOW}{mode}{NC}"

    print(f"{BOLD}引擎{NC}  {GREEN}{engine.name}{NC} · {mode_tag}")

    if d_engine["daemon_up"]:
        runs_str = (f"  {YELLOW}runs={d_engine['runs']}{NC}"
                    if d_engine["runs"] and d_engine["runs"] != "1" else "")
        print(f"  daemon  {GREEN}✓{NC} PID {d_engine['pid']}  "
              f"uptime {d_engine['etime'] or '?'}{runs_str}")
    else:
        print(f"  daemon  — stopped")

    port_parts = []
    for desc, ok in d_ports["ports"].items():
        if ok:
            port_parts.append(f"{GREEN}{desc}{NC}")
        elif d_engine["daemon_up"]:
            port_parts.append(f"{RED}{desc}✗{NC}")
        else:
            port_parts.append(f"{desc}—")
    print(f"  ports  {' '.join(port_parts)}")

    if d_ports["cp_running"]:
        if d_ports["cp_port"]:
            print(f"  claude  {GREEN}✓{NC} PID {d_ports['cp_pid'] or '?'} :7891")
        else:
            print(f"  claude  {YELLOW}✓{NC} daemon up, {RED}port 7891 not listening{NC}")
    else:
        print(f"  claude  — not running")


def _print_tun(engine, d_tun: dict):
    print(f"\n{BOLD}TUN{NC}")
    tun_iface = d_tun["tun_iface"]
    if tun_iface:
        print(f"  iface   {GREEN}{tun_iface}{NC}  "
              f"{d_tun['addr'] or '?'}  mtu={d_tun['mtu'] or '?'}")
    else:
        print(f"  iface   {RED}✗ 未找到 TUN 接口{NC}")

    if d_tun["fakeip"]:
        color = GREEN if d_tun["fakeip"] == "on" else YELLOW
        print(f"  fakeip  {color}{d_tun['fakeip']}{NC}")
    if d_tun["hijack"]:
        print(f"  hijack  {d_tun['hijack']}")
    excludes = d_tun["excludes"]
    if excludes:
        shown = " ".join(excludes[:6])
        extra = "..." if len(excludes) > 6 else ""
        print(f"  exclude {shown}{extra}")

    ri = d_tun["route_iface"]
    if ri:
        if tun_iface and ri == tun_iface:
            print(f"  route   default via {GREEN}{ri}{NC}")
        else:
            print(f"  route   default via {YELLOW}{ri}{NC} (非 TUN)")


def _print_proxy_settings(d_proxy: dict, daemon_up: bool, mode: str):
    print(f"\n{BOLD}系统代理{NC}")
    active_svc = d_proxy["active_svc"]
    if not active_svc:
        return

    any_on     = False
    proxy_parts = []
    bad_port   = False
    for proto, (on, port) in d_proxy["info"].items():
        if on == "Yes":
            any_on = True
            if port == "7890":
                proxy_parts.append(f"{GREEN}{proto.upper()}:{port}{NC}")
            else:
                proxy_parts.append(f"{YELLOW}{proto.upper()}:{port}{NC}")
                bad_port = True

    if any_on:
        print(f"  {active_svc}: {' '.join(proxy_parts)}")
        if bad_port:
            print(f"  {YELLOW}⚠ 端口 ≠ 7890，可能指向其他代理{NC}")
    else:
        if daemon_up and mode == "proxy":
            print(f"  {RED}✗{NC} 未开启 (proxy 模式需要开启)")
        elif daemon_up:
            print(f"  — 未开启 (tun 模式不需要)")
        else:
            print(f"  — 未开启")


def _print_dns(daemon_up: bool, d_dns: dict, mode: str):
    """打印 DNS 状态段。

    始终显示 listen/system/lock 的实际状态。
    mode 决定的是"异常判定标准"：
    - tun/mixed：DNS 必须指向 127.0.0.1，否则标红
    - proxy：只显示事实，不判定对错
    """
    dns_hijack = mode in ("tun", "mixed")

    print(f"\n{BOLD}DNS{NC}")

    # listen: 53 端口是否在监听
    if d_dns["dns_up"]:
        print(f"  listen  {GREEN}127.0.0.1:53{NC}")
    elif daemon_up and dns_hijack:
        # tun/mixed 模式下 53 没起来才算错
        print(f"  listen  {RED}127.0.0.1:53 ✗{NC}")
    elif daemon_up:
        # proxy 模式下 53 没起来是正常的
        print(f"  listen  — (proxy 模式，不需要)")
    else:
        print(f"  listen  — (daemon stopped)")

    # system: 当前系统 DNS 指向
    sys_dns = d_dns.get("sys_dns", "")
    if dns_hijack:
        if sys_dns == "127.0.0.1":
            tag = f"{GREEN}→ 127.0.0.1{NC}" if daemon_up else \
                  f"{RED}→ 127.0.0.1{NC} (daemon 未运行，DNS 将不可用!)"
        else:
            tag = (f"{RED}→ {sys_dns or 'unknown'}{NC} (应为 127.0.0.1)"
                   if daemon_up else f"→ {sys_dns or 'unknown'}")
    else:
        tag = f"→ {sys_dns or 'DHCP'}"
    print(f"  system  {tag}")

    # lock: dns-lock watchdog 状态
    if d_dns.get("lock_up"):
        tag = f"{GREEN}✓{NC} running" if daemon_up else \
              f"{YELLOW}✓{NC} running (daemon 未运行，建议 proxyctl dns-unlock)"
    else:
        tag = "— not running"
    print(f"  lock    {tag}")

    if d_dns.get("overrides"):
        print(f"  {YELLOW}⚠ /etc/resolver/ 覆盖: {' '.join(d_dns['overrides'])}{NC}")


def _print_network(d_net: dict):
    """打印网络状态段。

    核心（跨平台）：默认出口网卡 + IP。
    macOS 扩展：企业网/VPN、Tailscale、TUIC relay。
    """
    print(f"\n{BOLD}网络{NC}")

    # 核心：默认出口
    iface = d_net.get("default_iface", "")
    ip = d_net.get("default_ip", "")
    if iface and ip:
        # macOS 扩展：30.x 网段标记为办公网
        corp_tag = f"  {GREEN}办公网{NC}" if IS_MACOS and ip.startswith("30.") else ""
        print(f"  {iface:<8s}{ip}{corp_tag}")
    elif iface:
        print(f"  {iface:<8s}{YELLOW}no IP{NC}")
    else:
        print(f"  default {YELLOW}无默认路由{NC}")

    # ── macOS 扩展 ──
    if not IS_MACOS:
        return

    # 企业内网 VPN
    if d_net.get("vpn_iface"):
        print(f"  内网    {GREEN}✓{NC} {d_net['vpn_iface']}({d_net['vpn_ip']})")
    elif ip.startswith("30."):
        print(f"  内网    {GREEN}✓{NC} 直连")

    # Tailscale
    ts = d_net.get("ts_state", "absent")
    if ts == "absent":
        pass
    elif ts == "no-login":
        print(f"  tailsc  {YELLOW}—{NC} 未登录")
    elif ts == "no-peer":
        print(f"  tailsc  {YELLOW}!{NC} {d_net['ts_self']} (peer home-ubuntu 离线)")
    elif ts == "ok":
        via = f"via {d_net['ts_via']}" if d_net.get("ts_via") else ""
        print(f"  tailsc  {GREEN}✓{NC} {d_net['ts_self']} → "
              f"{d_net['ts_peer_ip']} {d_net['ts_latency']} {via}".rstrip())
    elif ts == "unreachable":
        print(f"  tailsc  {RED}✗{NC} {d_net['ts_self']} → "
              f"{d_net['ts_peer_ip']} 不可达")

    # TUIC relay
    if d_net.get("relay_host"):
        if d_net.get("relay_ip"):
            print(f"  relay   {GREEN}✓{NC} {d_net['relay_host']} → "
                  f"{d_net['relay_ip']} ({d_net['relay_path']})")
        else:
            print(f"  relay   {RED}✗{NC} {d_net['relay_host']} DNS 解析失败")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def cmd_status(engine, api: str, api_secret: str,
               config: dict, mode: str = ""):
    """proxyctl status — 并发采集数据，顺序打印状态面板。

    Args:
        engine: Backend 实例
        api: Clash API 基础 URL
        api_secret: Clash API Bearer token
        config: 全局配置字典
        mode: 代理模式字符串（tun/proxy/mixed）
    """
    dns_lock_label = config.get("dns_lock_label", "com.proxyctl.dns-lock")
    claude_proxy_label = config.get("claude_proxy_label", "com.proxyctl.claude-proxy")

    # 全部 section 并发采集，拿到一个打印一个
    with ThreadPoolExecutor(max_workers=8) as pool:
        f_engine  = pool.submit(_gather_engine, engine)
        f_ports   = pool.submit(_gather_ports, claude_proxy_label)
        f_tun     = pool.submit(_gather_tun, engine, True)
        f_proxy   = pool.submit(_gather_proxy_settings)
        f_dns     = pool.submit(_gather_dns, dns_lock_label)
        f_network = pool.submit(_gather_network, engine)

        d_engine  = f_engine.result()
        d_ports   = f_ports.result()
        _print_engine(engine, mode, d_engine, d_ports)
        daemon_up = d_engine["daemon_up"]

        if mode in ("tun", "mixed"):
            _print_tun(engine, f_tun.result())

        _print_proxy_settings(f_proxy.result(), daemon_up, mode)
        _print_dns(daemon_up, f_dns.result(), mode)
        _print_network(f_network.result())

    # 环境变量代理
    env_parts = [f"{var}={os.environ[var]}"
                 for var in ("http_proxy", "https_proxy", "all_proxy")
                 if os.environ.get(var)]
    if env_parts:
        print(f"\n{BOLD}ENV{NC}")
        print(f"  {' '.join(env_parts)}")
