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
DIM    = "\033[2m"
NC     = "\033[0m"

from proxyctl._io import maybe_disable_module_colors as _pc_maybe_disable
_pc_maybe_disable(__name__)

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
    """采集引擎进程信息：PID、运行次数、运行时间、版本（v0.4.7+）。"""
    from proxyctl.cli import get_engine_version
    version_info = get_engine_version(engine.name)  # None / dict

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
        return {"pid": pid, "runs": runs, "daemon_up": daemon_up,
                "etime": etime, "version": version_info}

    # macOS: launchctl
    pid   = _launchctl_pid(engine.label)
    runs  = _launchctl_runs(engine.label)
    daemon_up = bool(pid and pid != "0")
    etime = ""
    if daemon_up:
        r = subprocess.run(["ps", "-o", "etime=", "-p", pid],
                           capture_output=True, text=True)
        etime = r.stdout.strip()
    return {"pid": pid, "runs": runs, "daemon_up": daemon_up,
            "etime": etime, "version": version_info}


def _gather_ports(claude_proxy_label: str,
                   port_list: list[tuple[int, str]] | None = None) -> dict:
    """采集端口监听状态。

    Args:
        port_list: [(port, desc), ...]，由调用方根据 config/api_base 解析后传入。
                   None 时回退到 [(7890,"proxy"), (9090,"api")] 以兼容旧调用。
    """
    if port_list is None:
        port_list = [(7890, "proxy"), (9090, "api")]
    ports = [(p, desc, _port_listening(p)) for p, desc in port_list]
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
    """采集通用网络环境：默认出口网卡 + IP。

    本机特例（企业 VPN 接口、Tailscale peer、TUIC relay 解析路径等）走
    StatusSection 插件，不在 core 里采集。
    """
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

    return {"default_iface": default_iface, "default_ip": default_ip}


# ── 打印函数（顺序执行，使用采集结果） ───────────────────────────────────────

def _print_engine(engine, mode: str, d_engine: dict, d_ports: dict):
    if mode == "tun":
        mode_tag = f"{GREEN}tun{NC}"
    elif mode == "proxy":
        mode_tag = f"{CYAN}proxy{NC}"
    else:
        mode_tag = f"{YELLOW}{mode}{NC}"

    # 引擎名 + 模式 + 版本号（v0.4.7+，引擎 binary 找不到时只显示名字）
    ver = d_engine.get("version") or {}
    ver_str = ""
    if ver.get("version"):
        ver_str = f" {DIM}v{ver['version']}{NC}"
        if ver.get("build_date"):
            ver_str += f" {DIM}({ver['build_date']}, go{ver.get('go_version','?')}){NC}"
    print(f"{BOLD}引擎{NC}  {GREEN}{engine.name}{NC}{ver_str} · {mode_tag}")

    if d_engine["daemon_up"]:
        runs_str = (f"  {YELLOW}runs={d_engine['runs']}{NC}"
                    if d_engine["runs"] and d_engine["runs"] != "1" else "")
        print(f"  daemon  {GREEN}✓{NC} PID {d_engine['pid']}  "
              f"uptime {d_engine['etime'] or '?'}{runs_str}")
    else:
        print(f"  daemon  — stopped")

    port_parts = []
    for port_num, desc, ok in d_ports["ports"]:
        label = f"{desc}:{port_num}"
        if ok:
            port_parts.append(f"{GREEN}{label}{NC}")
        elif d_engine["daemon_up"]:
            port_parts.append(f"{RED}{label}✗{NC}")
        else:
            port_parts.append(f"{label}—")
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


