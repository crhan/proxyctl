"""测试 proxyctl commands --schema 输出符合 JSON Schema Draft 2020-12，
且能用来 validate proxyctl commands --json 的 data。"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest
from jsonschema import Draft202012Validator

from proxyctl import cli, _io, explain


def _run_capture(argv: list[str]) -> tuple[str, str, int]:
    _io.set_no_color(True)
    _io._JSON_MODE = False  # type: ignore[attr-defined]
    _io._T0_NS = None  # type: ignore[attr-defined]
    _io._REQUEST_ID = None  # type: ignore[attr-defined]
    out, err = io.StringIO(), io.StringIO()
    code = 0
    sys.argv = argv
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cli.main()
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    finally:
        # 清掉 explain 的全局 flag 共享，避免跨测试污染（cli.main 通过
        # explain.set_global_flags 把 GLOBAL_FLAGS 注入到 explain 模块）
        explain.set_global_flags(
            {"json": False, "no_color": False, "quiet": False,
             "dry_run": False, "plain": False})
    return out.getvalue(), err.getvalue(), code


def test_commands_schema_subcommand_outputs_valid_json_schema():
    out, _, code = _run_capture(["proxyctl", "commands", "--schema"])
    assert code == 0
    schema = json.loads(out)
    # 应该是合法的 Draft 2020-12 schema
    Draft202012Validator.check_schema(schema)
    # 必含关键字
    assert schema["type"] == "object"
    assert "required" in schema
    assert "schema_version" in schema["required"]
    assert "commands" in schema["required"]


def test_commands_schema_via_json_envelope():
    """`--json commands --schema` 输出 envelope，data 是 schema 自身。"""
    out, _, code = _run_capture(
        ["proxyctl", "--json", "commands", "--schema"])
    assert code == 0
    obj = json.loads(out)
    assert obj["schema_version"] == _io.SCHEMA_VERSION
    assert obj["cmd"] == "commands"
    Draft202012Validator.check_schema(obj["data"])


def test_commands_json_validates_against_commands_schema():
    """实际的 commands --json 输出必须能通过 commands --schema 的校验。"""
    schema_out, _, _ = _run_capture(["proxyctl", "commands", "--schema"])
    schema = json.loads(schema_out)
    data_out, _, _ = _run_capture(["proxyctl", "--json", "commands"])
    env = json.loads(data_out)
    # data 段 vs schema
    Draft202012Validator(schema).validate(env["data"])


def test_commands_schema_includes_side_effect_enum():
    """schema 必须把 side_effects 锁定为已知 5 元素枚举。"""
    out, _, _ = _run_capture(["proxyctl", "commands", "--schema"])
    schema = json.loads(out)
    items = schema["properties"]["commands"]["items"]
    se = items["properties"]["side_effects"]
    assert se["type"] == "array"
    enum_values = set(se["items"]["enum"])
    assert enum_values == set(explain.SIDE_EFFECT_ENUM)


def test_version_features_commands_schema_true():
    out, _, _ = _run_capture(["proxyctl", "--version", "--json"])
    feat = json.loads(out)["data"]["supported_features"]
    assert feat["commands_schema"] is True


def test_version_features_doctor_extended_true():
    out, _, _ = _run_capture(["proxyctl", "--version", "--json"])
    feat = json.loads(out)["data"]["supported_features"]
    assert feat["doctor_extended"] is True


def test_version_features_log_ndjson_v2_true():
    out, _, _ = _run_capture(["proxyctl", "--version", "--json"])
    feat = json.loads(out)["data"]["supported_features"]
    assert feat["log_ndjson_v2"] is True


def test_new_explain_topics_registered():
    """0.3.0 新 topics 必须存在。"""
    for t in ("subscription", "agent-protocol", "locks", "flags"):
        assert t in explain.TOPICS, f"explain topic 缺：{t}"


def test_new_topics_emit_valid_envelope():
    for t in ("subscription", "agent-protocol", "locks", "flags"):
        out, _, code = _run_capture(["proxyctl", "--json", "explain", t])
        assert code == 0, f"explain {t} 失败"
        obj = json.loads(out)
        assert obj["data"]["topic"] == t
        # next_commands 字段必须用新名（不再用 next）
        nxt = obj["data"].get("next_commands")
        assert isinstance(nxt, list)


def test_all_topics_use_next_commands_field():
    """所有 topic 卡片的 next 字段都应改为 next_commands（0.3.0 重命名）。"""
    for name in explain.TOPICS:
        out, _, _ = _run_capture(["proxyctl", "--json", "explain", name])
        obj = json.loads(out)
        # 允许 next_commands 缺失（不是所有 topic 都给 next）
        # 但不允许仍出现旧 "next" 键
        assert "next" not in obj["data"], \
            f"topic {name!r} 仍含旧字段 'next'，应改为 'next_commands'"
