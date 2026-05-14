"""插件机制 — 注册中心 + Plugin 基类 + Hook 数据类

设计原则：
- core 不感知任何具体业务（公司、订阅、设备名都走插件）
- 插件分两种加载源：
    1. src/proxyctl/builtin_plugins/*.py  — 仓库内置（通用增强）
    2. ~/.config/proxyctl/plugins/*.py    — 用户自定义（本机特例）
- 加载顺序：内置先于用户；用户可通过 config.yaml 关闭某个插件
- 单插件失败不影响主流程（捕获并打 warning 到 stderr）

每类扩展点对应一个 Hook 数据类，插件通过返回这些数据类的实例集合声明能力。
core 命令拿到集合后聚合执行，避免到处散落条件分支。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import pkgutil
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ── Hook 数据类 ──────────────────────────────────────────────────────────────

@dataclass
class CheckTarget:
    """连通性检查项（check 命令第 3 阶段）。

    mode:
      - proxy : 走 socks5h://127.0.0.1:7890
      - direct: 绕过所有代理
      - tcp   : 纯 TCP 连接测试（url 形如 'tcp:host:port'）
      - dns   : dig 测试（url 是 'dns:server:domain'）
    """
    name: str
    url: str
    mode: str = "proxy"
    timeout: int = 8
    only_when: Optional[Callable[[dict], bool]] = None  # ctx -> bool，控制是否启用


@dataclass
class OutboundProbe:
    """出口 IP 探测项（check 命令第 4 阶段）。

    通过指定不同的 mode + url，验证不同出口的实际 IP 归属。
    经典三种：proxy（走主代理）/ direct（绕代理）/ 命名组（走特定规则路由）。
    """
    name: str               # 展示名，如 'proxy' / 'claude' / 'direct'
    mode: str = "proxy"     # 'proxy' | 'direct'
    url: str = "https://ifconfig.me"


@dataclass
class StatusSection:
    """status 命令的附加段（如企业 VPN、Tailscale、Relay 等）。"""
    name: str
    gather: Callable[[dict], dict]      # ctx -> data
    render: Callable[[dict, dict], None]  # (ctx, data) -> None，自行 print


@dataclass
class DnsHook:
    """DNS activate/deactivate 时的额外动作（如劫持 AnyConnect 条目）。"""
    name: str
    activate: Optional[Callable[[dict], None]] = None     # ctx -> None
    deactivate: Optional[Callable[[dict], None]] = None


@dataclass
class RouteHook:
    """start/stop 时的额外路由注入（如 Tailscale 100.64/10 冲突修复）。"""
    name: str
    activate: Optional[Callable[[dict], None]] = None
    deactivate: Optional[Callable[[dict], None]] = None


@dataclass
class WatchdogLayer:
    """dns-watchdog 的附加检查层（如 TUIC 健康检测）。

    script_snippet 会被 watchdog 主脚本 source 进来，因此变量命名要避免冲突。
    用 layer 前缀（layer_xxx_var）规避。
    """
    name: str
    script_snippet: str


# ── Plugin 基类 ──────────────────────────────────────────────────────────────

class Plugin:
    """插件基类。子类按需实现 hook 方法，未实现的方法默认返回空集合。

    name 必填，作为唯一标识用于配置开关 / 日志归属。
    """
    name: str = ""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    # ── 生命周期钩子（接收 ctx，无返回值）─────────────────────────────────
    def on_start(self, ctx: dict) -> None: pass
    def on_stop(self, ctx: dict) -> None: pass
    def on_recover(self, ctx: dict) -> None: pass

    # ── 检查项扩展 ────────────────────────────────────────────────────────
    def check_groups(self) -> list[str]:
        """返回关注的代理组名（check/status 默认展示这些组）。"""
        return []

    def check_targets(self, ctx: dict) -> list[CheckTarget]:
        """返回额外的连通性测试项。"""
        return []

    def check_outbound_probes(self, ctx: dict) -> list[OutboundProbe]:
        """返回额外的出口 IP 探测项。"""
        return []

    # ── DNS / 路由钩子 ───────────────────────────────────────────────────
    def dns_hooks(self) -> list[DnsHook]:
        return []

    def route_hooks(self) -> list[RouteHook]:
        return []

    # ── status 段扩展 ────────────────────────────────────────────────────
    def status_sections(self, ctx: dict) -> list[StatusSection]:
        return []

    # ── watchdog 层扩展 ──────────────────────────────────────────────────
    def watchdog_layers(self) -> list[WatchdogLayer]:
        return []

    # ── audit 扩展 ───────────────────────────────────────────────────────
    def audit_skip_hosts(self) -> set[str]:
        return set()

    def audit_known_proxy_kw(self) -> list[str]:
        return []


# ── Registry ─────────────────────────────────────────────────────────────────

class PluginRegistry:
    """插件注册中心。负责发现、加载、调用。"""

    def __init__(self):
        self.plugins: list[Plugin] = []
        self.errors: list[tuple[str, str]] = []  # (source, error_msg) 加载期错误

    def register(self, plugin: Plugin) -> None:
        self.plugins.append(plugin)

    def load_builtin(self, config: dict) -> None:
        """加载 src/proxyctl/builtin_plugins/ 下的所有内置插件。"""
        try:
            from proxyctl import builtin_plugins as _bp
        except ImportError:
            return
        bp_dir = os.path.dirname(_bp.__file__)
        for _, modname, _ in pkgutil.iter_modules([bp_dir]):
            if modname.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"proxyctl.builtin_plugins.{modname}")
                self._discover_in_module(mod, config, source=f"builtin/{modname}")
            except Exception as e:
                self.errors.append((f"builtin/{modname}", _fmt_err(e)))

    def load_user(self, plugin_dir: str, config: dict) -> None:
        """加载 ~/.config/proxyctl/plugins/*.py。"""
        if not os.path.isdir(plugin_dir):
            return
        for fname in sorted(os.listdir(plugin_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            path = os.path.join(plugin_dir, fname)
            mod_name = f"proxyctl_user_plugin.{fname[:-3]}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError(f"failed to build spec for {path}")
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
                self._discover_in_module(mod, config, source=f"user/{fname}")
            except Exception as e:
                self.errors.append((f"user/{fname}", _fmt_err(e)))

    def _discover_in_module(self, mod, config: dict, *, source: str) -> None:
        """在模块里搜 Plugin 子类，实例化并注册。"""
        disabled = set(config.get("plugins_disabled", []) or [])
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if not isinstance(attr, type):
                continue
            if not issubclass(attr, Plugin) or attr is Plugin:
                continue
            try:
                inst = attr(config)
                if inst.name in disabled:
                    continue
                if not inst.name:
                    # 没起名字的插件容易和其他插件冲突，警告但仍加载
                    sys.stderr.write(
                        f"[plugin warning] {source}:{attr.__name__} has empty name\n"
                    )
                self.register(inst)
            except Exception as e:
                self.errors.append((f"{source}:{attr.__name__}", _fmt_err(e)))

    # ── Hook 调用接口 ────────────────────────────────────────────────────

    def collect(self, hook_name: str, *args, **kwargs) -> list:
        """收集所有插件某 hook 的返回值，拼成单个 list 返回。

        用于返回 list 类型的 hook（check_targets / status_sections 等）。
        """
        result = []
        for p in self.plugins:
            fn = getattr(p, hook_name, None)
            if fn is None:
                continue
            try:
                vals = fn(*args, **kwargs)
                if vals:
                    result.extend(vals)
            except Exception as e:
                sys.stderr.write(
                    f"[plugin warning] {p.name}.{hook_name} failed: {_fmt_err(e)}\n"
                )
        return result

    def collect_set(self, hook_name: str, *args, **kwargs) -> set:
        """同 collect，但用 set 合并（去重）。"""
        result: set = set()
        for p in self.plugins:
            fn = getattr(p, hook_name, None)
            if fn is None:
                continue
            try:
                vals = fn(*args, **kwargs)
                if vals:
                    result.update(vals)
            except Exception as e:
                sys.stderr.write(
                    f"[plugin warning] {p.name}.{hook_name} failed: {_fmt_err(e)}\n"
                )
        return result

    def invoke(self, hook_name: str, *args, **kwargs) -> None:
        """调用所有插件的某 hook（无返回值场景，如生命周期钩子）。"""
        for p in self.plugins:
            fn = getattr(p, hook_name, None)
            if fn is None:
                continue
            try:
                fn(*args, **kwargs)
            except Exception as e:
                sys.stderr.write(
                    f"[plugin warning] {p.name}.{hook_name} failed: {_fmt_err(e)}\n"
                )


def _fmt_err(e: BaseException) -> str:
    if os.environ.get("PROXYCTL_DEBUG"):
        return traceback.format_exc()
    return f"{type(e).__name__}: {e}"


# ── 入口工厂 ─────────────────────────────────────────────────────────────────

def build_registry(config: dict, user_plugin_dir: str) -> PluginRegistry:
    """加载内置 + 用户插件，返回 registry。"""
    reg = PluginRegistry()
    reg.load_builtin(config)
    reg.load_user(user_plugin_dir, config)
    return reg
