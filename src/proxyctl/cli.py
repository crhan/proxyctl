"""proxyctl — Proxy configuration lifecycle management

支持 Mihomo 后端（首发）/ Sing-box 后端（预留）

用法：proxyctl [start|stop|restart|status|log|check|fix|recover|
               mode|audit|trace|bench|dns-lock|dns-unlock]
"""

import json
import os
import platform
import re
import socket
import subprocess
import sys
import time

from proxyctl import _io
from proxyctl._io import maybe_disable_module_colors
maybe_disable_module_colors(__name__)

# ── 平台检测 ────────────────────────────────────────────────────────────────
PLATFORM = platform.system()   # "Darwin" | "Linux"
IS_MACOS = PLATFORM == "Darwin"
IS_LINUX = PLATFORM == "Linux"

# ── 路径常量 ────────────────────────────────────────────────────────────────
HOME = os.path.expanduser("~")
DEFAULT_CONFIG_DIR = os.path.join(HOME, ".config", "proxyctl")
CONFIG_FILE = os.path.join(DEFAULT_CONFIG_DIR, "config.yaml")

# 默认后端：mihomo（首发支持）
DEFAULT_BACKEND = "mihomo"

# ── 颜色 ────────────────────────────────────────────────────────────────────
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
DIM = "\033[2m"
NC = "\033[0m"

# ── 默认配置（可通过 config.yaml 覆盖）───────────────────────────────────────
DEFAULTS = {
    "backend": "mihomo",  # mihomo | singbox
    "api_base": "http://127.0.0.1:9090",
    "api_secret": "",     # 必填：Clash API Bearer token
    "config_dir": os.path.join(HOME, ".config"),
    "dns_lock_label": "com.proxyctl.dns-lock",
    # 引擎对外暴露的 HTTP/SOCKS mixed-port（应与 mihomo config 的 port/mixed-port 一致）
    "proxy_port": 7890,
    # 个人附加的 NO_PROXY 项（追加到默认 localhost/私网集合之后）
    # 例: ["corp.example.com", "intranet.local"] 或 "corp.example.com,intranet.local"
    "no_proxy_extra": [],
}

SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
USER_PLUGIN_DIR = os.path.join(DEFAULT_CONFIG_DIR, "plugins")


def load_config() -> dict:
    """加载配置文件，返回合并后的配置字典。"""
    cfg = DEFAULTS.copy()
    if os.path.isfile(CONFIG_FILE):
        try:
            import yaml
            with open(CONFIG_FILE) as f:
                user_cfg = yaml.safe_load(f) or {}
            cfg.update(user_cfg)
        except Exception as e:
            print(f"{YELLOW}警告：读取配置文件失败：{e}{NC}")
            print(f"  使用默认配置，可能需要在 {CONFIG_FILE} 中配置 api_secret")
    return cfg


# ── 后端类 ───────────────────────────────────────────────────────────────────

class Backend:
    """后端抽象基类，封装引擎名称、路径和平台差异。"""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.config_dir = config.get("config_dir", os.path.join(HOME, ".config"))

    @property
    def label(self) -> str:
        """macOS launchctl service label。"""
        raise NotImplementedError

    @property
    def plist(self) -> str:
        """macOS LaunchDaemon plist 路径。"""
        raise NotImplementedError

    @property
    def unit(self) -> str:
        """Linux systemd user service unit 名称。"""
        raise NotImplementedError

    @property
    def config_file(self) -> str:
        raise NotImplementedError

    @property
    def cache_file(self) -> str:
        raise NotImplementedError

    @property
    def log_file(self) -> str:
        raise NotImplementedError


class MihomoBackend(Backend):
    """Mihomo (Clash) 后端实现"""

    def __init__(self, config: dict):
        super().__init__("mihomo", config)

    @property
    def label(self) -> str:
        return "system/com.mihomo.tun"

    @property
    def plist(self) -> str:
        return "/Library/LaunchDaemons/com.mihomo.tun.plist"

    @property
    def unit(self) -> str:
        return "mihomo.service"

    @property
    def config_file(self) -> str:
        return os.path.join(self.config_dir, "mihomo", "config.yaml")

    @property
    def cache_file(self) -> str:
        return os.path.join(self.config_dir, "mihomo", "cache.db")

    @property
    def log_file(self) -> str:
        return os.path.join(self.config_dir, "mihomo", "mihomo.log")


class SingboxBackend(Backend):
    """Sing-box 后端实现"""

    def __init__(self, config: dict):
        super().__init__("singbox", config)

    @property
    def label(self) -> str:
        return "system/com.singbox.tun"

    @property
    def plist(self) -> str:
        return "/Library/LaunchDaemons/com.singbox.tun.plist"

    @property
    def unit(self) -> str:
        return "sing-box.service"

    @property
    def config_file(self) -> str:
        return os.path.join(self.config_dir, "sing-box", "config.json")

    @property
    def cache_file(self) -> str:
        return os.path.join(self.config_dir, "sing-box", "cache.db")

    @property
    def log_file(self) -> str:
        return os.path.join(self.config_dir, "sing-box", "sing-box.log")


def get_backend(config: dict) -> Backend:
    """根据配置返回后端实例。"""
    backend_name = config.get("backend", DEFAULT_BACKEND)
    if backend_name == "mihomo":
        return MihomoBackend(config)
    else:
        return SingboxBackend(config)


# ── 插件加载 ─────────────────────────────────────────────────────────────────

def load_plugins(config: dict):
    """加载内置 + 用户插件目录，返回 registry。

    用户禁用某插件：在 config.yaml 加 `plugins_disabled: [name1, name2]`。
    用户插件目录：~/.config/proxyctl/plugins/*.py
    """
    from proxyctl.core.plugin import build_registry
    return build_registry(config, USER_PLUGIN_DIR)


def cmd_plugins(registry):
    """proxyctl plugins — 显示已加载插件 + 加载期错误。"""
    print(f"{BOLD}插件列表{NC}  (内置 + ~/.config/proxyctl/plugins/)")
    if not registry.plugins and not registry.errors:
        print(f"  {YELLOW}—{NC} 无已加载插件")
    for p in registry.plugins:
        # 简略列出该插件实现了哪些 hook（识别 dataclass 返回的方法）
        hook_names = [
            "check_groups", "check_targets", "check_outbound_probes",
            "dns_hooks", "route_hooks", "status_sections",
            "watchdog_layers", "audit_skip_hosts", "audit_known_proxy_kw",
        ]
        active = []
        for h in hook_names:
            method = getattr(type(p), h, None)
            base_method = getattr(__import__("proxyctl.core.plugin",
                                              fromlist=["Plugin"]).Plugin, h)
            # 比较函数对象，子类覆盖才算 active
            if method is not None and method is not base_method:
                active.append(h)
        active_str = ", ".join(active) if active else "(no hooks)"
        print(f"  {GREEN}✓{NC} {CYAN}{p.name or type(p).__name__}{NC}  "
              f"[{type(p).__module__}]  {active_str}")
    if registry.errors:
        print(f"\n{YELLOW}加载错误:{NC}")
        for source, err in registry.errors:
            print(f"  {RED}✗{NC} {source}  {err}")
    print(f"\n用户插件目录: {USER_PLUGIN_DIR}")
    if not os.path.isdir(USER_PLUGIN_DIR):
        print(f"  {YELLOW}—{NC} 目录不存在（首次使用请 mkdir -p）")


# ── 基础工具 ─────────────────────────────────────────────────────────────────

def run(cmd: list, *, sudo: bool = False, check: bool = False,
        capture: bool = False, stdin_text: str = None) -> subprocess.CompletedProcess:
    """执行系统命令。sudo=True 自动加 sudo，capture=True 返回文本输出。"""
    if sudo:
        cmd = ["sudo"] + cmd
    kw: dict = {"check": check}
    if capture:
        kw["capture_output"] = True
        kw["text"] = True
    if stdin_text is not None:
        kw["input"] = stdin_text
        kw["text"] = True
    return subprocess.run(cmd, **kw)


