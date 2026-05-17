"""测试 `proxyctl --version` 与 `proxyctl --version --json` 的输出。"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

from proxyctl import cli, _io


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
    return out.getvalue(), err.getvalue(), code


def test_version_human_one_line():
    out, _, code = _run_capture(["proxyctl", "--version"])
    assert code == 0
    assert out.strip().startswith("proxyctl v")


def test_version_dash_v_alias():
    out, _, code = _run_capture(["proxyctl", "-v"])
    assert code == 0
    assert out.strip().startswith("proxyctl v")


def test_version_json_envelope():
    out, _, code = _run_capture(["proxyctl", "--version", "--json"])
    assert code == 0
    obj = json.loads(out)
    assert obj["schema_version"] == 2
    assert obj["cmd"] == "version"
    assert obj["ok"] is True
    data = obj["data"]
    assert data["version"].startswith("0.")
    assert data["schema_version"] == 2
    assert "python" in data
    assert "platform" in data
    assert "supported_features" in data


def test_version_json_supported_features_known_keys():
    """supported_features 必须包含已稳定的能力 keys；agent 用它探测。"""
    out, _, _ = _run_capture(["proxyctl", "--version", "--json"])
    data = json.loads(out)["data"]
    feat = data["supported_features"]
    # 这些 keys 在 0.3.0 必须存在（值可能 false）
    required_keys = {
        "envelope_v2", "agent_guide", "commands_json", "explain",
        "version_json", "discovery_envelope", "help_subcommand",
        "exit_codes_extended", "did_you_mean", "lock_path_in_error",
        "side_effects_enum", "dry_run", "plain",
        "flag_position_invariant", "commands_schema", "doctor_extended",
        "doctor_healthy_field", "log_ndjson_v2", "agents_md",
    }
    missing = required_keys - set(feat)
    assert not missing, f"supported_features 缺少 keys: {missing}"


def test_version_json_features_are_booleans():
    out, _, _ = _run_capture(["proxyctl", "--version", "--json"])
    feat = json.loads(out)["data"]["supported_features"]
    for k, v in feat.items():
        assert isinstance(v, bool), f"supported_features[{k!r}]={v!r} 不是 bool"


def test_version_json_phase1_features_are_true():
    """Phase 1（PR-1~4）已实施的能力必须 true。"""
    out, _, _ = _run_capture(["proxyctl", "--version", "--json"])
    feat = json.loads(out)["data"]["supported_features"]
    for k in ("envelope_v2", "version_json", "discovery_envelope",
              "help_subcommand", "exit_codes_extended",
              "did_you_mean", "lock_path_in_error"):
        assert feat[k] is True, f"{k} 应已实施 (True)"
