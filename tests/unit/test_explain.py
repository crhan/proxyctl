"""测试 explain / agent-guide / commands / config / doctor 模块。"""

from __future__ import annotations

import io
import json
import sys

import pytest

from proxyctl import _io, explain
from proxyctl.cli import MihomoBackend


@pytest.fixture
def backend():
    return MihomoBackend({"config_dir": "/tmp/test", "proxy_port": 7890})


@pytest.fixture
def config():
    return {
        "backend": "mihomo",
        "proxy_port": 7890,
        "api_base": "http://127.0.0.1:9090",
        "api_secret": "",
        "extra_daemons": {"claude-proxy": {"label": "com.example"}},
        "corp_dns": {"server": "10.0.0.53", "domain": "corp.example.com"},
    }


# ── Topic registry ─────────────────────────────────────────────────────────
def test_topics_registered():
    """所有一级 topic 必须存在。"""
    for name in ("rules", "nodes", "config", "dns", "engine",
                 "troubleshooting", "exit-codes", "agent",
                 "ports", "extra-daemons", "env", "corp-dns", "plugins"):
        assert name in explain.TOPICS


def test_each_topic_returns_required_fields(backend, config):
    for name, fn in explain.TOPICS.items():
        card = fn(backend, config)
        # 字段契约
        assert card["topic"] == name
        for key in ("summary", "file", "edit", "verify"):
            assert key in card, f"{name} 缺 {key}"
            assert isinstance(card[key], str), f"{name}.{key} 不是 str"
        assert isinstance(card.get("next", []), list)


def test_rules_card_points_to_backend_config_file(backend, config):
    card = explain.TOPICS["rules"](backend, config)
    assert backend.config_file in card["file"]
    assert "rules:" in card["file"]


def test_nodes_card_mentions_proxies_section(backend, config):
    card = explain.TOPICS["nodes"](backend, config)
    assert "proxies" in card["file"]


def test_exit_codes_card_lists_all_codes(backend, config):
    card = explain.TOPICS["exit-codes"](backend, config)
    for code in (0, 1, 2, 3, 5, 6, 7, 8):
        assert f" {code} " in card["edit"] or f" {code}  " in card["edit"]


# ── cmd_explain / quickref ────────────────────────────────────────────────
def test_cmd_explain_quickref_prints_three_questions(backend, config, capsys):
    explain.set_global_flags({"json": False})
    explain.cmd_explain([], backend, config)
    out = capsys.readouterr().out
    assert "rules" in out
    assert "nodes" in out
    assert "config" in out
    # 三大问题都给入口
    assert "proxyctl explain" in out


def test_cmd_explain_unknown_topic_fails_with_usage(backend, config):
    explain.set_global_flags({"json": False})
    with pytest.raises(SystemExit) as exc:
        explain.cmd_explain(["nosuchtopic"], backend, config)
    assert exc.value.code == _io.USAGE


def test_cmd_explain_unknown_topic_did_you_mean(backend, config, capsys):
    explain.set_global_flags({"json": False})
    with pytest.raises(SystemExit):
        explain.cmd_explain(["rule"], backend, config)
    err = capsys.readouterr().err
    assert "rules" in err  # 拼写建议