def run_out(cmd: list, *, sudo: bool = False) -> str:
    """执行命令，返回 stdout；失败返回空字符串。"""
    r = run(cmd, sudo=sudo, capture=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def wait_port(port: int, timeout: float = 10.0) -> bool:
    """轮询直到 127.0.0.1:port 就绪，超时返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def list_network_services() -> list:
    """返回系统所有网络服务名称（过滤掉 * 前缀的禁用项）。"""
    out = run_out(["networksetup", "-listallnetworkservices"])
    return [s for s in out.splitlines()[1:] if not s.startswith("*")]


def launchctl_running(label: str, *, sudo: bool = False) -> bool:
    """检查 launchctl 服务是否在运行（仅 macOS）。"""
    r = run(["launchctl", "print", label], sudo=sudo, capture=True)
    return r.returncode == 0


# ── 平台感知的服务管理 ──────────────────────────────────────────────────────

def service_running(backend: Backend) -> bool:
    """检查引擎服务是否在运行。"""
    if IS_MACOS:
        return launchctl_running(backend.label)
    r = run(["systemctl", "--user", "is-active", "--quiet", backend.unit], capture=True)
    return r.returncode == 0


def service_start(backend: Backend, config: dict) -> subprocess.CompletedProcess:
    """启动引擎服务。

    Args:
        backend: 后端实例
        config: 全局配置（macOS 需要定位 plist 源文件）

    Returns:
        subprocess.CompletedProcess
    """
    if IS_MACOS:
        if not os.path.isfile(backend.plist):
            src = os.path.join(DEFAULT_CONFIG_DIR, "launchdaemons",
                               os.path.basename(backend.plist))
            if not os.path.isfile(src):
                _io.fail(f"plist 源文件不存在：{src}",
                         hint="重新跑 install.sh 或检查 launchdaemons/ 目录",
                         doc="engine", code=_io.NOT_FOUND, cmd="start")
            run(["cp", src, backend.plist], sudo=True)
        return run(["launchctl", "bootstrap", "system", backend.plist],
                   sudo=True, capture=True)
    return run(["systemctl", "--user", "start", backend.unit], capture=True)


def service_stop(backend: Backend) -> subprocess.CompletedProcess:
    """停止引擎服务。"""
    if IS_MACOS:
        return run(["launchctl", "bootout", backend.label], sudo=True, capture=True)
    return run(["systemctl", "--user", "stop", backend.unit], capture=True)


def service_restart(backend: Backend) -> subprocess.CompletedProcess:
    """重启引擎服务。"""
    if IS_MACOS:
        return run(["launchctl", "kickstart", "-k", backend.label],
                   sudo=True, capture=True)
    return run(["systemctl", "--user", "restart", backend.unit], capture=True)


def scutil_exec(script: str):
    """向 sudo scutil stdin 写入脚本并执行。"""
    run(["scutil"], sudo=True, stdin_text=script)


def get_mode(backend: Backend) -> str:
    """从配置文件读取当前模式 (tun/proxy/mixed/unknown)。"""
    try:
        if backend.name == "mihomo":
            text = open(backend.config_file).read()
            tun_m = re.search(r'^tun:\s*\n((?:\s+.*\n)*)', text, re.M)
            tun_block = tun_m.group(0) if tun_m else ""
            tun_on = bool(re.search(r'enable:\s*true', tun_block))
            auto_rt = bool(re.search(r'auto-route:\s*true', tun_block))
            fakeip = bool(re.search(r'enhanced-mode:\s*fake-ip', text))
            if tun_on and auto_rt and fakeip:
                return "tun"
            elif not auto_rt and not fakeip:
                return "proxy"
            return "mixed"
        else:
            cfg = json.load(open(backend.config_file))
            ar = True
            for ib in cfg.get("inbounds", []):
                if ib.get("type") == "tun":
                    ar = ib.get("auto_route", True)
                    break
            fakeip = any(r.get("server") == "fakeip-dns"
                         for r in cfg.get("dns", {}).get("rules", []))
            if ar and fakeip:
                return "tun"
            elif not ar and not fakeip:
                return "proxy"
            return "mixed"
    except Exception:
        return "unknown"


def get_primary_resolver() -> str:
    """返回 scutil --dns resolver #1 的第一个 nameserver。"""
    r = subprocess.run(["scutil", "--dns"], capture_output=True, text=True)
    in_r1 = False
    for line in r.stdout.splitlines():
        if "resolver #1" in line:
            in_r1 = True
        if in_r1 and "nameserver[0]" in line:
            return line.split()[-1]
    return ""


# ── DNS 联动 ─────────────────────────────────────────────────────────────────

def dns_activate(config: dict):
    """设置系统 DNS → 127.0.0.1（三层防线）。

    Args:
        config: 全局配置字典，层 2 的 domain fallback 从 corp_dns.domain 读取
    """
    # 层 1: networksetup 对抗 DHCP
    for svc in list_network_services():
        run(["networksetup", "-setdnsservers", svc, "127.0.0.1"])

    # 层 2: 劫持 AnyConnect 自己的 DNS 条目
    ac_key = "State:/Network/Service/com.cisco.anyconnect/DNS"
    r = subprocess.run(["sudo", "scutil"], input=f"show {ac_key}\n",
                       capture_output=True, text=True)
    if "ServerAddresses" in r.stdout:
        domain = search_order = ""
        for line in r.stdout.splitlines():
            if "DomainName" in line and ":" in line and not domain:
                domain = line.split(":", 1)[1].strip()
            elif "SearchOrder" in line and ":" in line and not search_order:
                search_order = line.split(":", 1)[1].strip()
        corp = config.get("corp_dns", {}) or {}
        domain = domain or corp.get("domain", "") or "example.com"
        search_order = search_order or "1"
        scutil_exec(
            f"d.init\n"
            f"d.add DomainName {domain}\n"
            f"d.add SearchDomains * {domain}\n"
            f"d.add SearchOrder # {search_order}\n"
            f"d.add ServerAddresses * 127.0.0.1\n"
            f"d.add SupplementalMatchDomains * \"\" {domain}\n"
            f"set {ac_key}\n"
        )

    # 层 3: scutil 兜底 order:0
    scutil_exec(
        "d.init\n"
        "d.add ServerAddresses * 127.0.0.1\n"
        "d.add SupplementalMatchOrder # 0\n"
        "set State:/Network/Service/proxyctl-dns-override/DNS\n"
    )

    run(["dscacheutil", "-flushcache"], sudo=True)
    run(["killall", "-HUP", "mDNSResponder"], sudo=True)


def dns_deactivate(config: dict):
    """还原系统 DNS（清除三层注入）。

    静态 IP 和 DHCP 环境均安全：层 1 仅清除手动 DNS 设置，
    不影响静态配置中已有的 DNS；层 3 根据 corp_dns 配置决定
    还原为企业 DNS 或直接移除 AnyConnect key。

    Args:
        config: 全局配置字典，corp_dns.server 用于 AnyConnect DNS 还原
    """
    # 层 1: 清空手动 DNS（networksetup -setdnsservers <svc> empty
    # 效果：移除手动覆盖，恢复为网络服务自身的 DNS 来源——DHCP 或静态配置均可）
    for svc in list_network_services():
        run(["networksetup", "-setdnsservers", svc, "empty"])

    # 层 2: 删除 scutil 兜底 resolver
    scutil_exec("remove State:/Network/Service/proxyctl-dns-override/DNS\n")

    # 层 3: 还原 AnyConnect DNS key（如果还指向 127.0.0.1）
    ac_key = "State:/Network/Service/com.cisco.anyconnect/DNS"
    r = subprocess.run(["sudo", "scutil"], input=f"show {ac_key}\n",
                       capture_output=True, text=True)
    addr = ""
    for line in r.stdout.splitlines():
        if "0 :" in line:
            addr = line.split(":", 1)[1].strip()
            break
    if addr == "127.0.0.1":
        corp = config.get("corp_dns", {}) or {}
        corp_server = corp.get("server", "")
        if corp_server:
            # 有企业 DNS 配置：还原为企业 DNS 地址
            corp_v6 = corp.get("server_v6", "")
            corp_domain = corp.get("domain", "") or "example.com"
            addrs = f"{corp_server} {corp_v6}" if corp_v6 else corp_server
            scutil_exec(
                "d.init\n"
                f"d.add ServerAddresses * {addrs}\n"
                f"d.add DomainName {corp_domain}\n"
                f"d.add SearchDomains * {corp_domain}\n"
                "d.add SearchOrder # 1\n"
                f"d.add SupplementalMatchDomains * \"\" {corp_domain}\n"
                f"set {ac_key}\n"
            )
        else:
            # 无企业 DNS 配置：直接移除 key，vpnagentd 重连时会自动重建
            scutil_exec(f"remove {ac_key}\n")

    run(["dscacheutil", "-flushcache"], sudo=True)
    run(["killall", "-HUP", "mDNSResponder"], sudo=True)


def dns_lock_start(config: dict):
    """启动 dns-lock watchdog daemon（仅 macOS）。"""
    if not IS_MACOS:
        return
    dns_lock_label = config.get("dns_lock_label", DEFAULTS["dns_lock_label"])
    dns_lock_plist_src = os.path.join(DEFAULT_CONFIG_DIR, "launchdaemons", f"{dns_lock_label}.plist")
    dns_lock_plist = f"/Library/LaunchDaemons/{dns_lock_label}.plist"

    if os.path.isfile(dns_lock_plist) and not launchctl_running(f"system/{dns_lock_label}"):
        run(["launchctl", "bootstrap", "system", dns_lock_plist], sudo=True)


def dns_lock_stop(config: dict):
    """停止 dns-lock watchdog daemon（仅 macOS）。"""
    if not IS_MACOS:
        return
    dns_lock_label = config.get("dns_lock_label", DEFAULTS["dns_lock_label"])
    if launchctl_running(f"system/{dns_lock_label}"):
        run(["launchctl", "bootout", f"system/{dns_lock_label}"], sudo=True)


# ── 系统代理联动 ──────────────────────────────────────────────────────────────

def proxy_activate():
    """设置系统 HTTP/HTTPS/SOCKS 代理 → 127.0.0.1:7890。"""
    for svc in list_network_services():
        run(["networksetup", "-setwebproxy", svc, "127.0.0.1", "7890"])
        run(["networksetup", "-setsecurewebproxy", svc, "127.0.0.1", "7890"])
        run(["networksetup", "-setsocksfirewallproxy", svc, "127.0.0.1", "7890"])
        run(["networksetup", "-setwebproxystate", svc, "on"])
        run(["networksetup", "-setsecurewebproxystate", svc, "on"])
        run(["networksetup", "-setsocksfirewallproxystate", svc, "on"])


def proxy_deactivate():
    """关闭系统代理。"""
    for svc in list_network_services():
        run(["networksetup", "-setwebproxystate", svc, "off"])
        run(["networksetup", "-setsecurewebproxystate", svc, "off"])
        run(["networksetup", "-setsocksfirewallproxystate", svc, "off"])


# ── 引擎启停共用逻辑 ──────────────────────────────────────────────────────────

def _wait_ready(backend: Backend):
    """等待引擎端口就绪。

    macOS：等 DNS(53) + 代理(7890)，因为可能启用 TUN/fakeip。
    Linux（最小集）：只等代理(7890)，不劫持 DNS。
    """
    if IS_MACOS:
        wait_port(53, timeout=10)
    wait_port(int(backend.config.get("proxy_port", DEFAULTS["proxy_port"])),
              timeout=10)
    time.sleep(3)


# ── 路由钩子调度 ──────────────────────────────────────────────────────────────

def _apply_route_hooks(registry, ctx: dict, action: str) -> None:
    """调度所有插件的 RouteHook。action ∈ {'activate', 'deactivate'}。

    单个 hook 失败只打 warning，不中断主流程——路由注入是辅助优化，
    不能让某个插件挂了拖死 start/stop。
    """
    if registry is None:
        return
    hooks = registry.collect("route_hooks")
    for h in hooks:
        fn = getattr(h, action, None)
        if fn is None:
            continue
        try:
            fn(ctx)
        except Exception as e:
            sys.stderr.write(
                f"[route_hook warning] {h.name}.{action} failed: "
                f"{type(e).__name__}: {e}\n"
            )


# ── 命令：start ───────────────────────────────────────────────────────────────

def cmd_start(backend: Backend, config: dict, registry=None):
    r = service_start(backend, config)
    if r.returncode != 0:
        _io.fail(f"{backend.name} 启动失败",
                 hints=[r.stderr.strip()] if r.stderr else None,
                 doc="troubleshooting",
                 code=_io.ENGINE_DOWN, cmd="start")

    print(f"{backend.name} started")
    _wait_ready(backend)

    if IS_MACOS:
        dns_activate(config)
        dns_lock_start(config)
        print("DNS → 127.0.0.1 (已激活)")
        if get_mode(backend) == "proxy":
            proxy_activate()
            print("系统代理 → 127.0.0.1:7890 (已激活)")

    _apply_route_hooks(registry,
                       {"engine": backend.name, "config": config, "phase": "start"},
                       "activate")


# ── 命令：stop ────────────────────────────────────────────────────────────────

def cmd_stop(backend: Backend, config: dict, registry=None):
    _apply_route_hooks(registry,
                       {"engine": backend.name, "config": config, "phase": "stop"},
                       "deactivate")

    if IS_MACOS:
        dns_lock_stop(config)
        dns_deactivate(config)
        proxy_deactivate()
        print("DNS → DHCP (已还原), 系统代理已关闭")

    service_stop(backend)
    print(f"{backend.name} stopped")


# ── 命令：restart ─────────────────────────────────────────────────────────────

def cmd_restart(backend: Backend, config: dict, *, clean: bool = False, registry=None):
    if clean and os.path.isfile(backend.cache_file):
        os.remove(backend.cache_file)
    # 人工介入后清掉 watchdog 的失败状态，避免误判为"还在触顶窗口内"
    for f in ("/tmp/proxyctl-recover-history", "/tmp/proxyctl-recover-stuck",
              "/tmp/proxyctl-proxy-fail", "/tmp/proxyctl-recover-cooldown",
              "/tmp/sb-recover-history", "/tmp/sb-recover-stuck",
              "/tmp/sb-recover-count", "/tmp/sb-proxy-fail"):
        try: os.remove(f)
        except (FileNotFoundError, PermissionError): pass
    service_restart(backend)
    print(f"{backend.name} restarted{'  (cache cleared)' if clean else ''}")
    _wait_ready(backend)

    if IS_MACOS:
        dns_activate(config)
        dns_lock_start(config)
        print("DNS → 127.0.0.1 (已刷新)")
        if get_mode(backend) == "proxy":
            proxy_activate()
            print("系统代理 → 127.0.0.1:7890")
        else:
            proxy_deactivate()

    _apply_route_hooks(registry,
                       {"engine": backend.name, "config": config, "phase": "restart"},
                       "activate")


# ── 命令：fix ─────────────────────────────────────────────────────────────────

def cmd_fix(backend: Backend, config: dict, registry=None):
    """修复引擎状态：运行中则重注入 DNS/代理，已停止则还原系统配置。"""
    api_base = config.get("api_base", DEFAULTS["api_base"])
    api_secret = config.get("api_secret", "")

    if service_running(backend):
        if IS_MACOS:
            print(f"{BOLD}[引擎运行中] 修复 DNS → 127.0.0.1{NC}")
            before = get_primary_resolver()
            print(f"  修复前 primary resolver: {before or 'unknown'}")

            dns_activate(config)
            after = get_primary_resolver()
            if after == "127.0.0.1":
                print(f"  {GREEN}✓{NC} primary resolver → 127.0.0.1")
            else:
                print(f"  {RED}✗{NC} primary resolver 仍为 {after}，需手动排查")

            if get_mode(backend) == "proxy":
                proxy_activate()
                print(f"  {GREEN}✓{NC} 系统代理 → 127.0.0.1:7890")

            _apply_route_hooks(registry,
                               {"engine": backend.name, "config": config,
                                "phase": "fix"},
                               "activate")
        else:
            print(f"{BOLD}[引擎运行中] 尝试热重载配置{NC}")

        # 热重载配置（跨平台，通过 Clash API）
        if backend.name == "mihomo":
            subprocess.run(
                ["curl", "-s", "--noproxy", "*", "-X", "PUT",
                 f"http://127.0.0.1:9090/configs?force=true",
                 "-H", f"Authorization: Bearer {api_secret}",
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps({"path": backend.config_file})],
                capture_output=True, timeout=8
            )
            print(f"  {GREEN}✓{NC} mihomo 配置已热重载")
            subprocess.run(
                ["curl", "-s", "--noproxy", "*", "-X", "PUT",
                 f"http://127.0.0.1:9090/cache/fakeip",
                 "-H", f"Authorization: Bearer {api_secret}"],
                capture_output=True, timeout=5
            )
            print(f"  {GREEN}✓{NC} mihomo DNS 缓存已清空")

        print()
        if IS_MACOS:
            if before == "127.0.0.1":
                print(f"{GREEN}{BOLD}一切正常，无需修复。{NC}")
            else:
                print(f"{GREEN}{BOLD}修复完成。{NC}")
                dns_lock_label = config.get("dns_lock_label", DEFAULTS["dns_lock_label"])
                if not launchctl_running(f"system/{dns_lock_label}"):
                    print(f"{CYAN}建议执行 {BOLD}proxyctl dns-lock{NC}"
                          f"{CYAN} 防止 DNS 再次被覆盖。{NC}")
        else:
            print(f"{GREEN}{BOLD}修复完成。{NC}")
    else:
        if IS_MACOS:
            print(f"{BOLD}[引擎已停止] 还原系统配置为正常状态{NC}")
            before = get_primary_resolver()

            dns_lock_label = config.get("dns_lock_label", DEFAULTS["dns_lock_label"])
            if launchctl_running(f"system/{dns_lock_label}"):
                dns_lock_stop(config)
                print(f"  {GREEN}✓{NC} dns-lock 看门狗已停止")

            dns_deactivate(config)
            after = get_primary_resolver()
            print(f"  {GREEN}✓{NC} DNS → DHCP ({after or 'DHCP'})")

            proxy_deactivate()
            print(f"  {GREEN}✓{NC} 系统代理已关闭")

            print()
            if before != "127.0.0.1":
                print(f"{GREEN}{BOLD}一切正常，无需修复。{NC}")
            else:
                print(f"{GREEN}{BOLD}还原完成。{NC}")
        else:
            print(f"{YELLOW}引擎已停止，无需修复。{NC}")
            print(f"  使用 {BOLD}proxyctl start{NC} 启动引擎")


# ── 命令：recover ─────────────────────────────────────────────────────────────

def cmd_recover(backend: Backend, config: dict):
    """切网后软恢复（清 DNS 缓存 + 重测代理组，不重启进程）"""
    api_base = config.get("api_base", DEFAULTS["api_base"])
    api_secret = config.get("api_secret", "")

    if backend.name != "mihomo":
        _io.fail(f"recover 目前只支持 mihomo 后端；当前后端 {backend.name}",
                 hint="proxyctl restart",
                 doc="engine", code=_io.USAGE, cmd="recover")

    if not launchctl_running(backend.label):
        _io.fail(f"{backend.name} 未运行",
                 hint="proxyctl start",
                 doc="engine",
                 code=_io.ENGINE_DOWN, cmd="recover",
                 as_json=GLOBAL_FLAGS.get("json", False))

    t0 = time.monotonic()
    auth = ["-H", f"Authorization: Bearer {api_secret}"]

    # 步骤 1: 热重载 config（清 DNS cache）
    print(f"{BOLD}[1/3]{NC} 热重载配置（清 DNS 缓存）")
    r = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--noproxy", "*", "-X", "PUT",
         f"{api_base}/configs?force=true",
         *auth, "-H", "Content-Type: application/json",
         "-d", json.dumps({"path": backend.config_file})],
        capture_output=True, text=True, timeout=10
    )
    if r.stdout.strip() in ("200", "204"):
        print(f"  {GREEN}✓{NC} 配置已重载 (HTTP {r.stdout.strip()})")
    else:
        _io.fail(
            f"重载失败 (HTTP {r.stdout.strip() or 'n/a'})",
            hint="proxyctl restart",
            doc="troubleshooting", code=_io.NETWORK_ERR, cmd="recover")

    # 步骤 2: flush fakeip cache
    print(f"{BOLD}[2/3]{NC} 清空 fakeip 缓存")
    subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "--noproxy", "*", "-X", "POST",
         f"{api_base}/cache/fakeip/flush", *auth],
        capture_output=True, timeout=5
    )
    print(f"  {GREEN}✓{NC} fakeip 缓存已清空")

    # 步骤 3: 触发代理组 healthcheck
    print(f"{BOLD}[3/3]{NC} 触发代理组 healthcheck")
    r = subprocess.run(
        ["curl", "-s", "--noproxy", "*", *auth, f"{api_base}/proxies"],
        capture_output=True, text=True, timeout=5
    )
    try:
        proxies = json.loads(r.stdout).get("proxies", {})
    except Exception:
        _io.fail("无法拉取 proxies 列表",
                 hint="proxyctl status / proxyctl log --tail 50",
                 doc="troubleshooting",
                 code=_io.NETWORK_ERR, cmd="recover")

    import urllib.parse
    TEST_URL = "https://www.gstatic.com/generate_204"
    TIMEOUT_MS = 5000
    groups_to_test = [
        name for name, info in proxies.items()
        if info.get("type") in ("URLTest", "Fallback", "LoadBalance")
    ]
    if not groups_to_test:
        print(f"  {YELLOW}—{NC} 无 url-test 类型组")
    else:
        from concurrent.futures import ThreadPoolExecutor
        encoded_url = urllib.parse.quote(TEST_URL, safe="")

        def _test_group(gname: str) -> tuple:
            encoded = urllib.parse.quote(gname, safe="")
            endpoint = (f"{api_base}/group/{encoded}/delay"
                        f"?url={encoded_url}&timeout={TIMEOUT_MS}")
            rr = subprocess.run(
                ["curl", "-s", "--noproxy", "*", "--max-time", "10",
                 *auth, endpoint],
                capture_output=True, text=True, timeout=12
            )
            try:
                data = json.loads(rr.stdout) if rr.stdout else {}
            except Exception:
                data = {}
            alive = sum(1 for v in data.values() if isinstance(v, int) and v > 0)
            total = len(data) if data else 0
            return gname, alive, total

        with ThreadPoolExecutor(max_workers=len(groups_to_test)) as pool:
            results = list(pool.map(_test_group, groups_to_test))

        for gname, alive, total in results:
            if total == 0:
                mark = f"{RED}✗{NC}"
                detail = "无响应"
            elif alive == 0:
                mark = f"{RED}✗{NC}"
                detail = f"0/{total} 存活"
            elif alive < total:
                mark = f"{YELLOW}—{NC}"
                detail = f"{alive}/{total} 存活"
            else:
                mark = f"{GREEN}✓{NC}"
                detail = f"{alive}/{total} 存活"
            print(f"  {mark} {CYAN}{gname}{NC}  {detail}")

    elapsed = time.monotonic() - t0
    print()
    print(f"{GREEN}{BOLD}recover 完成{NC}  耗时 {elapsed:.1f}s")
    print(f"建议运行 {BOLD}proxyctl check{NC} 验证恢复情况；若仍失败请 {BOLD}proxyctl restart{NC}")


