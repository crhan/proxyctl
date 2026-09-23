"""测试 proxyctl.agent_env — agent 代理变量契约文件（v0.5.14+）。

策略：端口存活探测（`_port_open`）全部 monkeypatch，测试不依赖真实监听；
HOME 由 conftest 隔离到 tmp_path，契约文件写在沙箱里。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from proxyctl import agent_env, cli, suggest_rules


@pytest.fixture
def open_ports(monkeypatch):
    """可编程的端口存活表：open_ports({7890}) → 只有 7890 算活着。"""
    def _set(ports):
        alive = set(int(p) for p in ports)
        monkeypatch.setattr(agent_env, "_port_open",
                            lambda port, timeout=0.5: int(port) in alive)
    _set(set())
    return _set


CONFIG = {"proxy_port": 7890,
          "extra_daemons": {"claude-proxy": {"port": 7891}}}


# ────────────────────────────────────────────────────────────────────────────
# 变量名 / 配置解析
# ────────────────────────────────────────────────────────────────────────────

def test_provider_var_matches_pi_naming():
    """变量名规则与 pi 系 CLI 的 PI_PROXY_<PROVIDER> 变换一致。"""
    assert agent_env.provider_var("anthropic") == "PI_PROXY_ANTHROPIC"
    assert agent_env.provider_var("claude-code") == "PI_PROXY_CLAUDE_CODE"
    assert agent_env.provider_var("openai.codex") == "PI_PROXY_OPENAI_CODEX"


def test_resolve_defaults_and_user_override():
    assert agent_env.resolve({})["providers"] == ["anthropic"]
    cfg = agent_env.resolve({"agent_env": {"providers": "anthropic,openai-codex",
                                           "fallback_daemon": None}})
    assert cfg["providers"] == ["anthropic", "openai-codex"]
    assert cfg["fallback_daemon"] is None
    # 单字段覆盖不影响其余默认值
    assert cfg["shell_rc"] == agent_env.DEFAULTS["shell_rc"]


def test_var_names_uses_config_providers():
    cfg = agent_env.resolve({"agent_env": {"providers": ["anthropic", "openai"]}})
    assert agent_env.var_names(cfg) == ["PI_PROXY_ANTHROPIC", "PI_PROXY_OPENAI"]


# ────────────────────────────────────────────────────────────────────────────
# 出口解析：引擎 → 兜底 daemon → 无
# ────────────────────────────────────────────────────────────────────────────

def test_target_prefers_engine(open_ports):
    open_ports({7890, 7891})
    assert agent_env.target(CONFIG) == ("http://127.0.0.1:7890", "engine")


def test_target_falls_back_to_daemon(open_ports):
    open_ports({7891})
    assert agent_env.target(CONFIG) == ("http://127.0.0.1:7891",
                                        "daemon:claude-proxy")


def test_target_none_when_nothing_alive(open_ports):
    open_ports(set())
    assert agent_env.target(CONFIG) == (None, "none")


def test_target_without_fallback_daemon(open_ports):
    open_ports({7891})
    cfg = {"proxy_port": 7890, "extra_daemons": {"claude-proxy": {"port": 7891}},
           "agent_env": {"fallback_daemon": None}}
    assert agent_env.target(cfg) == (None, "none")


# ────────────────────────────────────────────────────────────────────────────
# 渲染 / 解析
# ────────────────────────────────────────────────────────────────────────────

def test_render_roundtrip_via_load(open_ports):
    open_ports({7890})
    cfg = agent_env.resolve(CONFIG)
    agent_env.sync(CONFIG)

    st = agent_env.load()
    assert st is not None
    assert st["schema_version"] == agent_env.SCHEMA_VERSION
    assert st["source"] == "engine"
    assert st["endpoint"] == "http://127.0.0.1:7890"
    assert st["providers"] == ["anthropic"]
    assert st["vars"] == {"PI_PROXY_ANTHROPIC": "http://127.0.0.1:7890"}
    assert st["fallback_daemon"] == "claude-proxy"
    assert st["path"].endswith(".config/proxyctl/agent-env.sh")


def test_rendered_file_is_sourceable_shell(open_ports):
    """契约文件本身可被 sh source：export 行 + 无语法噪声。"""
    open_ports({7890})
    agent_env.sync(CONFIG)
    text = Path(agent_env.env_path()).read_text(encoding="utf-8")
    body = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert body == ['export PI_PROXY_ANTHROPIC="http://127.0.0.1:7890"']


# ────────────────────────────────────────────────────────────────────────────
# sync：语义 = 有存活出口就写、没有就删
# ────────────────────────────────────────────────────────────────────────────

def test_sync_writes_then_is_idempotent(open_ports, monkeypatch):
    open_ports({7890})
    first = agent_env.sync(CONFIG)
    assert first["action"] == "written"

    before = Path(agent_env.env_path()).read_text(encoding="utf-8")
    monkeypatch.setattr(agent_env, "_now_iso",
                        lambda: "2099-01-01T00:00:00+08:00")
    second = agent_env.sync(CONFIG)
    assert second["action"] == "unchanged"
    # 时间戳变了也不重写（否则每次 start 都会"脏"一次文件）
    assert Path(agent_env.env_path()).read_text(encoding="utf-8") == before


def test_sync_removes_file_when_no_endpoint(open_ports):
    open_ports({7890})
    agent_env.sync(CONFIG)
    assert os.path.isfile(agent_env.env_path())

    open_ports(set())
    st = agent_env.sync(CONFIG)
    assert st["action"] == "removed"
    assert not os.path.isfile(agent_env.env_path())


def test_sync_removes_file_when_disabled(open_ports):
    open_ports({7890})
    agent_env.sync(CONFIG)
    st = agent_env.sync({**CONFIG, "agent_env": {"enabled": False}})
    assert st["action"] == "removed"
    assert not os.path.isfile(agent_env.env_path())


def test_sync_rewrites_when_endpoint_switches_to_daemon(open_ports):
    open_ports({7890})
    agent_env.sync(CONFIG)
    open_ports({7891})
    st = agent_env.sync(CONFIG)
    assert st["action"] == "written"
    assert st["source"] == "daemon:claude-proxy"
    assert agent_env.load()["vars"]["PI_PROXY_ANTHROPIC"] == "http://127.0.0.1:7891"


# ────────────────────────────────────────────────────────────────────────────
# shell rc 注入
# ────────────────────────────────────────────────────────────────────────────

def test_install_shell_is_idempotent_and_preserves_content(open_ports, tmp_path):
    open_ports({7890})
    rc = tmp_path / "home/.zprofile"
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_text("export PATH=/x:$PATH\n", encoding="utf-8")

    first = agent_env.install_shell(agent_env.resolve(CONFIG))
    assert first["action"] == "updated"
    text = rc.read_text(encoding="utf-8")
    assert "export PATH=/x:$PATH" in text
    assert agent_env.SHELL_BEGIN in text and agent_env.SHELL_END in text
    assert agent_env.env_path() in text
    # 同一行只出现一次
    assert text.count(agent_env.SHELL_BEGIN) == 1

    assert agent_env.install_shell(agent_env.resolve(CONFIG))["action"] == "unchanged"
    assert rc.read_text(encoding="utf-8").count(agent_env.SHELL_BEGIN) == 1


def test_install_shell_creates_missing_rc(open_ports):
    open_ports({7890})
    res = agent_env.install_shell(agent_env.resolve(CONFIG))
    assert res["action"] == "created"
    assert agent_env.shell_rc_installed(agent_env.resolve(CONFIG)) is True


def test_uninstall_shell_removes_only_marker_block(open_ports, tmp_path):
    open_ports({7890})
    rc = tmp_path / "home/.zprofile"
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_text("line-before\n", encoding="utf-8")
    agent_env.install_shell(agent_env.resolve(CONFIG))
    with open(rc, "a", encoding="utf-8") as f:
        f.write("line-after\n")

    res = agent_env.uninstall_shell(agent_env.resolve(CONFIG))
    assert res["action"] == "removed"
    text = rc.read_text(encoding="utf-8")
    assert agent_env.SHELL_BEGIN not in text
    assert "line-before" in text and "line-after" in text
    assert agent_env.uninstall_shell(agent_env.resolve(CONFIG))["action"] == "unchanged"


# ────────────────────────────────────────────────────────────────────────────
# status（status 面板 / doctor 的输入）
# ────────────────────────────────────────────────────────────────────────────

def test_status_before_write(open_ports):
    open_ports({7890})
    st = agent_env.status(CONFIG)
    assert st["present"] is False
    assert st["expected_endpoint"] == "http://127.0.0.1:7890"
    assert st["in_sync"] is False


def test_status_stale_when_endpoint_dead(open_ports):
    open_ports({7890})
    agent_env.sync(CONFIG)
    open_ports(set())          # 出口挂了，文件还在
    st = agent_env.status(CONFIG)
    assert st["present"] is True and st["reachable"] is False
    assert st["stale"] is True


def test_status_in_sync_after_write(open_ports):
    open_ports({7890})
    agent_env.sync(CONFIG)
    st = agent_env.status(CONFIG)
    assert st["in_sync"] is True and st["reachable"] is True
    assert st["vars"] == {"PI_PROXY_ANTHROPIC": "http://127.0.0.1:7890"}


# ────────────────────────────────────────────────────────────────────────────
# doctor 规则
# ────────────────────────────────────────────────────────────────────────────

def _state(**over):
    base = {"enabled": True, "present": True, "reachable": True,
            "stale": False, "in_sync": True, "shell_rc_installed": True,
            "path": "/tmp/agent-env.sh", "endpoint": "http://127.0.0.1:7890",
            "source": "engine", "expected_endpoint": "http://127.0.0.1:7890",
            "expected_source": "engine", "shell_rc": "/tmp/.zprofile",
            "providers": ["anthropic"], "vars": {}}
    base.update(over)
    return base


def test_agent_env_rules_clean_state_is_quiet():
    assert suggest_rules.agent_env_rules(_state()) == []


def test_agent_env_rules_skip_when_disabled_or_missing_input():
    assert suggest_rules.agent_env_rules(None) == []
    assert suggest_rules.agent_env_rules({"enabled": False}) == []
    assert suggest_rules.agent_env_rules({"error": "boom"}) == []


def test_agent_env_rules_dead_endpoint_is_warn_and_autofixable():
    rules = suggest_rules.agent_env_rules(_state(reachable=False, stale=True,
                                                expected_endpoint=None))
    assert [r["id"] for r in rules] == ["agent_env.dead_endpoint"]
    r = rules[0]
    assert r["severity"] == "warn" and r["auto_fixable"] is True
    assert r["fix_command"] == "proxyctl env --write"
    assert r["doc"] == "suggestion:agent_env.dead_endpoint"


def test_agent_env_rules_missing_file_with_live_endpoint():
    rules = suggest_rules.agent_env_rules(_state(present=False, in_sync=False))
    assert [r["id"] for r in rules] == ["agent_env.missing"]
    assert rules[0]["severity"] == "advisory"


def test_agent_env_rules_missing_file_without_endpoint_is_quiet():
    """没有存活出口时"文件缺失"不是问题——没什么可写的。"""
    assert suggest_rules.agent_env_rules(
        _state(present=False, in_sync=False, expected_endpoint=None)) == []


def test_agent_env_rules_not_sourced():
    rules = suggest_rules.agent_env_rules(_state(shell_rc_installed=False))
    assert [r["id"] for r in rules] == ["agent_env.not_sourced"]
    assert rules[0]["fix_command"] == "proxyctl env --install"


# ────────────────────────────────────────────────────────────────────────────
# CLI：proxyctl env
# ────────────────────────────────────────────────────────────────────────────

def _reset_flags(json_mode=False):
    cli.GLOBAL_FLAGS.update({"json": json_mode, "quiet": False,
                             "dry_run": False, "no_color": True})


def test_cmd_env_prints_agent_var_when_endpoint_alive(open_ports, capsys):
    open_ports({7890})
    _reset_flags()
    cli.cmd_env(dict(CONFIG))
    out = capsys.readouterr().out
    assert "export https_proxy=http://127.0.0.1:7890;" in out
    assert 'export PI_PROXY_ANTHROPIC=http://127.0.0.1:7890;' in out


def test_cmd_env_unsets_agent_var_when_nothing_alive(open_ports, capsys):
    """无存活出口 → 输出 unset，避免 eval 后残留死口。"""
    open_ports(set())
    _reset_flags()
    cli.cmd_env(dict(CONFIG))
    out = capsys.readouterr().out
    assert "unset PI_PROXY_ANTHROPIC;" in out
    assert "export PI_PROXY_ANTHROPIC" not in out


def test_cmd_env_unset_includes_agent_vars(open_ports, capsys):
    open_ports({7890})
    _reset_flags()
    cli.cmd_env(dict(CONFIG), unset=True)
    out = capsys.readouterr().out
    assert "unset https_proxy;" in out
    assert "unset PI_PROXY_ANTHROPIC;" in out


def test_cmd_env_json_exposes_agent_env(open_ports, capsys):
    open_ports({7890})
    agent_env.sync(CONFIG)
    _reset_flags(json_mode=True)
    cli.cmd_env(dict(CONFIG))
    env = json.loads(capsys.readouterr().out)
    assert env["cmd"] == "env"
    assert env["data"]["vars"]["PI_PROXY_ANTHROPIC"] == "http://127.0.0.1:7890"
    assert env["data"]["agent_env"]["present"] is True


def test_cmd_env_write_creates_contract_file(open_ports, capsys):
    open_ports({7890})
    _reset_flags()
    cli.cmd_env(dict(CONFIG), mode="write")
    assert "agent env" in capsys.readouterr().out
    assert agent_env.load()["endpoint"] == "http://127.0.0.1:7890"


def test_cmd_env_install_then_uninstall(open_ports, capsys):
    open_ports({7890})
    _reset_flags()
    cli.cmd_env(dict(CONFIG), mode="install")
    out = capsys.readouterr().out
    assert "shell rc" in out
    assert agent_env.status(CONFIG)["shell_rc_installed"] is True
    assert os.path.isfile(agent_env.env_path())

    cli.cmd_env(dict(CONFIG), mode="uninstall")
    capsys.readouterr()
    assert agent_env.status(CONFIG)["shell_rc_installed"] is False
    assert not os.path.isfile(agent_env.env_path())


def test_cmd_env_shell_rc_flag_overrides_config(open_ports, capsys, tmp_path):
    open_ports({7890})
    _reset_flags()
    alt = tmp_path / "alt-rc"
    cli.cmd_env(dict(CONFIG), mode="install", shell_rc=str(alt))
    capsys.readouterr()
    assert agent_env.SHELL_BEGIN in alt.read_text(encoding="utf-8")


# ── 模式解析 / dry-run plan ────────────────────────────────────────────────

def test_env_mode_precedence():
    assert cli._env_mode([]) == ("print", None)
    assert cli._env_mode(["--unset"])[0] == "unset"
    assert cli._env_mode(["off"])[0] == "unset"
    assert cli._env_mode(["--write"])[0] == "write"
    assert cli._env_mode(["--install", "--write"])[0] == "install"
    assert cli._env_mode(["--uninstall", "--install"])[0] == "uninstall"


def test_env_mode_reads_shell_rc_value_and_equals_form():
    assert cli._env_mode(["--install", "--shell-rc", "/tmp/rc"]) == ("install", "/tmp/rc")
    assert cli._env_mode(["--install", "--shell-rc=/tmp/rc"]) == ("install", "/tmp/rc")


def test_plan_env_covers_write_install_uninstall():
    w = cli._plan_env("write", "/p/agent-env.sh", "/p/.zprofile")
    assert [s["target"] for s in w] == ["/p/agent-env.sh"]

    i = cli._plan_env("install", "/p/agent-env.sh", "/p/.zprofile")
    assert [s["target"] for s in i] == ["/p/agent-env.sh", "/p/.zprofile"]

    u = cli._plan_env("uninstall", "/p/agent-env.sh", "/p/.zprofile")
    assert [s["target"] for s in u] == ["/p/.zprofile", "/p/agent-env.sh"]
    assert all("config-write" in s["side_effects"] for s in w + i + u)


def test_h_env_dry_run_does_not_write(open_ports, capsys):
    """--dry-run 只输出 plan，不落盘。"""
    open_ports({7890})
    _reset_flags(json_mode=True)
    cli.GLOBAL_FLAGS["dry_run"] = True
    try:
        with pytest.raises(SystemExit) as exc:
            cli._h_env({"config": dict(CONFIG), "args": ["--install"]})
        assert exc.value.code == 0
        env = json.loads(capsys.readouterr().out)
        assert [s["target"] for s in env["data"]["plan"]] == [
            agent_env.env_path(), agent_env.shell_rc_path(agent_env.resolve(CONFIG))]
        assert not os.path.isfile(agent_env.env_path())
    finally:
        cli.GLOBAL_FLAGS["dry_run"] = False


def test_h_env_keeps_legacy_unset_kwarg(open_ports, monkeypatch):
    """集成 smoke 依赖 cmd_env(unset=...) 的调用形状，不能被重构掉。"""
    seen = {}

    def fake_cmd_env(config, unset=False, **kw):
        seen["unset"] = unset
        seen["mode"] = kw.get("mode")

    monkeypatch.setattr(cli, "cmd_env", fake_cmd_env)
    cli._h_env({"config": dict(CONFIG), "args": ["--unset"]})
    assert seen == {"unset": True, "mode": "unset"}
    cli._h_env({"config": dict(CONFIG), "args": []})
    assert seen == {"unset": False, "mode": "print"}
