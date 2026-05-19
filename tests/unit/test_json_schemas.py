"""锁定 v2 JSON envelope / 各命令 data schema，防止意外破坏。

策略：用 jsonschema 库写最小化的字段契约（关键字段 required + 类型）。
schema 故意 additionalProperties=True — 允许后续版本加字段，但已有字段不能消失/改类型。
"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from proxyctl import explain
from proxyctl.cli import MihomoBackend


# ── envelope v2 schema ────────────────────────────────────────────────────
ENVELOPE_V2 = {
    "type": "object",
    "required": ["schema_version", "cmd", "ok", "data", "error", "code",
                 "hints", "warnings", "doc", "meta"],
    "properties": {
        "schema_version": {"const": 2},
        "cmd": {"type": "string"},
        "ok": {"type": "boolean"},
        "data": {},  # 任何类型（含 null）
        "error": {"type": ["string", "null"]},
        "code": {"type": "integer", "minimum": 0},
        "hints": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "doc": {"type": ["string", "null"]},
        "meta": {
            "type": "object",
            "required": ["ts", "elapsed_ms", "proxyctl_version", "request_id"],
            "properties": {
                "ts": {"type": "string"},
                "elapsed_ms": {"type": ["integer", "null"], "minimum": 0},
                "proxyctl_version": {"type": "string"},
                "request_id": {"type": "string"},
            },
        },
    },
}


# 0.3.0 commands schema：side_effects 为 list[str enum]，新增可选 conditional_side_effects
COMMANDS_V2 = {
    "type": "object",
    "required": ["schema_version", "version", "commands"],
    "properties": {
        "schema_version": {"const": 2},
        "version": {"type": "string"},
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "group", "summary", "supports_json",
                             "side_effects", "needs_sudo", "interactive",
                             "exit_codes", "examples"],
                "properties": {
                    "name": {"type": "string"},
                    "group": {"type": "string"},
                    "summary": {"type": "string"},
                    "supports_json": {"type": "boolean"},
                    "side_effects": {
                        "type": "array",
                        "items": {"enum": ["process", "system", "config-write",
                                            "cache", "network-io"]},
                    },
                    "conditional_side_effects": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "array",
                            "items": {"enum": ["process", "system",
                                                "config-write",
                                                "cache", "network-io"]},
                        },
                    },
                    "needs_sudo": {"type": "boolean"},
                    "interactive": {"type": "boolean"},
                    "exit_codes": {"type": "array",
                                   "items": {"type": "integer"}},
                    "examples": {"type": "array",
                                 "items": {"type": "string"}},
                },
            },
        },
    },
}


DOCTOR_V2 = {
    "type": "object",
    "required": ["engine_up", "port_listen", "dns_ok",
                 "system_proxy_ok", "connectivity_ok",
                 "score", "max", "healthy", "hint"],
    # additionalProperties=True 是默认值；显式标注让向后兼容契约清晰：
    # v0.5.0+ data 新增 suggestions[]、engine_version、mode 等字段都通过此通道追加，
    # 老 agent（v0.4.x）按 required 列表读取的代码不受影响。
    "additionalProperties": True,
    "properties": {
        # v0.5.0+ 放宽：--suggest-only 模式下 5 项布尔 / score / healthy / hint
        # 全置 null，agent 据顶层 data.doctor_mode 字段识别。
        "engine_up": {"type": ["boolean", "null"]},
        "port_listen": {"type": ["boolean", "null"]},
        "dns_ok": {"type": ["boolean", "null"]},
        "system_proxy_ok": {"type": ["boolean", "null"]},
        "connectivity_ok": {"type": ["boolean", "null"]},
        "score": {"type": ["integer", "null"]},
        "max": {"type": ["integer", "null"]},
        "healthy": {"type": ["boolean", "null"]},
        "hint": {"type": ["string", "null"]},
        "doctor_mode": {"enum": ["full", "suggest_only"]},
        # v0.5.0+ 新增字段（非 required，老 agent 可忽略）
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "severity", "actor", "title", "evidence",
                             "doc", "fingerprint", "first_seen", "since"],
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"enum": ["info", "advisory", "warn"]},
                    "actor": {"enum": ["user", "agent", "cron", "engine"]},
                    "title": {"type": "string"},
                    "evidence": {"type": "object"},
                    "inspect_command": {"type": ["string", "null"]},
                    "fix_command": {"type": ["string", "null"]},
                    "auto_fixable": {"type": "boolean"},
                    "doc": {"type": "string"},
                    "fingerprint": {"type": "string"},
                    "first_seen": {"type": "string"},
                    "since": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
    },
}


EXPLAIN_TOPIC_V2 = {
    "type": "object",
    "required": ["topic", "summary", "file", "edit", "verify"],
    "properties": {
        "topic": {"type": "string"},
        "summary": {"type": "string"},
        "file": {"type": "string"},
        "edit": {"type": "string"},
        "verify": {"type": "string"},
        # PR-10 将统一改名 next → next_commands；当前两者都接受
        "next": {"type": "array", "items": {"type": "string"}},
        "next_commands": {"type": "array", "items": {"type": "string"}},
    },
}


CONFIG_GET_V2 = {
    "type": "object",
    "required": ["path", "key", "value"],
    "properties": {
        "path": {"type": "string"},
        "key": {"type": "string"},
        "value": {},  # 任何类型
    },
}


CONFIG_SET_V2 = {
    "type": "object",
    "required": ["path", "key", "old_value", "new_value", "backup"],
    "properties": {
        "path": {"type": "string"},
        "key": {"type": "string"},
        "backup": {"type": ["string", "null"]},
    },
}


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
        "extra_daemons": {},
        "corp_dns": {},
    }


def _validate(schema: dict, instance) -> None:
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(instance)


# ── envelope 通用结构（所有 --json 都得满足）─────────────────────────────
def test_envelope_v2_explain_rules(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_explain(["rules"], backend, config)
    env = json.loads(capsys.readouterr().out)
    _validate(ENVELOPE_V2, env)


def test_envelope_v2_explain_quickref(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_explain([], backend, config)
    env = json.loads(capsys.readouterr().out)
    _validate(ENVELOPE_V2, env)


def test_envelope_v2_agent_guide(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_agent_guide([], backend, config)
    env = json.loads(capsys.readouterr().out)
    _validate(ENVELOPE_V2, env)


def test_envelope_v2_commands(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_commands([], backend, config)
    env = json.loads(capsys.readouterr().out)
    _validate(ENVELOPE_V2, env)


def test_envelope_v2_config_path(backend, config, capsys, tmp_path):
    explain.set_global_flags({"json": True})
    explain.cmd_config(["path"], backend, config)
    env = json.loads(capsys.readouterr().out)
    _validate(ENVELOPE_V2, env)


def test_envelope_v2_config_get(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_config(["get", "proxy_port"], backend, config)
    env = json.loads(capsys.readouterr().out)
    _validate(ENVELOPE_V2, env)
    _validate(CONFIG_GET_V2, env["data"])


def test_envelope_v2_fail_path(backend, config, capsys):
    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit):
        explain.cmd_config(["get", "no.such.key"], backend, config)
    env = json.loads(capsys.readouterr().out)
    _validate(ENVELOPE_V2, env)
    assert env["ok"] is False
    assert env["data"] is None
    assert isinstance(env["error"], str)
    assert isinstance(env["hints"], list)


# ── data sub-schema（特定命令的关键字段）─────────────────────────────────
def test_explain_topic_data_schema_all_topics(backend, config, capsys):
    """每个 topic 的 data 段必须符合 EXPLAIN_TOPIC_V2。"""
    for name in explain.TOPICS:
        explain.set_global_flags({"json": True})
        explain.cmd_explain([name], backend, config)
        env = json.loads(capsys.readouterr().out)
        _validate(EXPLAIN_TOPIC_V2, env["data"])


def test_commands_data_schema(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_commands([], backend, config)
    env = json.loads(capsys.readouterr().out)
    _validate(COMMANDS_V2, env["data"])


def test_doctor_data_schema(backend, config, capsys, monkeypatch):
    monkeypatch.setattr("proxyctl.cli.service_running", lambda *a, **k: False)
    monkeypatch.setattr(explain, "_tcp_open", lambda *a, **k: False)
    monkeypatch.setattr(explain, "_dns_points_to_loopback", lambda: False)
    monkeypatch.setattr(explain, "_system_proxy_points_to_loopback", lambda p: False)
    monkeypatch.setattr(explain, "_quick_connectivity", lambda *a: False)

    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit):
        explain.cmd_doctor([], backend, config)
    env = json.loads(capsys.readouterr().out)
    _validate(ENVELOPE_V2, env)
    _validate(DOCTOR_V2, env["data"])


def test_config_set_envelope_includes_old_new_value(backend, config, capsys,
                                                     tmp_path, monkeypatch):
    monkeypatch.setattr(explain, "_io_proxyctl_config_path",
                        lambda: str(tmp_path / "config.yaml"))
    explain.set_global_flags({"json": True})
    explain.cmd_config(["set", "proxy_port", "7899"], backend, config)
    env = json.loads(capsys.readouterr().out)
    _validate(ENVELOPE_V2, env)
    _validate(CONFIG_SET_V2, env["data"])
    assert env["data"]["new_value"] == 7899


# ── _io envelope helper 也要符合（防退化）────────────────────────────────
def test_io_envelope_default_matches_schema():
    from proxyctl import _io
    env = _io.envelope("foo", data={"bar": 1})
    _validate(ENVELOPE_V2, env)


def test_io_envelope_failure_matches_schema():
    from proxyctl import _io
    env = _io.envelope("foo", ok=False, error="boom",
                       code=_io.NOT_FOUND, hint="h", doc="d")
    _validate(ENVELOPE_V2, env)