# ── 命令：mode ────────────────────────────────────────────────────────────────

def cmd_mode(backend: Backend, target: str):
    if not target:
        cur = get_mode(backend)
        print(f"当前：backend={backend.name} mode={cur}")
        print("切换：proxyctl mode tun | proxyctl mode proxy")
        return

    if target not in ("tun", "proxy"):
        import difflib
        suggest = difflib.get_close_matches(target, ["tun", "proxy"], n=1, cutoff=0.4)
        hints = ["proxyctl mode tun     — 全局接管 (auto_route + fakeip)",
                 "proxyctl mode proxy   — 仅代理端口 + real DNS"]
        if suggest:
            hints.insert(0, f"是否想要：{suggest[0]}？")
        _io.fail(f"未识别 mode 目标：{target}",
                 hints=hints, doc="engine",
                 code=_io.USAGE, cmd="mode")

    if backend.name == "mihomo":
        _mode_mihomo(backend.config_file, target)
    else:
        _mode_singbox(backend.config_file, target)

    if target == "proxy":
        proxy_activate()
        print("系统代理 → 127.0.0.1:7890")
    else:
        proxy_deactivate()
        print("系统代理已关闭")


def _mode_mihomo(config_path: str, target: str):
    text = open(config_path).read()
    if target == "tun":
        text = re.sub(r'(tun:\s*\n(?:\s*#[^\n]*\n)*\s*enable:\s*)false', r'\1true', text)
        text = re.sub(r'(auto-route:\s*)false', r'\1true', text)
        text = re.sub(r'enhanced-mode:\s*redir-host', 'enhanced-mode: fake-ip', text)
        msg = "已切换到 tun 模式 (auto_route + fakeip)"
    else:
        text = re.sub(r'(tun:\s*\n(?:\s*#[^\n]*\n)*\s*enable:\s*)true', r'\1false', text)
        text = re.sub(r'(auto-route:\s*)true', r'\1false', text)
        text = re.sub(r'enhanced-mode:\s*fake-ip', 'enhanced-mode: redir-host', text)
        msg = "已切换到 proxy_only 模式 (7890 + redir-host)"
    open(config_path, "w").write(text)
    print(msg)
    print("执行 proxyctl restart 生效")


