"""测试 core/plugin.py — Plugin 基类、Hook 数据类、PluginRegistry。

注册中心是 proxyctl 插件机制的核心，覆盖：
- 内置插件加载
- 用户插件加载（成功/失败/隔离/禁用列表）
- collect/collect_set/invoke 调用语义
- 错误处理（不抛出，记录到 errors 里）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from proxyctl.core.plugin import (
    CheckTarget,
    OutboundProbe,
    StatusSection,
    DnsHook,
    RouteHook,
    WatchdogLayer,
    Plugin,
    PluginRegistry,
    build_registry,
    _fmt_err,
)


# ────────────────────────────────────────────────────────────────────────────
# Hook 数据类：默认值与赋值
# ────────────────────────────────────────────────────────────────────────────

def test_check_target_defaults():
    t = CheckTarget(name="x", url="https://x")
    assert t.mode == "proxy"
    assert t.timeout == 8
    assert t.only_when is None


def test_outbound_probe_defaults():
    p = OutboundProbe(name="proxy")
    assert p.url == "https://api.ipify.org"
    assert p.mode == "proxy"
    assert p.extract_re == ""


def test_status_section_callable():
    section = StatusSection(name="x", gather=lambda c: {"a": 1}, render=lambda c, d: None)
    assert section.gather({}) == {"a": 1}


def test_dns_route_hooks_optional():
    h = DnsHook(name="x")
    assert h.activate is None and h.deactivate is None
    r = RouteHook(name="y")
    assert r.activate is None


def test_watchdog_layer_basic():
    w = WatchdogLayer(name="tuic", script_snippet="echo ok")
    assert w.script_snippet == "echo ok"


# ────────────────────────────────────────────────────────────────────────────
# Plugin 基类：默认实现
# ────────────────────────────────────────────────────────────────────────────

def test_plugin_default_returns_empty():
    p = Plugin()
    assert p.check_groups() == []
    assert p.check_targets({}) == []
    assert p.check_outbound_probes({}) == []
    assert p.dns_hooks() == []
    assert p.route_hooks() == []
    assert p.status_sections({}) == []
    assert p.watchdog_layers() == []
    assert p.audit_skip_hosts() == set()
    assert p.audit_known_proxy_kw() == []


def test_plugin_lifecycle_hooks_dont_raise():
    p = Plugin()
    p.on_start({})
    p.on_stop({})
    p.on_recover({})


def test_plugin_carries_config():
    p = Plugin({"k": "v"})
    assert p.config == {"k": "v"}


# ────────────────────────────────────────────────────────────────────────────
# Registry: 注册与调用
# ────────────────────────────────────────────────────────────────────────────

class _Echo(Plugin):
    name = "echo"

    def check_groups(self) -> list[str]:
        return ["g1", "g2"]

    def check_targets(self, ctx: dict) -> list[CheckTarget]:
        return [CheckTarget(name="t", url="u")]

    def audit_skip_hosts(self) -> set[str]:
        return {"a.com", "b.com"}


class _Boom(Plugin):
    name = "boom"

    def check_groups(self) -> list[str]:
        raise RuntimeError("kaboom")


def test_registry_register_and_collect():
    reg = PluginRegistry()
    reg.register(_Echo())
    assert reg.collect("check_groups") == ["g1", "g2"]
    assert reg.collect("check_targets", {})[0].name == "t"


def test_registry_collect_set_dedup():
    reg = PluginRegistry()
    reg.register(_Echo())
    reg.register(_Echo())   # 同样的集合两次
    assert reg.collect_set("audit_skip_hosts") == {"a.com", "b.com"}


def test_registry_collect_swallows_hook_errors(capsys):
    reg = PluginRegistry()
    reg.register(_Boom())
    out = reg.collect("check_groups")
    assert out == []
    captured = capsys.readouterr()
    assert "boom.check_groups" in captured.err
    assert "RuntimeError" in captured.err


def test_registry_invoke():
    captured = []

    class P(Plugin):
        name = "p"

        def on_start(self, ctx: dict) -> None:
            captured.append(("start", ctx))

    reg = PluginRegistry()
    reg.register(P())
    reg.invoke("on_start", {"x": 1})
    assert captured == [("start", {"x": 1})]


def test_registry_invoke_swallows_hook_errors(capsys):
    class P(Plugin):
        name = "boom2"

        def on_start(self, ctx: dict) -> None:
            raise ValueError("die")

    reg = PluginRegistry()
    reg.register(P())
    reg.invoke("on_start", {})
    assert "boom2.on_start" in capsys.readouterr().err


def test_registry_collect_ignores_missing_hook():
    """plugin 没实现某 hook 时不抛错。"""
    reg = PluginRegistry()
    reg.register(Plugin())
    assert reg.collect("not_a_real_hook") == []


# ────────────────────────────────────────────────────────────────────────────
# Registry: 内置插件加载
# ────────────────────────────────────────────────────────────────────────────

def test_load_builtin_picks_up_known_plugins():
    """加载内置插件目录应至少有 connectivity-basic。"""
    reg = PluginRegistry()
    reg.load_builtin({})
    names = {p.name for p in reg.plugins}
    assert "connectivity-basic" in names


# ────────────────────────────────────────────────────────────────────────────
# Registry: 用户插件加载（隔离失败 / 禁用名单）
# ────────────────────────────────────────────────────────────────────────────

def test_load_user_skips_missing_dir(tmp_path: Path):
    reg = PluginRegistry()
    reg.load_user(str(tmp_path / "nonexistent"), {})
    assert reg.plugins == []
    assert reg.errors == []


def test_load_user_succeeds(tmp_path: Path):
    """正常的用户插件能加载。"""
    (tmp_path / "good.py").write_text(
        "from proxyctl.core.plugin import Plugin\n"
        "class P(Plugin):\n"
        "    name = 'usergood'\n"
    )
    reg = PluginRegistry()
    reg.load_user(str(tmp_path), {})
    assert "usergood" in {p.name for p in reg.plugins}


def test_load_user_isolates_broken_plugin(tmp_path: Path):
    """一个文件 import 失败不影响其它文件加载。"""
    (tmp_path / "bad.py").write_text("raise SystemError('broken at import')\n")
    (tmp_path / "ok.py").write_text(
        "from proxyctl.core.plugin import Plugin\n"
        "class P(Plugin):\n"
        "    name = 'survivor'\n"
    )
    reg = PluginRegistry()
    reg.load_user(str(tmp_path), {})
    names = {p.name for p in reg.plugins}
    assert "survivor" in names
    sources = {src for src, _ in reg.errors}
    assert any("bad.py" in s for s in sources)


def test_load_user_ignores_underscore_and_non_py(tmp_path: Path):
    (tmp_path / "_private.py").write_text("raise RuntimeError('should be skipped')\n")
    (tmp_path / "note.txt").write_text("not python")
    reg = PluginRegistry()
    reg.load_user(str(tmp_path), {})
    assert reg.errors == []


def test_load_user_respects_plugins_disabled(tmp_path: Path):
    (tmp_path / "muted.py").write_text(
        "from proxyctl.core.plugin import Plugin\n"
        "class P(Plugin):\n"
        "    name = 'muted'\n"
    )
    reg = PluginRegistry()
    reg.load_user(str(tmp_path), {"plugins_disabled": ["muted"]})
    assert all(p.name != "muted" for p in reg.plugins)


def test_load_user_constructor_error_recorded(tmp_path: Path):
    (tmp_path / "constructor_boom.py").write_text(
        "from proxyctl.core.plugin import Plugin\n"
        "class P(Plugin):\n"
        "    name = 'cboom'\n"
        "    def __init__(self, config=None):\n"
        "        raise RuntimeError('constructor died')\n"
    )
    reg = PluginRegistry()
    reg.load_user(str(tmp_path), {})
    assert not any(p.name == "cboom" for p in reg.plugins)
    assert any("constructor_boom.py" in src for src, _ in reg.errors)


def test_load_user_empty_name_warns_but_loads(tmp_path: Path, capsys):
    (tmp_path / "noname.py").write_text(
        "from proxyctl.core.plugin import Plugin\n"
        "class P(Plugin):\n"
        "    name = ''\n"
    )
    reg = PluginRegistry()
    reg.load_user(str(tmp_path), {})
    assert len([p for p in reg.plugins if type(p).__name__ == "P"]) == 1
    assert "empty name" in capsys.readouterr().err


# ────────────────────────────────────────────────────────────────────────────
# build_registry 工厂
# ────────────────────────────────────────────────────────────────────────────

def test_build_registry(tmp_path: Path):
    (tmp_path / "extra.py").write_text(
        "from proxyctl.core.plugin import Plugin\n"
        "class P(Plugin):\n"
        "    name = 'extra'\n"
    )
    reg = build_registry({}, str(tmp_path))
    names = {p.name for p in reg.plugins}
    assert "extra" in names
    assert "connectivity-basic" in names


# ────────────────────────────────────────────────────────────────────────────
# _fmt_err 行为
# ────────────────────────────────────────────────────────────────────────────

def test_fmt_err_compact_by_default(monkeypatch):
    monkeypatch.delenv("PROXYCTL_DEBUG", raising=False)
    e = ValueError("oops")
    s = _fmt_err(e)
    assert s == "ValueError: oops"


def test_fmt_err_traceback_when_debug(monkeypatch):
    monkeypatch.setenv("PROXYCTL_DEBUG", "1")
    try:
        raise ValueError("oops")
    except ValueError as e:
        s = _fmt_err(e)
    assert "Traceback" in s
