"""集成测试：通过 main() 入口走 CLI 路由，mock 每个 cmd_* 实现。

目的：
1. 验证 sys.argv → 子命令分发正确
2. 验证 --help/--version/无参数等全局开关
3. 验证 audit / mode / trace 等带参数的解析

不测命令的具体业务逻辑（已被单元测试覆盖）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from proxyctl import cli


# ────────────────────────────────────────────────────────────────────────────
# 公共 fixture：用一个干净 config + 替换 plugin loader，避开真实文件 IO
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def patched_env(monkeypatch):
    """把 main() 内所有 IO 桩掉，返回各 cmd_* 的 mock 字典。"""
    mocks = {}

    def _add(name: str) -> MagicMock:
        m = MagicMock(name=name)
        mocks[name] = m
        monkeypatch.setattr(cli, name, m)
        return m

    monkeypatch.setattr(cli, "load_config", lambda: {
        "backend": "mihomo",
        "api_base": "http://127.0.0.1:9090",
        "api_secret": "test-secret",
        "config_dir": "/tmp/proxyctl-test",
        "dns_lock_label": "com.proxyctl.dns-lock",
    })
    monkeypatch.setattr(cli, "load_plugins", lambda c: MagicMock(plugins=[], errors=[],
                                                                  collect=lambda *a, **k: []))
    monkeypatch.setattr(cli, "get_mode", lambda b: "tun")

    # 各个 cmd_*
    _add("cmd_start")
    _add("cmd_stop")
    _add("cmd_restart")
    _add("cmd_fix")
    _add("cmd_recover")
    _add("cmd_dns_lock")
    _add("cmd_dns_unlock")
    _add("cmd_env")
    _add("cmd_plugins")
    _add("cmd_engine")
    _add("cmd_daemon")
    _add("cmd_mode")
    _add("cmd_help")

    # 子模块里的命令也要桩
    import proxyctl.status as status_mod
    import proxyctl.check as check_mod
    import proxyctl.audit as audit_mod
    import proxyctl.trace as trace_mod

    mocks["cmd_status"] = MagicMock()
    mocks["cmd_check"]  = MagicMock()
    mocks["cmd_bench"]  = MagicMock()
    mocks["cmd_audit"]  = MagicMock()
    mocks["cmd_trace"]  = MagicMock()
    monkeypatch.setattr(status_mod, "cmd_status", mocks["cmd_status"])
    monkeypatch.setattr(check_mod,  "cmd_check",  mocks["cmd_check"])
    monkeypatch.setattr(check_mod,  "cmd_bench",  mocks["cmd_bench"])
    monkeypatch.setattr(audit_mod,  "cmd_audit",  mocks["cmd_audit"])
    monkeypatch.setattr(trace_mod,  "cmd_trace",  mocks["cmd_trace"])

    return mocks


def _run(monkeypatch, *argv: str):
    """模拟 sys.argv 调用 cli.main()。"""
    monkeypatch.setattr(sys, "argv", ["proxyctl", *argv])
    cli.main()


# ────────────────────────────────────────────────────────────────────────────
# 全局开关
# ────────────────────────────────────────────────────────────────────────────

def test_version_exits_zero(monkeypatch, capsys, patched_env):
    monkeypatch.setattr(sys, "argv", ["proxyctl", "--version"])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "proxyctl v" in out


def test_help_long_form(monkeypatch, patched_env):
    _run(monkeypatch, "--help")
    patched_env["cmd_help"].assert_called_once()


def test_help_short_form(monkeypatch, patched_env):
    _run(monkeypatch, "-h")
    patched_env["cmd_help"].assert_called_once()


def test_no_args_defaults_to_status(monkeypatch, patched_env):
    _run(monkeypatch)
    patched_env["cmd_status"].assert_called_once()


def test_unknown_command_did_you_mean(monkeypatch, patched_env, capsys):
    """v0.2: 未识别子命令 → did-you-mean + USAGE(2)，不再走 cmd_help。"""
    monkeypatch.setattr(sys, "argv", ["proxyctl", "nonsense-cmd"])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "未识别子命令" in err


# ────────────────────────────────────────────────────────────────────────────
# 基础命令路由
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,mock_name", [
    ("start",    "cmd_start"),
    ("stop",     "cmd_stop"),
    ("status",   "cmd_status"),
    ("check",    "cmd_check"),
    ("fix",      "cmd_fix"),
    ("recover",  "cmd_recover"),
    ("dns-unlock", "cmd_dns_unlock"),
    ("plugins",  "cmd_plugins"),
])
def test_basic_command_dispatch(monkeypatch, patched_env, cmd, mock_name):
    _run(monkeypatch, cmd)
    patched_env[mock_name].assert_called_once()


def test_restart_default(monkeypatch, patched_env):
    _run(monkeypatch, "restart")
    args, kwargs = patched_env["cmd_restart"].call_args
    assert kwargs.get("clean", False) is False


def test_restart_clean(monkeypatch, patched_env):
    _run(monkeypatch, "restart-clean")
    args, kwargs = patched_env["cmd_restart"].call_args
    assert kwargs.get("clean") is True


def test_env_set(monkeypatch, patched_env):
    _run(monkeypatch, "env")
    args, kwargs = patched_env["cmd_env"].call_args
    assert kwargs.get("unset", False) is False


def test_env_unset(monkeypatch, patched_env):
    _run(monkeypatch, "env", "--unset")
    args, kwargs = patched_env["cmd_env"].call_args
    assert kwargs.get("unset") is True


def test_env_off_alias(monkeypatch, patched_env):
    """env off 等价于 env --unset。"""
    _run(monkeypatch, "env", "off")
    args, kwargs = patched_env["cmd_env"].call_args
    assert kwargs.get("unset") is True


# ────────────────────────────────────────────────────────────────────────────
# DNS-lock 带 --reload 参数
# ────────────────────────────────────────────────────────────────────────────

def test_dns_lock_default(monkeypatch, patched_env):
    _run(monkeypatch, "dns-lock")
    args, kwargs = patched_env["cmd_dns_lock"].call_args
    assert kwargs.get("reload", False) is False


def test_dns_lock_reload(monkeypatch, patched_env):
    _run(monkeypatch, "dns-lock", "--reload")
    args, kwargs = patched_env["cmd_dns_lock"].call_args
    assert kwargs.get("reload") is True


# ────────────────────────────────────────────────────────────────────────────
# audit 参数解析
# ────────────────────────────────────────────────────────────────────────────

def test_audit_default_days(monkeypatch, patched_env):
    """无参数时默认 1 天，apply=False。"""
    _run(monkeypatch, "audit")
    args, kwargs = patched_env["cmd_audit"].call_args
    days, api_base, api_secret, do_apply = args
    assert days == 1
    assert do_apply is False


def test_audit_custom_days(monkeypatch, patched_env):
    _run(monkeypatch, "audit", "7")
    days = patched_env["cmd_audit"].call_args[0][0]
    assert days == 7


def test_audit_apply_default_days(monkeypatch, patched_env):
    """proxyctl audit apply → days=1, apply=True。"""
    _run(monkeypatch, "audit", "apply")
    args = patched_env["cmd_audit"].call_args[0]
    assert args[0] == 1
    assert args[3] is True


def test_audit_apply_with_days(monkeypatch, patched_env):
    """proxyctl audit apply 3 → days=3, apply=True。"""
    _run(monkeypatch, "audit", "apply", "3")
    args = patched_env["cmd_audit"].call_args[0]
    assert args[0] == 3
    assert args[3] is True


def test_audit_invalid_days_usage_error(monkeypatch, patched_env, capsys):
    """0.3.0：audit 参数既不是数字也不是 'apply' → USAGE(2)，不再静默 fallback。"""
    monkeypatch.setattr(sys, "argv", ["proxyctl", "audit", "not-a-number"])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "未识别 audit 参数" in err


# ────────────────────────────────────────────────────────────────────────────
# trace
# ────────────────────────────────────────────────────────────────────────────

def test_trace_requires_arg(monkeypatch, patched_env, capsys):
    """v0.2: trace 缺参 → USAGE(2)，错误信息走 stderr，附 hint。"""
    monkeypatch.setattr(sys, "argv", ["proxyctl", "trace"])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "trace" in err and ("domain" in err or "url" in err)
    assert "hint" in err


def test_trace_passes_domain(monkeypatch, patched_env):
    _run(monkeypatch, "trace", "example.com")
    args = patched_env["cmd_trace"].call_args[0]
    assert args[0] == "example.com"


# ────────────────────────────────────────────────────────────────────────────
# bench (位置参数 groups)
# ────────────────────────────────────────────────────────────────────────────

def test_bench_no_groups(monkeypatch, patched_env):
    _run(monkeypatch, "bench")
    args = patched_env["cmd_bench"].call_args[0]
    # signature: (api, api_secret, bench_groups, default_groups=...)
    assert args[2] is None   # no specific groups


def test_bench_with_groups(monkeypatch, patched_env):
    _run(monkeypatch, "bench", "proxy", "claude")
    args = patched_env["cmd_bench"].call_args[0]
    assert args[2] == ["proxy", "claude"]


# ────────────────────────────────────────────────────────────────────────────
# mode / engine / daemon 命令
# ────────────────────────────────────────────────────────────────────────────

def test_mode_target_passed(monkeypatch, patched_env):
    _run(monkeypatch, "mode", "tun")
    args = patched_env["cmd_mode"].call_args[0]
    assert args[1] == "tun"


def test_engine_target_passed(monkeypatch, patched_env):
    _run(monkeypatch, "engine", "mihomo")
    args = patched_env["cmd_engine"].call_args[0]
    assert args[1] == "mihomo"


def test_daemon_subcmd_passed(monkeypatch, patched_env):
    _run(monkeypatch, "daemon", "claude-proxy", "status")
    args = patched_env["cmd_daemon"].call_args[0]
    assert args[0] == "claude-proxy"
    assert args[1] == "status"


def test_claude_proxy_alias(monkeypatch, patched_env):
    """proxyctl claude-proxy <subcmd> = proxyctl daemon claude-proxy <subcmd>"""
    _run(monkeypatch, "claude-proxy", "stop")
    args = patched_env["cmd_daemon"].call_args[0]
    assert args[0] == "claude-proxy"
    assert args[1] == "stop"


# ────────────────────────────────────────────────────────────────────────────
# 配置层 helper
# ────────────────────────────────────────────────────────────────────────────

def test_get_backend_mihomo():
    b = cli.get_backend({"backend": "mihomo"})
    assert isinstance(b, cli.MihomoBackend)


def test_get_backend_singbox_fallback():
    b = cli.get_backend({"backend": "singbox"})
    assert isinstance(b, cli.SingboxBackend)


def test_get_backend_unknown_falls_back_to_singbox():
    """非 mihomo 一律走 SingboxBackend，符合 main 的判断。"""
    b = cli.get_backend({"backend": "future-engine"})
    assert isinstance(b, cli.SingboxBackend)


def test_load_config_returns_defaults_when_no_file(monkeypatch, _isolate_home: Path):
    """无 config.yaml 时返回 DEFAULTS 副本。"""
    # _isolate_home fixture 已把 HOME 切到 tmp，config 文件不存在
    monkeypatch.setattr(cli, "CONFIG_FILE", str(_isolate_home / "no.yaml"))
    cfg = cli.load_config()
    assert cfg["backend"] == "mihomo"
    assert cfg["api_secret"] == ""


def test_load_config_merges_user_yaml(monkeypatch, _isolate_home: Path):
    cfg_file = _isolate_home / "config.yaml"
    cfg_file.write_text("api_secret: my-secret\nbackend: singbox\n")
    monkeypatch.setattr(cli, "CONFIG_FILE", str(cfg_file))
    cfg = cli.load_config()
    assert cfg["api_secret"] == "my-secret"
    assert cfg["backend"] == "singbox"


def test_load_config_handles_bad_yaml(monkeypatch, _isolate_home: Path, capsys):
    cfg_file = _isolate_home / "config.yaml"
    cfg_file.write_text("not valid: yaml: :\n  - [\n")
    monkeypatch.setattr(cli, "CONFIG_FILE", str(cfg_file))
    cfg = cli.load_config()
    # 失败仍返回 DEFAULTS 副本
    assert cfg["backend"] == "mihomo"
    err = capsys.readouterr().out + capsys.readouterr().err
    # 不强制内容，但应该返回了一份可用配置


def test_cmd_plugins_lists_loaded(capsys):
    """cmd_plugins 打印插件列表 + 错误。"""
    from proxyctl.core.plugin import Plugin, PluginRegistry

    class P(Plugin):
        name = "test-pl"

        def check_groups(self) -> list[str]:
            return ["g"]

    reg = PluginRegistry()
    reg.register(P())
    reg.errors.append(("user/x.py", "ImportError: nope"))

    cli.cmd_plugins(reg)
    out = capsys.readouterr().out
    assert "test-pl" in out
    assert "user/x.py" in out