def _mode_singbox(config_path: str, target: str):
    cfg = json.load(open(config_path))
    for ib in cfg.get("inbounds", []):
        if ib.get("type") == "tun":
            ib["auto_route"] = (target == "tun")
            break
    for rule in cfg.get("dns", {}).get("rules", []):
        qt = rule.get("query_type", [])
        if "A" in qt and "AAAA" in qt:
            rule["server"] = "fakeip-dns" if target == "tun" else "proxy-dns"
            break
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if target == "tun":
        print("已切换到 tun 模式 (auto_route + fakeip)")
    else:
        print("已切换到 proxy_only 模式 (7890 + real DNS)")
    print("执行 proxyctl restart 生效")


# ── 命令：dns-lock / dns-unlock ───────────────────────────────────────────────

DNS_LOCK_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{watchdog_path}</string>
  </array>
  <key>StartInterval</key>
  <integer>30</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PROXYCTL_CONFIG_DIR</key>
    <string>{config_dir}</string>
    <key>PROXYCTL_API_BASE</key>
    <string>{api_base}</string>
    <key>PROXYCTL_API_SECRET</key>
    <string>{api_secret}</string>
    <key>PROXYCTL_ENGINE_LABEL</key>
    <string>{engine_label}</string>
    <key>PROXYCTL_CORP_DOMAIN</key>
    <string>{corp_domain}</string>
    <key>PROXYCTL_TUIC_HEALTHCHECK</key>
    <string>{tuic_healthcheck}</string>
  </dict>
  <key>StandardOutPath</key>
  <string>{log_path}</string>
  <key>StandardErrorPath</key>
  <string>{log_path}</string>