def test_cmd_explain_json_is_valid_json(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_explain(["dns"], backend, config)
    obj = json.loads(capsys.readouterr().out)
    assert obj["cmd"] == "explain"
    assert obj["data"]["topic"] == "dns"


def test_cmd_explain_no_arg_json_includes_topics_list(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_explain([], backend, config)
    obj = json.loads(capsys.readouterr().out)
    assert isinstance(obj["data"]["topics"], list)
    assert "rules" in obj["data"]["topics"]
    assert "quickref" in obj["data"]


# ── agent-guide ───────────────────────────────────────────────────────────
def test_agent_guide_has_required_sections(backend, config, capsys):
    explain.set_global_flags({"json": False})
    explain.cmd_agent_guide([], backend, config)
    text = capsys.readouterr().out

    # 9 个关键章节关键词必须都在
    required = [
        "一句话",  # 一句话定位
        "能做什么",
        "不能做什么",
        "概念地图",
        "关键路径",
        "退出码",
        "故障决策树",
        "non-interactive",
        "JSON envelope",
        "footgun",
    ]
    for kw in required:
        assert kw in text, f"agent-guide 缺少章节关键词: {kw}"

    # 至少 80 行（agent 入门要够厚）
    assert text.count("\n") >= 80


def test_agent_guide_json_includes_markdown(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_agent_guide([], backend, config)
    obj = json.loads(capsys.readouterr().out)
    assert obj["cmd"] == "agent-guide"
    assert "markdown" in obj["data"]
    assert "agent-guide" in obj["data"]["markdown"] or "Agent" in obj["data"]["markdown"]


# ── commands ──────────────────────────────────────────────────────────────
def test_commands_meta_required_fields():
    """每条命令元数据都必须有 agent 决策必备字段。"""
    required = {"name", "group", "summary", "supports_json",
                "side_effects", "needs_sudo", "interactive",
                "exit_codes", "examples"}
    for c in explain.COMMANDS_META:
        missing = required - set(c.keys())
        assert not missing, f"{c['name']} 缺字段: {missing}"
        assert isinstance(c["exit_codes"], list)
        assert isinstance(c["examples"], list) and c["examples"]


def test_commands_meta_covers_all_dispatch_targets():
    """与 cli.main() 中可识别的命令一致。"""
    from proxyctl.cli import _known_commands
    meta_names = {c["name"] for c in explain.COMMANDS_META}
    known = set(_known_commands())
    # known 比 meta 多 'help' 和 'restart-clean'，meta 不必包含 help
    assert meta_names.issubset(known | {"help"})
    # 关键命令必须在 meta 里
    for must in ("status", "doctor", "explain", "agent-guide",
                 "commands", "config", "trace", "fix"):
        assert must in meta_names


def test_cmd_commands_json_valid(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_commands([], backend, config)
    obj = json.loads(capsys.readouterr().out)
    assert obj["schema_version"] == 1
    assert obj["data"]["schema_version"] == 1
    assert len(obj["data"]["commands"]) >= 20


# ── config get / path ─────────────────────────────────────────────────────
def test_cmd_config_path_prints_path(backend, config, capsys):
    explain.set_global_flags({"json": False})
    explain.cmd_config(["path"], backend, config)
    out = capsys.readouterr().out
    assert out.strip().endswith("config.yaml")


def test_cmd_config_get_top_level(backend, config, capsys):
    explain.set_global_flags({"json": False})
    explain.cmd_config(["get", "proxy_port"], backend, config)
    assert capsys.readouterr().out.strip() == "7890"


def test_cmd_config_get_dot_key(backend, config, capsys):
    explain.set_global_flags({"json": False})
    explain.cmd_config(["get", "corp_dns.server"], backend, config)
    assert capsys.readouterr().out.strip() == "10.0.0.53"


def test_cmd_config_get_missing_returns_not_found(backend, config):
    explain.set_global_flags({"json": False})
    with pytest.raises(SystemExit) as exc:
        explain.cmd_config(["get", "no.such.key"], backend, config)
    assert exc.value.code == _io.NOT_FOUND


def test_cmd_config_unknown_subcmd_usage(backend, config):
    explain.set_global_flags({"json": False})
    with pytest.raises(SystemExit) as exc:
        explain.cmd_config(["wat"], backend, config)
    assert exc.value.code == _io.USAGE


# ── doctor ────────────────────────────────────────────────────────────────
def test_doctor_data_shape(backend, config, monkeypatch, capsys):
    """mock 所有探测函数，断言 data 字段完整。"""
    monkeypatch.setattr("proxyctl.cli.service_running", lambda *a, **kw: True)
    monkeypatch.setattr(explain, "_tcp_open", lambda *a, **kw: True)
    monkeypatch.setattr(explain, "_dns_points_to_loopback", lambda: False)
    monkeypatch.setattr(explain, "_system_proxy_points_to_loopback", lambda p: True)
    monkeypatch.setattr(explain, "_quick_connectivity", lambda *a: True)

    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit) as exc:
        explain.cmd_doctor([], backend, config)
    # dns_ok=False → 非健康 → ENGINE_DOWN
    assert exc.value.code == _io.ENGINE_DOWN

    obj = json.loads(capsys.readouterr().out)
    for key in ("engine_up", "port_listen", "dns_ok",
                "system_proxy_ok", "connectivity_ok",
                "score", "max", "hint"):
        assert key in obj["data"]
    assert obj["data"]["score"] == 4
    assert obj["data"]["max"] == 5
    assert obj["data"]["hint"] == "proxyctl fix"


def test_doctor_all_healthy_returns_ok(backend, config, monkeypatch):
    monkeypatch.setattr("proxyctl.cli.service_running", lambda *a, **kw: True)
    monkeypatch.setattr(explain, "_tcp_open", lambda *a, **kw: True)
    monkeypatch.setattr(explain, "_dns_points_to_loopback", lambda: True)
    monkeypatch.setattr(explain, "_system_proxy_points_to_loopback", lambda p: True)
    monkeypatch.setattr(explain, "_quick_connectivity", lambda *a: True)
    explain.set_global_flags({"json": False})
    with pytest.raises(SystemExit) as exc:
        explain.cmd_doctor([], backend, config)
    assert exc.value.code == _io.OK