def _print_network(d_net: dict, ctx: dict | None = None, registry=None):
    """打印网络状态段（通用部分）：默认出口网卡 + IP。

    其他信息（企业 VPN、Tailscale、TUIC relay 等）由插件的 StatusSection 提供，
    在打印完通用部分后由 core 统一调度。
    """
    print(f"\n{BOLD}网络{NC}")

    iface = d_net.get("default_iface", "")
    ip = d_net.get("default_ip", "")
    if iface and ip:
        print(f"  {iface:<8s}{ip}")
    elif iface:
        print(f"  {iface:<8s}{YELLOW}no IP{NC}")
    else:
        print(f"  default {YELLOW}无默认路由{NC}")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def cmd_status(engine, api: str, api_secret: str,
               config: dict, mode: str = "", registry=None):
    """proxyctl status — 并发采集数据，顺序打印状态面板。

    Args:
        engine: Backend 实例
        api: Clash API 基础 URL
        api_secret: Clash API Bearer token
        config: 全局配置字典
        mode: 代理模式字符串（tun/proxy/mixed）
        registry: PluginRegistry，提供 status_sections（VPN/Tailscale/TUIC relay 等）
    """
    # 检测 --json 全局 flag（Step 2 注入到 explain._GLOBAL_FLAGS）
    try:
        from proxyctl.explain import GLOBAL_FLAGS_REF as _gf
        as_json = bool(_gf().get("json"))
    except Exception:
        as_json = False

    dns_lock_label = config.get("dns_lock_label", "com.proxyctl.dns-lock")
    claude_proxy_label = config.get("claude_proxy_label", "com.proxyctl.claude-proxy")

    # 端口列表：proxy 取 config.proxy_port，api 从 api_base URL 解析
    proxy_port = int(config.get("proxy_port", 7890))
    api_port = 9090
    try:
        from urllib.parse import urlparse
        api_port = urlparse(api).port or 9090
    except Exception:
        pass
    port_list = [(proxy_port, "proxy"), (api_port, "api")]

    # 全部 section 并发采集，拿到一个打印一个
    with ThreadPoolExecutor(max_workers=8) as pool:
        f_engine  = pool.submit(_gather_engine, engine)
        f_ports   = pool.submit(_gather_ports, claude_proxy_label, port_list)
        f_tun     = pool.submit(_gather_tun, engine, True)
        f_proxy   = pool.submit(_gather_proxy_settings)
        f_dns     = pool.submit(_gather_dns, dns_lock_label)
        f_network = pool.submit(_gather_network, engine)

        # 插件 status_sections：每个 section 并发跑 gather
        ctx = {"engine": engine.name, "mode": mode, "config": config}
        sections = []
        if registry is not None:
            sections = registry.collect("status_sections", ctx=ctx)
        f_sections = [(s, pool.submit(s.gather, ctx)) for s in sections]

        d_engine  = f_engine.result()
        d_ports   = f_ports.result()
        d_tun     = f_tun.result()
        d_proxy   = f_proxy.result()
        d_dns     = f_dns.result()
        d_network = f_network.result()
        daemon_up = d_engine["daemon_up"]

        d_sections: dict = {}
        for section, future in f_sections:
            try:
                d_sections[section.name] = future.result()
            except Exception as e:
                d_sections[section.name] = {"error": str(e)}

    env_proxy = {var: os.environ[var]
                 for var in ("http_proxy", "https_proxy", "all_proxy")
                 if os.environ.get(var)}

    # 订阅状态（从契约文件读，本身不拉远端；文件不存在为 None 不报错）
    from proxyctl import subscription as _sub
    sub_data = _sub.load()

    if as_json:
        from proxyctl._io import emit_json, envelope
        data = {
            "engine": d_engine,
            "ports": d_ports,
            "tun": d_tun if mode in ("tun", "mixed") else None,
            "system_proxy": d_proxy,
            "dns": d_dns,
            "network": d_network,
            "sections": _jsonify(d_sections),
            "env": env_proxy,
            "mode": mode,
            "backend": engine.name,
            "subscription": sub_data,
        }
        # 订阅风险摘要 → envelope.hints（过期 / 流量满 / fetch fail）
        hints = _sub.summarize_hints(sub_data) if sub_data else []
        emit_json(envelope("status", data=data,
                           ok=bool(daemon_up),
                           code=0 if daemon_up else 5,
                           hints=hints))
        return

    # 人类模式：与之前一致的顺序渲染
    _print_engine(engine, mode, d_engine, d_ports)
    if mode in ("tun", "mixed"):
        _print_tun(engine, d_tun)
    _print_proxy_settings(d_proxy, daemon_up, mode)
    _print_dns(daemon_up, d_dns, mode)
    _print_network(d_network)
    # 插件 sections（VPN/Tailscale/TUIC relay 等本机特例都走这里）
    for section in sections:
        try:
            section.render(ctx, d_sections.get(section.name))
        except Exception as e:
            import sys
            sys.stderr.write(
                f"[plugin warning] status_section {section.name} failed: {e}\n"
            )

    if sub_data:
        _print_subscription(sub_data)

    if env_proxy:
        print(f"\n{BOLD}ENV{NC}")
        print(f"  {' '.join(f'{k}={v}' for k, v in env_proxy.items())}")


def _print_subscription(sub: dict) -> None:
    """打印订阅状态一段。受 severity 染色。"""
    from proxyctl import subscription as _sub
    sev = _sub.severity(sub)
    color = GREEN if sev == "ok" else (YELLOW if sev == "warn" else RED)
    mark = "✓" if sev == "ok" else ("!" if sev == "warn" else "✗")
    print(f"\n{BOLD}SUBSCRIPTION{NC}")
    print(f"  {color}{mark}{NC} {_sub.format_line(sub)}")
    updated = _sub.updated_at_human(sub)
    if updated:
        print(f"     {updated}")
    info_nodes = sub.get("info_nodes") or []
    if info_nodes:
        # 机场塞 name 的元信息行（"官网" / "已用" / "到期"），逐行展示
        for line in info_nodes[:4]:
            print(f"     {CYAN}{line}{NC}")


def _jsonify(obj):
    """递归地把对象转换成 JSON 可序列化的形式。"""
    if isinstance(obj, dict):
        return {str(k): _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(x) for x in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    return str(obj)