</dict>
</plist>
"""


def _render_dns_lock_plist(backend: Backend, config: dict) -> str:
    """根据 config 渲染 dns-lock plist 内容。

    所有路径/label/secret 由 config + 默认值组合得出；plist 内容不依赖任何
    仓库外文件（无须先复制模板到 ~/.config/proxyctl/launchdaemons/）。
    """
    dns_lock_label = config.get("dns_lock_label", DEFAULTS["dns_lock_label"])
    watchdog_path = os.path.join(HOME, ".local", "bin", "proxyctl-dns-watchdog")
    corp = (config.get("corp_dns") or {})
    healthcheck_off = (config.get("watchdog") or {}).get("tuic_healthcheck") is False

    return DNS_LOCK_PLIST_TEMPLATE.format(
        label           = dns_lock_label,
        watchdog_path   = watchdog_path,
        config_dir      = DEFAULT_CONFIG_DIR,
        api_base        = config.get("api_base", DEFAULTS["api_base"]),
        api_secret      = config.get("api_secret", ""),
        engine_label    = backend.label,
        corp_domain     = corp.get("domain", ""),
        tuic_healthcheck= "0" if healthcheck_off else "1",
        log_path        = os.path.join(DEFAULT_CONFIG_DIR, "dns-watchdog.log"),
    )


def cmd_engine(backend: Backend, target: str, config: dict):
    """proxyctl engine [singbox|mihomo] — 切换代理引擎。

    无参时打印当前引擎 + 已部署 plist 状态。
    切换时执行：停旧 daemon → 撤旧 plist → 装新 plist → 起新 daemon。
    引擎持久化到 ~/.config/proxyctl/engine 文件。
    """
    engine_file = os.path.join(DEFAULT_CONFIG_DIR, "engine")
    if not target:
        cur = backend.name
        sb_ok = os.path.isfile("/Library/LaunchDaemons/com.singbox.tun.plist")
        mh_ok = os.path.isfile("/Library/LaunchDaemons/com.mihomo.tun.plist")
        print(f"当前引擎: {cur}")
        print(f"已部署 plist: singbox={sb_ok} mihomo={mh_ok}")
        print("切换: proxyctl engine singbox | proxyctl engine mihomo")
        return

    if target not in ("singbox", "mihomo"):
        import difflib
        suggest = difflib.get_close_matches(target, ["singbox", "mihomo"],
                                            n=1, cutoff=0.4)
        hints = ["proxyctl engine singbox | proxyctl engine mihomo"]
        if suggest:
            hints.insert(0, f"是否想要：{suggest[0]}？")
        _io.fail(f"未识别 engine 目标：{target}",
                 hints=hints, doc="engine",
                 code=_io.USAGE, cmd="engine")
    if target == backend.name:
        print(f"已经是 {target}，无需切换")
        return
    if not IS_MACOS:
        _io.fail("engine 切换暂仅支持 macOS launchd",
                 doc="engine", code=_io.USAGE, cmd="engine")

    new_backend_cfg = dict(config)
    new_backend_cfg["backend"] = target
    new_backend = get_backend(new_backend_cfg)

    plist_src = os.path.join(DEFAULT_CONFIG_DIR, "launchdaemons",
                              os.path.basename(new_backend.plist))
    # 预检
    if not os.path.isfile(plist_src):
        _io.fail(f"plist 源文件不存在: {plist_src}",
                 hint="重新跑 install.sh 或检查 launchdaemons/ 目录",
                 doc="engine", code=_io.NOT_FOUND, cmd="engine")
    if not os.path.isfile(new_backend.config_file):
        _io.fail(f"配置文件不存在: {new_backend.config_file}",
                 hint="proxyctl explain config",
                 doc="config", code=_io.NOT_FOUND, cmd="engine")

    print(f"停止 {backend.name} ...")
    dns_lock_stop(config)
    dns_deactivate(config)
    proxy_deactivate()
    run(["launchctl", "bootout", backend.label], sudo=True)
    run(["/bin/rm", "-f", backend.plist], sudo=True)

    r = run(["/bin/cp", plist_src, new_backend.plist], sudo=True, capture=True)
    if r.returncode != 0:
        _io.fail("部署 plist 失败",
                 hints=[r.stderr.strip()] if r.stderr else None,
                 doc="engine", code=_io.PERMISSION, cmd="engine")

    # 持久化引擎选择
    os.makedirs(DEFAULT_CONFIG_DIR, exist_ok=True)
    with open(engine_file, "w") as f:
        f.write(target)

    print(f"启动 {new_backend.name} ...")
    r = run(["launchctl", "bootstrap", "system", new_backend.plist],
            sudo=True, capture=True)
    if r.returncode != 0:
        _io.fail("启动失败",
                 hints=[r.stderr.strip()] if r.stderr else None,
                 doc="troubleshooting",
                 code=_io.ENGINE_DOWN, cmd="engine")

    _wait_ready(new_backend)
    dns_activate(config)
    dns_lock_start(config)
    print("DNS → 127.0.0.1 (已激活)")
    if get_mode(new_backend) == "proxy":
        proxy_activate()
        print("系统代理 → 127.0.0.1:7890")
    print(f"{GREEN}引擎已切换到 {new_backend.name}{NC}")
    print(f"{CYAN}提示: 把 engine: {target} 写到 ~/.config/proxyctl/config.yaml 保持持久{NC}")


def cmd_daemon(name: str, subcmd: str, config: dict):
    """proxyctl daemon [name] [subcmd] — 管理 config.extra_daemons 中声明的辅助 daemon。

    config 示例：
      extra_daemons:
        my-secondary:
          label: com.example.my-secondary
          plist_src: /path/to/com.example.my-secondary.plist
          log_path:  /path/to/my-secondary.log
          port: 7891
    """
    if not IS_MACOS:
        print(f"{YELLOW}daemon 命令暂仅支持 macOS launchd{NC}")
        return

    daemons = (config.get("extra_daemons") or {})

    if not name:
        if not daemons:
            print(f"{YELLOW}—{NC} config.yaml 中未声明任何 extra_daemons")
            return
        print(f"{BOLD}已声明的 daemon:{NC}")
        for d_name, d_cfg in daemons.items():
            label = d_cfg.get("label", "?")
            running = launchctl_running(f"system/{label}", sudo=True)
            mark = f"{GREEN}✓{NC}" if running else f"{YELLOW}—{NC}"
            print(f"  {mark} {d_name}  label={label}")
        return

    d_cfg = daemons.get(name)
    if not d_cfg:
        declared = list(daemons.keys()) or ["(无)"]
        _io.fail(f"未声明的 daemon: {name}",
                 hint=f"已声明：{', '.join(declared)}；"
                      f"在 {CONFIG_FILE} 的 extra_daemons: 段加入 {name}",
                 doc="extra-daemons",
                 code=_io.NOT_FOUND, cmd="daemon",
                 as_json=GLOBAL_FLAGS.get("json", False))

    label    = d_cfg.get("label", "")
    plist_src = os.path.expanduser(d_cfg.get("plist_src", ""))
    log_path  = os.path.expanduser(d_cfg.get("log_path", ""))
    port      = d_cfg.get("port")
    if not label:
        _io.fail(f"daemon {name} 缺少 label 字段",
                 hint="检查 config.yaml 的 extra_daemons[<name>].label",
                 doc="extra-daemons", code=_io.CONFIG_ERR, cmd="daemon")

    full_label = f"system/{label}"
    plist_dst = f"/Library/LaunchDaemons/{label}.plist"

    subcmd = subcmd or "status"
    valid_subcmds = ("start", "stop", "restart", "log", "status")
    if subcmd not in valid_subcmds:
        import difflib
        suggest = difflib.get_close_matches(subcmd, valid_subcmds, n=1, cutoff=0.4)
        hints = [f"子命令: {', '.join(valid_subcmds)}"]
        if suggest:
            hints.insert(0, f"是否想要：{suggest[0]}？")
        _io.fail(f"未识别 daemon 子命令：{subcmd}",
                 hints=hints, doc="extra-daemons",
                 code=_io.USAGE, cmd="daemon")
    if subcmd == "start":
        if launchctl_running(full_label, sudo=True):
            print(f"{name} 已在运行")
            return
        if not os.path.isfile(plist_dst):
            if not os.path.isfile(plist_src):
                _io.fail(f"plist 源文件不存在: {plist_src}",
                         hint="检查 extra_daemons.plist_src 配置",
                         doc="extra-daemons",
                         code=_io.NOT_FOUND, cmd="daemon")
            run(["/bin/cp", plist_src, plist_dst], sudo=True)
        r = run(["launchctl", "bootstrap", "system", plist_dst],
                sudo=True, capture=True)
        if r.returncode != 0:
            _io.fail(f"{name} 启动失败",
                     hints=[r.stderr.strip()] if r.stderr else None,
                     doc="extra-daemons",
                     code=_io.PERMISSION, cmd="daemon")
        if port:
            wait_port(int(port), timeout=10)
            print(f"{GREEN}✓{NC} {name} started (127.0.0.1:{port})")
        else:
            print(f"{GREEN}✓{NC} {name} started")

    elif subcmd == "stop":
        if launchctl_running(full_label, sudo=True):
            run(["launchctl", "bootout", full_label], sudo=True)
            print(f"{name} stopped")
        else:
            print(f"{name} 未在运行")

    elif subcmd == "restart":
        run(["launchctl", "kickstart", "-k", full_label], sudo=True)
        print(f"{name} restarted")

    elif subcmd == "log":
        if not log_path:
            _io.fail(f"daemon {name} 未声明 log_path",
                     hint="在 config.yaml 的 extra_daemons[<name>] 加 log_path 字段",
                     doc="extra-daemons",
                     code=_io.CONFIG_ERR, cmd="daemon")
        os.execvp("tail", ["tail", "-f", log_path])

    else:  # status
        if launchctl_running(full_label, sudo=True):
            r = subprocess.run(["sudo", "launchctl", "print", full_label],
                               capture_output=True, text=True)
            pid = next((l.split()[-1] for l in r.stdout.splitlines()
                        if "pid =" in l), "?")
            port_str = f", port {port}" if port else ""
            print(f"{GREEN}✓{NC} {name} running (PID {pid}{port_str})")
        else:
            print(f"{RED}✗{NC} {name} not running")


def cmd_dns_lock(config: dict, backend: Backend, *, reload: bool = False):
    if not IS_MACOS:
        print(f"{YELLOW}dns-lock 仅支持 macOS（Linux 不劫持系统 DNS）{NC}")
        return
    dns_lock_label = config.get("dns_lock_label", DEFAULTS["dns_lock_label"])
    dns_lock_plist = f"/Library/LaunchDaemons/{dns_lock_label}.plist"
    dns_watchdog = os.path.join(HOME, ".local", "bin", "proxyctl-dns-watchdog")

    full_label = f"system/{dns_lock_label}"
    already_registered = launchctl_running(full_label)

    # 默认行为：如果已 registered 且 plist 已存在，认为已装好
    # reload=True：强制 bootout + 重写 plist + 重新 bootstrap
    if already_registered and not reload:
        print(f"dns-lock daemon 已注册（如需更新 plist，请运行 proxyctl dns-lock --reload）")
        return

    if not os.access(dns_watchdog, os.X_OK):
        _io.fail(f"看门狗脚本不可执行 {dns_watchdog}",
                 hint="把 scripts/dns-watchdog 安装到该位置（或重新跑 install.sh）",
                 doc="dns", code=_io.DEPENDENCY_MISSING, cmd="dns-lock")

    # reload 时先 bootout
    if already_registered:
        r0 = run(["launchctl", "bootout", full_label], sudo=True, capture=True)
        if r0.returncode != 0:
            print(f"{YELLOW}⚠{NC} bootout 失败（继续尝试 bootstrap）: {r0.stderr.strip()}")

    rendered = _render_dns_lock_plist(backend, config)
    # 通过 sudo tee 写入（sudoers 允许 /usr/bin/tee <target.plist>）
    r = run(["tee", dns_lock_plist], sudo=True, stdin_text=rendered, capture=True)
    if r.returncode != 0:
        _io.fail(
            f"写入 plist 失败: {r.stderr or '权限不足，请检查 sudoers'}",
            hint="确认 sudoers 允许 /usr/bin/tee 写 LaunchDaemons",
            doc="dns", code=_io.PERMISSION, cmd="dns-lock")

    r2 = run(["launchctl", "bootstrap", "system", dns_lock_plist],
             sudo=True, capture=True)
    if r2.returncode != 0:
        _io.fail(f"bootstrap 失败: {r2.stderr}",
                 doc="dns", code=_io.PERMISSION, cmd="dns-lock")

    print(f"{GREEN}dns-lock daemon 已安装并启动{NC}")
    print(f"label: {dns_lock_label}")
    print(f"plist: {dns_lock_plist}  (内嵌模板渲染，含 config.yaml 注入的 env)")
    print(f"日志:  {os.path.join(DEFAULT_CONFIG_DIR, 'dns-watchdog.log')}")


def cmd_dns_unlock(config: dict):
    if not IS_MACOS:
        print(f"{YELLOW}dns-unlock 仅支持 macOS{NC}")
        return
    dns_lock_label = config.get("dns_lock_label", DEFAULTS["dns_lock_label"])

    r = run(["launchctl", "bootout", f"system/{dns_lock_label}"], sudo=True, capture=True)
    if r.returncode == 0:
        print(f"{GREEN}dns-lock daemon 已停止{NC}")
    else:
        print("dns-lock daemon 未在运行")
    run(["rm", "-f", dns_lock_plist], sudo=True)
    print(f"已删除 {dns_lock_plist} (源文件保留在 {DEFAULT_CONFIG_DIR}/launchdaemons/)")


# ── 命令：env ────────────────────────────────────────────────────────────────

def cmd_env(config: dict, unset: bool = False):
    """输出设置/清除代理环境变量的 shell 语句。

    用法：
        eval $(proxyctl env)         # 设置代理
        eval $(proxyctl env --unset) # 清除代理

    Args:
        config: 全局配置字典
        unset: True 则输出 unset 语句
    """
    if unset:
        for var in ("http_proxy", "https_proxy", "all_proxy",
                     "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                     "no_proxy", "NO_PROXY"):
            print(f"unset {var};")
        return

    port = int(config.get("proxy_port", DEFAULTS["proxy_port"]))  # mixed-port
    proxy_http = f"http://127.0.0.1:{port}"
    proxy_socks = f"socks5://127.0.0.1:{port}"
    no_proxy = "localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    # 用户附加的 NO_PROXY 项（个人域名等）；接受 list[str] 或逗号分隔 str
    extra = config.get("no_proxy_extra") or []
    if isinstance(extra, str):
        extra = [s.strip() for s in extra.split(",") if s.strip()]
    if extra:
        no_proxy = no_proxy + "," + ",".join(extra)

    for var in ("http_proxy", "HTTP_PROXY"):
        print(f"export {var}={proxy_http};")
    for var in ("https_proxy", "HTTPS_PROXY"):
        print(f"export {var}={proxy_http};")
    for var in ("all_proxy", "ALL_PROXY"):
        print(f"export {var}={proxy_socks};")
    for var in ("no_proxy", "NO_PROXY"):
        print(f"export {var}={no_proxy};")


# ── 命令：log ─────────────────────────────────────────────────────────────────

def cmd_log(backend: "Backend", args: list) -> None:
    """proxyctl log [--tail N] [--no-follow] [--json]

    无参 → tail -f（向后兼容）。
    --tail N / --no-follow → 立即返回（agent 友好，不挂死）。
    --json + --tail N / --no-follow → 输出 JSON Lines（每行 {ts, line, file}）。
    """
    log_file = backend.log_file
    if not os.path.isfile(log_file):
        _io.fail(f"日志文件不存在：{log_file}",
                 hint=f"先 proxyctl start 让 {backend.name} 生成日志",
                 code=_io.NOT_FOUND, cmd="log",
                 as_json=GLOBAL_FLAGS.get("json", False))
        return

    # 用 _io.extract_flags 让 flag 位置无关（无论 --tail / --no-follow 出现在哪都识别）
    _positional, flags = _io.extract_flags(
        args, known={"--tail": "value", "--no-follow": "bool"})
    follow = True
    tail_n: int | None = None
    if flags.get("no_follow"):
        follow = False
    if "tail" in flags:
        tval = flags["tail"]
        if tval is None:
            _io.fail("--tail 需要一个数字参数",
                     code=_io.USAGE, cmd="log",
                     as_json=GLOBAL_FLAGS.get("json", False))
        try:
            tail_n = int(tval)
        except ValueError:
            _io.fail(f"--tail 参数不是数字：{tval}",
                     code=_io.USAGE, cmd="log",
                     as_json=GLOBAL_FLAGS.get("json", False))
        follow = False

    as_json = GLOBAL_FLAGS.get("json", False)

    if as_json:
        # NDJSON v2 规范化（W19）：
        #   首行：meta header { schema_version, cmd, stream, path }
        #   每后续行：一条事件 { source, line }（line 已 strip 换行）
        print(json.dumps({
            "schema_version": _io.SCHEMA_VERSION,
            "cmd": "log",
            "stream": "log",
            "path": log_file,
        }, ensure_ascii=False))
        lines = _read_log_lines(log_file, tail_n)
        for line in lines:
            print(json.dumps(
                {"source": log_file, "line": line.rstrip()},
                ensure_ascii=False))
        return

    if follow:
        os.execvp("tail", ["tail", "-f", log_file])  # 向后兼容：默认行为不变

    if tail_n is not None:
        os.execvp("tail", ["tail", "-n", str(tail_n), log_file])
    # --no-follow 且没指定 --tail：cat 全文
    os.execvp("cat", ["cat", log_file])


def _read_log_lines(path: str, tail_n: int | None) -> list:
    """读日志为行列表；tail_n 为 None 表示读全文。"""
    try:
        with open(path, errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        return []
    if tail_n is None:
        return all_lines
    return all_lines[-tail_n:] if tail_n > 0 else []


# ── 帮助 ──────────────────────────────────────────────────────────────────────

def _read_version() -> str:
    """单一事实来源：pyproject.toml 的 [project] version。"""
    try:
        from importlib.metadata import version
        return version("proxyctl")
    except Exception:
        return "unknown"


VERSION = _read_version()


# Help 输出按 group 分块的顺序（避免依赖 dict 插入顺序变化）
_HELP_GROUP_ORDER = ["lifecycle", "diagnostic", "config", "maintenance",
                     "daemon", "tool", "agent"]


def _side_effects_badge(se) -> str:
    """把 COMMANDS_META 的 side_effects 字段渲染为 badge 文本。

    兼容 str（v0.2 旧值）与 list[str]（v0.3 枚举形态）。
    """
    if not se or se == "none":
        return ""
    if isinstance(se, list):
        return "+".join(se) if se else ""
    return str(se)


def cmd_help():
    """proxyctl --help / help：元数据驱动的顶层帮助。

    输出区块（按 clig.dev / Agent 友好原则）：
      1. version + tagline
      2. AGENT 接入小 box（4 条入口）
      3. 用法行
      4. 按 group 分组的命令清单（从 COMMANDS_META 派生）
      5. 全局 flag
      6. 环境变量
      7. 配置文件 / 仓库地址 / 单命令说明
    """
    from proxyctl.explain import COMMANDS_META

    print(f"proxyctl v{VERSION}")
    print("Proxy configuration lifecycle management\n")

    # AGENT 接入小 box（醒目，agent 第一眼能看到）
    print(f"{BOLD}AGENT 接入{NC}")
    print(f"  {CYAN}proxyctl agent-guide{NC}          "
          f"{DIM}Agent 入门 markdown（喂给 LLM）{NC}")
    print(f"  {CYAN}proxyctl commands --json{NC}      "
          f"{DIM}全部命令元数据（机读）{NC}")
    print(f"  {CYAN}proxyctl explain{NC}              "
          f"{DIM}我要改 X 去哪？速查表{NC}")
    print(f"  {CYAN}PROXYCTL_AGENT=1 proxyctl ...{NC}  "
          f"{DIM}一键 JSON + 关色 + 非交互{NC}")
    print()

    print(f"{BOLD}用法{NC}  proxyctl <command> [args] "
          f"[--json|--plain] [--dry-run] [--no-color] [--quiet]")
    print()

    # 按 group 元数据驱动渲染
    by_group: dict[str, list] = {}
    for c in COMMANDS_META:
        by_group.setdefault(c["group"], []).append(c)
    for g in _HELP_GROUP_ORDER:
        items = by_group.get(g)
        if not items:
            continue
        print(f"{BOLD}{g}{NC}")
        for c in items:
            badges: list[str] = []
            if c.get("needs_sudo"):
                badges.append("sudo")
            if c.get("supports_json"):
                badges.append("--json")
            if c.get("supports_dry_run"):
                badges.append("--dry-run")
            se = _side_effects_badge(c.get("side_effects"))
            if se:
                badges.append(se)
            badge_str = f"  {DIM}[{' / '.join(badges)}]{NC}" if badges else ""
            print(f"  {CYAN}{c['name']:<16}{NC} {c['summary']}{badge_str}")
        print()

    # 全局 flag
    print(f"{BOLD}全局 flag{NC}")
    print(f"  --json         输出 envelope JSON（schema v2）")
    print(f"  --plain        输出纯 TSV（audit/check 等支持表格的命令）")
    print(f"  --dry-run      预演写操作（输出 plan，不真正执行）")
    print(f"  --no-color     关闭 ANSI（也读 NO_COLOR）")
    print(f"  --quiet/-q     压制非关键 stderr")
    print(f"  --help/-h      本帮助")
    print(f"  --version/-v   版本号（可加 --json）")
    print()

    # 环境变量
    print(f"{BOLD}环境变量{NC}")
    print(f"  PROXYCTL_AGENT=1     等价 --json + --no-color + 非交互承诺")
    print(f"  PROXYCTL_DEBUG=1     打印插件加载日志到 stderr")
    print(f"  PROXYCTL_NO_COLOR    等价 --no-color")
    print(f"  NO_COLOR             遵循 no-color.org")
    print()

    print(f"配置文件：~/.config/proxyctl/config.yaml")
    print(f"仓库地址：https://github.com/crhan/proxyctl")
    print(f"单命令说明：{CYAN}proxyctl help <command>{NC}  "
          f"{DIM}（或 proxyctl <command> --help）{NC}")


def cmd_discovery(backend, config) -> None:
    """proxyctl 无参输出：JSON 模式 = discovery envelope；人类模式 = stderr banner（不退出，由 caller 决定后续 status 行为）。

    设计意图：
      - JSON / PROXYCTL_AGENT=1 → 输出 discovery envelope 并 sys.exit(0)，让
        agent 一次 round-trip 拿到能力清单 + 引擎一行状态，避免吐 status 全量。
      - 人类模式 → 仅把 banner 写 stderr（不污染 stdout），让 stdout 继续走
        默认 status 命令（保留 0.2 体验）。
    """
    port = config.get("proxy_port", 7890)
    try:
        engine_up = launchctl_running(backend.label) if IS_MACOS else False
    except Exception:
        engine_up = False

    if GLOBAL_FLAGS.get("json"):
        data = {
            "version": VERSION,
            "schema_version": _io.SCHEMA_VERSION,
            "engine": {
                "name": backend.name,
                "running": engine_up,
                "port": port,
            },
            "entrypoints": {
                "agent_guide":  "proxyctl agent-guide",
                "commands":     "proxyctl commands --json",
                "commands_schema": "proxyctl commands --schema",
                "explain":      "proxyctl explain",
                "doctor":       "proxyctl doctor --json",
                "help":         "proxyctl --help",
                "version":      "proxyctl --version --json",
            },
            "hints_for_agent": [
                "Run 'proxyctl agent-guide' first if you are an LLM agent.",
                "Set PROXYCTL_AGENT=1 to force --json + no-color + non-interactive.",
                "All write commands accept --dry-run.",
            ],
        }
        _io.emit_json(_io.envelope("", data=data, doc="agent"))
        sys.exit(0)

    # 人类：banner 到 stderr（不退出，交给 caller）
    mark = f"{GREEN}✓{NC}" if engine_up else f"{RED}✗{NC}"
    print(f"{BOLD}proxyctl v{VERSION}{NC}   "
          f"{mark} engine={backend.name} port={port}",
          file=sys.stderr)
    print(file=sys.stderr)
    print(f"{DIM}下一步：{NC}", file=sys.stderr)
    print(f"  {CYAN}proxyctl agent-guide{NC}    "
          f"{DIM}Agent 入门（LLM 必读）{NC}", file=sys.stderr)
    print(f"  {CYAN}proxyctl explain{NC}        "
          f"{DIM}我要改 X 去哪？{NC}", file=sys.stderr)
    print(f"  {CYAN}proxyctl doctor{NC}         "
          f"{DIM}5 项健康打分（最快）{NC}", file=sys.stderr)
    print(f"  {CYAN}proxyctl help <cmd>{NC}     "
          f"{DIM}单命令说明{NC}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"{DIM}（设 PROXYCTL_AGENT=1 等价 --json + 关色 + 非交互；"
          f"下面是默认 status 输出）{NC}", file=sys.stderr)


def cmd_version_print() -> None:
    """proxyctl --version：人类一行；--json 输出 envelope.data 含 supported_features。"""
    if not GLOBAL_FLAGS.get("json"):
        print(f"proxyctl v{VERSION}")
    else:
        import platform as _plat
        try:
            backend_name = "mihomo"  # default; 不加载 config，避免依赖磁盘
        except Exception:
            backend_name = "unknown"
        # supported_features：每个 0.3.0 工作项落地时翻 true；agent 用它探测
        # "该 release 是否支持某能力"。schema 字段名称稳定不删。
        data = {
            "version": VERSION,
            "schema_version": _io.SCHEMA_VERSION,
            "python": _plat.python_version(),
            "platform": _plat.system().lower(),
            "default_backend": backend_name,
            "supported_features": {
                "envelope_v2":             True,
                "agent_guide":             True,
                "commands_json":           True,
                "explain":                 True,
                "version_json":            True,
                "discovery_envelope":      True,
                "help_subcommand":         True,
                "exit_codes_extended":     True,
                "did_you_mean":            True,
                "lock_path_in_error":      True,
                "side_effects_enum":       True,
                "dry_run":                 True,
                "plain":                   True,
                "flag_position_invariant": True,
                "agents_md":               True,
                "commands_schema":         True,
                "doctor_extended":         True,
                "log_ndjson_v2":           True,
            },
        }
        _io.emit_json(_io.envelope("version", data=data))
    sys.exit(0)


def cmd_subcommand_help(name: str) -> None:
    """proxyctl <name> --help — 从 COMMANDS_META 派生子命令帮助文本。"""
    from proxyctl.explain import COMMANDS_META, TOPICS
    meta = next((c for c in COMMANDS_META if c["name"] == name), None)
    if meta is None:
        _io.fail(f"未识别子命令：{name}",
                 hint="proxyctl commands  # 列出所有子命令",
                 code=_io.USAGE, cmd=name,
                 as_json=GLOBAL_FLAGS.get("json", False))

    if GLOBAL_FLAGS.get("json"):
        _io.emit_json(_io.envelope(name, data=meta))
        return

    print(f"{BOLD}proxyctl {meta['name']}{NC}  ({meta['group']})")
    print(f"  {meta['summary']}\n")

    # 用法行
    args_parts = []
    for a in meta.get("args", []):
        spec = a["name"]
        if a.get("choices"):
            spec = "|".join(a["choices"])
        if a.get("variadic"):
            spec = f"{spec}..."
        args_parts.append(f"<{spec}>" if a.get("required") else f"[{spec}]")
    flags_part = " [--json]" if meta.get("supports_json") else ""
    print(f"{BOLD}用法{NC}     proxyctl {meta['name']}"
          f"{' ' + ' '.join(args_parts) if args_parts else ''}{flags_part}")

    if meta.get("examples"):
        print(f"\n{BOLD}示例{NC}")
        for ex in meta["examples"]:
            print(f"  {CYAN}{ex}{NC}")

    if meta.get("exit_codes"):
        codes_str = ", ".join(
            f"{c}({_io.EXIT_CODE_HELP.get(c, '?').split('（')[0].strip()})"
            for c in meta["exit_codes"]
        )
        print(f"\n{BOLD}退出码{NC}   {codes_str}")

    badges = []
    if meta.get("needs_sudo"):       badges.append("需要 sudo")
    if meta.get("interactive"):       badges.append("交互式")
    se = meta.get("side_effects")
    if se:
        if isinstance(se, list):
            badges.append(f"副作用={'+'.join(se)}")
        elif se != "none":
            badges.append(f"副作用={se}")
    cse = meta.get("conditional_side_effects") or {}
    for trigger, effects in cse.items():
        badges.append(f"副作用[{trigger}]={'+'.join(effects)}")
    if meta.get("supports_dry_run"):
        badges.append("支持 --dry-run")
    if not meta.get("supports_json"):
        badges.append("无 --json")
    if badges:
        print(f"\n{DIM}({' / '.join(badges)}){NC}")

    if name in TOPICS:
        print(f"\n{DIM}详细说明：proxyctl explain {name}{NC}")


# ── 全局 flag 预解析 ──────────────────────────────────────────────────────

# 入口运行期填充，子命令可读取（如 status 决定是否 --json）
GLOBAL_FLAGS: dict = {
    "json": False, "no_color": False, "quiet": False,
    "dry_run": False, "plain": False,
}


def _extract_global_flags(argv: list) -> tuple:
    """从 argv 中剥离全局 flag，返回 (剩余 argv, flag dict)。

    clig.dev 原则：flag 位置无关 — 在任意位置出现的 --json / --no-color /
    --quiet / -q / --dry-run / --plain 都会被识别并剥离，剩余位置参数顺序保持不变。
    """
    flags = {
        "json": False, "no_color": False, "quiet": False,
        "dry_run": False, "plain": False,
    }
    remaining = []
    for a in argv:
        if a == "--json":
            flags["json"] = True
        elif a == "--no-color":
            flags["no_color"] = True
        elif a in ("--quiet", "-q"):
            flags["quiet"] = True
        elif a == "--dry-run":
            flags["dry_run"] = True
        elif a == "--plain":
            flags["plain"] = True
        else:
            remaining.append(a)
    return remaining, flags


def _maybe_dry_run(cmd_name: str, plan_fn) -> None:
    """如果 --dry-run，调用 plan_fn() 拿 plan 列表，emit 并 sys.exit(0)。

    plan_fn 是一个无参可调用，返回 list[PlanStep]，每个 step 含：
        step / action / target / reversible / requires_sudo / side_effects / summary
    """
    if not GLOBAL_FLAGS.get("dry_run"):
        return
    plan = list(plan_fn())
    # 兜底：补 step 序号、补默认字段
    for i, s in enumerate(plan, start=1):
        s.setdefault("step", i)
        s.setdefault("reversible", False)
        s.setdefault("requires_sudo", False)
        s.setdefault("side_effects", [])
    if GLOBAL_FLAGS.get("json"):
        _io.emit_json(_io.envelope(
            cmd_name,
            data={"plan": plan, "dry_run": True},
            hints=[
                f"去掉 --dry-run 即可真正执行 {cmd_name}",
                "plan schema 见 proxyctl explain agent-protocol",
            ]))
    else:
        print(f"{BOLD}[--dry-run]{NC} 将执行以下步骤（不真正执行）：")
        for s in plan:
            tags = []
            if s.get("requires_sudo"):
                tags.append("sudo")
            if s.get("side_effects"):
                tags.append("+".join(s["side_effects"]))
            tag = f"  {DIM}[{' / '.join(tags)}]{NC}" if tags else ""
            print(f"  {CYAN}{s['step']}.{NC} {s['summary']}{tag}")
        print(f"\n{DIM}去掉 --dry-run 即可真正执行 {cmd_name}{NC}")
    sys.exit(0)


def _plan_mode(backend, target: str) -> list[dict]:
    return [
        {"action": "edit_yaml",
         "target": backend.config_file,
         "summary": f"修改 {backend.config_file}：切换 mode 为 {target}",
         "reversible": True,
         "side_effects": ["config-write"]},
        {"action": "subprocess",
         "target": f"launchctl kickstart -k system/{backend.label}",
         "summary": f"重启 launchd 服务以读取新 mode",
         "reversible": True, "requires_sudo": True,
         "side_effects": ["process"]},
    ]


def _plan_engine(backend, target: str) -> list[dict]:
    from proxyctl.cli import get_backend, DEFAULT_CONFIG_DIR
    new_cfg = {"backend": target}
    try:
        new_backend = get_backend(new_cfg)
        new_plist = new_backend.plist
    except Exception:
        new_plist = f"/Library/LaunchDaemons/<{target}>.plist"
    return [
        {"action": "subprocess",
         "target": f"launchctl bootout system/{backend.label}",
         "summary": f"停止当前引擎 {backend.name}",
         "reversible": True, "requires_sudo": True,
         "side_effects": ["process"]},
        {"action": "fs_write",
         "target": new_plist,
         "summary": f"部署新引擎 plist 到 {new_plist}",
         "reversible": True, "requires_sudo": True,
         "side_effects": ["config-write"]},
        {"action": "fs_write",
         "target": f"{DEFAULT_CONFIG_DIR}/engine",
         "summary": f"持久化引擎选择到 {DEFAULT_CONFIG_DIR}/engine = {target}",
         "reversible": True,
         "side_effects": ["config-write"]},
        {"action": "subprocess",
         "target": f"launchctl bootstrap system {new_plist}",
         "summary": f"启动新引擎 {target}",
         "reversible": True, "requires_sudo": True,
         "side_effects": ["process"]},
    ]


def _plan_fix(backend, config) -> list[dict]:
    return [
        {"action": "subprocess",
         "target": "networksetup -setdnsservers <svc> 127.0.0.1",
         "summary": "重置系统 DNS 指向 127.0.0.1（对抗 DHCP 续租）",
         "reversible": True, "requires_sudo": True,
         "side_effects": ["system"]},
        {"action": "subprocess",
         "target": "scutil + dscacheutil",
         "summary": "清 macOS DNS 缓存（含 fakeip 表）",
         "reversible": True, "requires_sudo": True,
         "side_effects": ["cache"]},
        {"action": "http_put",
         "target": f"{config.get('api_base', 'http://127.0.0.1:9090')}/configs?force=true",
         "summary": "向 Clash API 发热重载请求（不重启进程）",
         "reversible": True,
         "side_effects": ["network-io"]},
    ]


def _plan_audit_apply(days: int) -> list[dict]:
    return [
        {"action": "scan_log",
         "target": "<engine log>",
         "summary": f"扫描最近 {days} 天后端日志，找疑似应直连的域名",
         "reversible": True,
         "side_effects": []},
        {"action": "edit_yaml",
         "target": "<engine config>.yaml [rules: 段]",
         "summary": "把候选域名作为 DOMAIN-SUFFIX,...,DIRECT 加到 rules 段顶部",
         "reversible": True,
         "side_effects": ["config-write"]},
        {"action": "http_put",
         "target": "Clash API /configs?force=true",
         "summary": "热重载配置使新规则生效",
         "reversible": True,
         "side_effects": ["network-io"]},
    ]


def _plan_config_set(path: str, key: str, value_repr: str) -> list[dict]:
    return [
        {"action": "fs_copy",
         "target": f"{path} → {path}.bak",
         "summary": "拷贝当前配置到 .bak 备份",
         "reversible": True,
         "side_effects": ["config-write"]},
        {"action": "fs_write_atomic",
         "target": path,
         "summary": f"原子写入：{key} = {value_repr}（tmp + rename + YAML 校验）",
         "reversible": True,
         "side_effects": ["config-write"]},
    ]


def _plan_daemon(name: str, subcmd: str, plist_dst: str) -> list[dict]:
    if subcmd == "start":
        return [
            {"action": "fs_write",
             "target": plist_dst,
             "summary": f"如缺失则部署 plist 到 {plist_dst}",
             "reversible": True, "requires_sudo": True,
             "side_effects": ["config-write"]},
            {"action": "subprocess",
             "target": f"launchctl bootstrap system {plist_dst}",
             "summary": f"启动 daemon {name}",
             "reversible": True, "requires_sudo": True,
             "side_effects": ["process"]},
        ]
    if subcmd == "stop":
        return [
            {"action": "subprocess",
             "target": f"launchctl bootout system/<{name}.label>",
             "summary": f"停止 daemon {name}",
             "reversible": True, "requires_sudo": True,
             "side_effects": ["process"]},
        ]
    if subcmd == "restart":
        return [
            {"action": "subprocess",
             "target": f"launchctl kickstart -k system/<{name}.label>",
             "summary": f"重启 daemon {name}",
             "reversible": True, "requires_sudo": True,
             "side_effects": ["process"]},
        ]
    return []


def _plan_dns_lock(reload: bool) -> list[dict]:
    plan: list[dict] = []
    if reload:
        plan.append({
            "action": "subprocess",
            "target": "launchctl bootout system/<dns-lock.label>",
            "summary": "如已注册，先 bootout 重装",
            "reversible": True, "requires_sudo": True,
            "side_effects": ["process"]})
    plan += [
        {"action": "fs_write",
         "target": "/Library/LaunchDaemons/<dns-lock>.plist",
         "summary": "渲染并写入 dns-lock launchd plist",
         "reversible": True, "requires_sudo": True,
         "side_effects": ["config-write"]},
        {"action": "subprocess",
         "target": "launchctl bootstrap system <plist>",
         "summary": "启动 DNS 看门狗 daemon",
         "reversible": True, "requires_sudo": True,
         "side_effects": ["process"]},
    ]
    return plan


def _plan_dns_unlock() -> list[dict]:
    return [
        {"action": "subprocess",
         "target": "launchctl bootout system/<dns-lock.label>",
         "summary": "停止 DNS 看门狗",
         "reversible": True, "requires_sudo": True,
         "side_effects": ["process"]},
        {"action": "fs_remove",
         "target": "/Library/LaunchDaemons/<dns-lock>.plist",
         "summary": "删除 launchd plist（可选；保留下次 dns-lock 直接启动）",
         "reversible": False, "requires_sudo": True,
         "side_effects": ["config-write"]},
    ]


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    # 信号：避免 `proxyctl ... | head` 的 BrokenPipeError；Ctrl-C → exit 130
    _io.install_signal_handlers()

    # 记录本次调用的 t0（envelope.meta.elapsed_ms 用）和 request_id
    _io.set_invocation_t0()
    _io.new_request_id()

    # 全局 flag：从 sys.argv 中剥离（位置无关），子命令分发不变
    new_argv, gflags = _extract_global_flags(sys.argv)
    sys.argv = new_argv
    GLOBAL_FLAGS.update(gflags)

    # PROXYCTL_AGENT=1 一键模式：等价 --json + --no-color
    if _io.agent_mode_active():
        GLOBAL_FLAGS["json"] = True
        GLOBAL_FLAGS["no_color"] = True

    # 把 json 模式同步到 _io 全局（让 _io.fail 等子模块无需手工传 as_json）
    _io.set_json_mode(GLOBAL_FLAGS["json"])

    # 关色决策：显式 --no-color / --json / PROXYCTL_AGENT 强制；否则按 TTY+env 自动
    if GLOBAL_FLAGS["no_color"] or GLOBAL_FLAGS["json"]:
        _io.set_no_color(True)
    else:
        _io.auto_color_init()

    # 处理全局帮助 / 版本（位置仍要求是第一个非全局参数）
    if len(sys.argv) > 1:
        if sys.argv[1] in ("--help", "-h"):
            cmd_help()
            return
        elif sys.argv[1] == "help":
            # `proxyctl help`        → 顶层 --help
            # `proxyctl help <cmd>`  → 单命令 help（与 `<cmd> --help` 同源）
            if len(sys.argv) >= 3:
                cmd_subcommand_help(sys.argv[2])
                return
            cmd_help()
            return
        elif sys.argv[1] in ("--version", "-v"):
            cmd_version_print()
            return

    # 子命令 --help / -h（位置：sys.argv[2:] 中任一处出现）
    if len(sys.argv) > 2 and any(a in ("--help", "-h") for a in sys.argv[2:]):
        cmd_subcommand_help(sys.argv[1])
        return

    config = load_config()
    backend = get_backend(config)
    registry = load_plugins(config)
    api_base = config.get("api_base", DEFAULTS["api_base"])
    api_secret = config.get("api_secret", "")

    # 检查 api_secret 配置
    if not api_secret:
        print(f"{YELLOW}警告：未在配置文件中找到 api_secret{NC}", file=sys.stderr)
        print(f"  请在 {CONFIG_FILE} 中配置 api_secret: <your-clash-api-secret>",
              file=sys.stderr)
        print(file=sys.stderr)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    # 把全局 flag 共享给 explain（status/doctor 等子命令通过它读 --json）
    from proxyctl import explain as _ex_share
    _ex_share.set_global_flags(GLOBAL_FLAGS)

    # 裸 proxyctl → 输出 discovery 信号：
    #   - JSON 模式：discovery envelope（sys.exit(0)，不再走 status）
    #   - 人类模式：stderr banner + 继续走默认 status
    if len(sys.argv) == 1:
        cmd_discovery(backend, config)
        # 人类模式下 cmd_discovery 不退出，继续走 status

    ctx = {
        "backend": backend, "config": config, "registry": registry,
        "api_base": api_base, "api_secret": api_secret,
        "args": sys.argv[2:],
    }
    handler = DISPATCH.get(cmd)
    if handler is None:
        _suggest_command_and_exit(cmd)
    handler(ctx)


# ── Dispatch handlers + 路由表 ────────────────────────────────────────────

def _h_start(ctx):    cmd_start(ctx["backend"], ctx["config"], registry=ctx["registry"])
def _h_stop(ctx):     cmd_stop(ctx["backend"], ctx["config"], registry=ctx["registry"])
def _h_restart(ctx):  cmd_restart(ctx["backend"], ctx["config"], registry=ctx["registry"])
def _h_restart_clean(ctx):
    cmd_restart(ctx["backend"], ctx["config"], clean=True, registry=ctx["registry"])
def _h_recover(ctx):  cmd_recover(ctx["backend"], ctx["config"])
def _h_dns_unlock(ctx):
    _maybe_dry_run("dns-unlock", lambda: _plan_dns_unlock())
    _exec_with_lock("daemon", "dns-unlock", cmd_dns_unlock, ctx["config"])
def _h_plugins(ctx):  cmd_plugins(ctx["registry"])

def _h_status(ctx):
    from proxyctl.status import cmd_status
    mode_str = get_mode(ctx["backend"])
    cmd_status(ctx["backend"], ctx["api_base"], ctx["api_secret"],
               ctx["config"], mode_str, registry=ctx["registry"])

def _h_log(ctx):
    cmd_log(ctx["backend"], ctx["args"])

def _h_check(ctx):
    from proxyctl.check import cmd_check
    mode_str = get_mode(ctx["backend"])
    cmd_check(ctx["backend"], ctx["api_base"], ctx["api_secret"],
              ctx["config"], mode_str, registry=ctx["registry"])

def _h_bench(ctx):
    from proxyctl.check import cmd_bench
    bench_groups = ctx["args"] or None
    default_groups = ctx["registry"].collect("check_groups") if ctx["registry"] else None
    cmd_bench(ctx["api_base"], ctx["api_secret"], bench_groups,
              default_groups=default_groups)

def _h_fix(ctx):
    _maybe_dry_run("fix", lambda: _plan_fix(ctx["backend"], ctx["config"]))
    _exec_with_lock("system", "fix", cmd_fix,
                    ctx["backend"], ctx["config"], registry=ctx["registry"])

def _h_dns_lock(ctx):
    reload = "--reload" in ctx["args"]
    _maybe_dry_run("dns-lock", lambda: _plan_dns_lock(reload))
    _exec_with_lock("daemon", "dns-lock", cmd_dns_lock,
                    ctx["config"], ctx["backend"], reload=reload)

def _h_env(ctx):
    unset = "--unset" in ctx["args"] or "off" in ctx["args"]
    cmd_env(ctx["config"], unset=unset)

def _h_engine(ctx):
    target = ctx["args"][0] if ctx["args"] else ""
    if target:
        _maybe_dry_run("engine", lambda: _plan_engine(ctx["backend"], target))
        _exec_with_lock("config", "engine",
                        cmd_engine, ctx["backend"], target, ctx["config"])
    else:
        cmd_engine(ctx["backend"], target, ctx["config"])

def _h_daemon(ctx):
    name = ctx["args"][0] if ctx["args"] else ""
    subcmd = ctx["args"][1] if len(ctx["args"]) > 1 else ""
    if subcmd in ("start", "stop", "restart"):
        _maybe_dry_run("daemon",
                       lambda: _plan_daemon(name, subcmd, "<plist_dst>"))
        _exec_with_lock("daemon", "daemon", cmd_daemon, name, subcmd, ctx["config"])
    else:
        cmd_daemon(name, subcmd, ctx["config"])

def _h_claude_proxy(ctx):
    """daemon claude-proxy <subcmd> 的别名，向后兼容。"""
    subcmd = ctx["args"][0] if ctx["args"] else "status"
    if subcmd in ("start", "stop", "restart"):
        _maybe_dry_run("claude-proxy",
                       lambda: _plan_daemon("claude-proxy", subcmd, "<plist_dst>"))
        _exec_with_lock("daemon", "claude-proxy",
                        cmd_daemon, "claude-proxy", subcmd, ctx["config"])
    else:
        cmd_daemon("claude-proxy", subcmd, ctx["config"])

def _h_audit(ctx):
    arg = ctx["args"][0] if ctx["args"] else "1"
    apply_mode = (arg == "apply")
    days_str = (ctx["args"][1] if (apply_mode and len(ctx["args"]) > 1)
                else (arg if not apply_mode else "1"))
    # arg 既不是数字也不是 "apply" → did-you-mean
    if not apply_mode and not days_str.lstrip("-").isdigit():
        import difflib
        suggest = difflib.get_close_matches(arg, ["apply"], n=1, cutoff=0.5)
        hints = ["proxyctl audit [days]  # 扫描最近 N 天",
                 "proxyctl audit apply [days]  # 自动应用建议"]
        if suggest:
            hints.insert(0, f"是否想要：{suggest[0]}？")
        _io.fail(f"未识别 audit 参数：{arg}",
                 hints=hints, doc="rules",
                 code=_io.USAGE, cmd="audit")
    try:
        days = int(days_str)
    except ValueError:
        days = 1
    from proxyctl.audit import cmd_audit
    if apply_mode:
        _maybe_dry_run("audit", lambda: _plan_audit_apply(days))
        _exec_with_lock("config", "audit", cmd_audit,
                        days, ctx["api_base"], ctx["api_secret"], apply_mode)
    else:
        cmd_audit(days, ctx["api_base"], ctx["api_secret"], apply_mode)

def _h_mode(ctx):
    target = ctx["args"][0] if ctx["args"] else ""
    if target in ("tun", "proxy"):
        _maybe_dry_run("mode", lambda: _plan_mode(ctx["backend"], target))
        _exec_with_lock("config", "mode", cmd_mode, ctx["backend"], target)
    else:
        cmd_mode(ctx["backend"], target)

def _h_trace(ctx):
    if not ctx["args"]:
        _io.fail("trace 需要一个 domain 或 url 参数",
                 hint="proxyctl trace github.com",
                 doc="troubleshooting",
                 code=_io.USAGE, cmd="trace",
                 as_json=GLOBAL_FLAGS.get("json", False))
    from proxyctl.trace import cmd_trace
    cmd_trace(ctx["args"][0], ctx["api_base"], ctx["api_secret"], ctx["config"])

def _h_explain(ctx):
    from proxyctl import explain as _ex
    _ex.cmd_explain(ctx["args"], ctx["backend"], ctx["config"])

def _h_agent_guide(ctx):
    from proxyctl import explain as _ex
    _ex.cmd_agent_guide(ctx["args"], ctx["backend"], ctx["config"])

def _h_commands(ctx):
    from proxyctl import explain as _ex
    _ex.cmd_commands(ctx["args"], ctx["backend"], ctx["config"])

def _h_config(ctx):
    from proxyctl import explain as _ex
    if ctx["args"] and ctx["args"][0] == "set":
        # config set <key> <value>
        if len(ctx["args"]) >= 3:
            _maybe_dry_run(
                "config",
                lambda: _plan_config_set(
                    os.path.join(HOME, ".config", "proxyctl", "config.yaml"),
                    ctx["args"][1], ctx["args"][2]))
        _exec_with_lock("config", "config",
                        _ex.cmd_config, ctx["args"], ctx["backend"], ctx["config"])
    else:
        _ex.cmd_config(ctx["args"], ctx["backend"], ctx["config"])

def _h_doctor(ctx):
    from proxyctl import explain as _ex
    _ex.cmd_doctor(ctx["args"], ctx["backend"], ctx["config"])

def _h_completion(ctx):
    from proxyctl import completion as _cmp
    _cmp.cmd_completion(ctx["args"])


def _h_help(ctx):
    """proxyctl help [<cmd>] — main() 已前置处理，但保留在 DISPATCH 用于元数据完备性。"""
    if ctx["args"]:
        cmd_subcommand_help(ctx["args"][0])
    else:
        cmd_help()


DISPATCH: dict = {
    "start":           _h_start,
    "stop":            _h_stop,
    "restart":         _h_restart,
    "restart-clean":   _h_restart_clean,
    "status":          _h_status,
    "log":             _h_log,
    "check":           _h_check,
    "bench":           _h_bench,
    "fix":             _h_fix,
    "recover":         _h_recover,
    "dns-lock":        _h_dns_lock,
    "dns-unlock":      _h_dns_unlock,
    "env":             _h_env,
    "plugins":         _h_plugins,
    "engine":          _h_engine,
    "daemon":          _h_daemon,
    "claude-proxy":    _h_claude_proxy,
    "audit":           _h_audit,
    "mode":            _h_mode,
    "trace":           _h_trace,
    "explain":         _h_explain,
    "agent-guide":     _h_agent_guide,
    "agent_guide":     _h_agent_guide,  # 下划线别名
    "commands":        _h_commands,
    "config":          _h_config,
    "doctor":          _h_doctor,
    "completion":      _h_completion,
    "help":            _h_help,
}


def _exec_with_lock(lock_name: str, cmd_label: str, fn, *args, **kwargs):
    """包装写操作：拿不到锁则 LOCKED(8) 失败；拿到则执行。"""
    try:
        with _io.with_lock(lock_name):
            return fn(*args, **kwargs)
    except _io.LockedError as e:
        _io.fail(
            f"另一个 proxyctl 写操作正在进行（lock: {lock_name}）",
            hints=[
                f"锁文件: {e.lock_path}",
                f"排查: lsof {e.lock_path}  # 看谁持有",
                f"确认无 proxyctl 进程后可手动: rm {e.lock_path}",
            ],
            doc="locks",
            code=_io.LOCKED, cmd=cmd_label)


def _known_commands() -> list:
    """所有可识别的顶层命令（含别名）。"""
    from proxyctl.explain import COMMANDS_META
    names = [c["name"] for c in COMMANDS_META]
    names += ["restart-clean", "help"]
    return sorted(set(names))


def _suggest_command_and_exit(unknown: str) -> None:
    """未识别子命令 → 拼写建议 + 退出码 USAGE(2)。"""
    import difflib
    cands = difflib.get_close_matches(unknown, _known_commands(), n=3, cutoff=0.5)
    as_json = GLOBAL_FLAGS.get("json", False)
    hint = "proxyctl commands  # 查看所有命令"
    if cands:
        hint = f"是否想要：{', '.join(cands)} ？  ({hint})"
    _io.fail(f"未识别子命令：{unknown}", hint=hint, doc="agent",
             code=_io.USAGE, cmd=unknown, as_json=as_json)


if __name__ == "__main__":
    main()