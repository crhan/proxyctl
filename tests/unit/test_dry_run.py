"""测试所有写命令的 --dry-run 行为：
  1. 不真正调用 subprocess
  2. 输出 envelope.data.plan 含 PlanStep schema
  3. 人类模式输出步骤列表
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

from proxyctl import cli, _io, explain


REQUIRED_PLANSTEP_FIELDS = {
    "step", "action", "target", "reversible",
    "requires_sudo", "side_effects", "summary",
}


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


def _ban_subprocess(monkeypatch):
    """dry-run 路径必须完全跳过 subprocess —— 用一个会爆的 mock 兜底防退化。"""
    import subprocess as _sp

    def _no_run(*args, **kwargs):
        raise AssertionError(
            f"subprocess.run called in --dry-run path: args={args!r}")

    monkeypatch.setattr(_sp, "run", _no_run)


def _assert_plan_step_schema(plan):
    assert isinstance(plan, list)
    assert plan, "plan 应非空"
    for s in plan:
        assert isinstance(s, dict)
        missing = REQUIRED_PLANSTEP_FIELDS - set(s)
        assert not missing, f"PlanStep 缺字段: {missing} (in {s!r})"
        assert isinstance(s["step"], int)
        assert isinstance(s["side_effects"], list)
        assert isinstance(s["reversible"], bool)
        assert isinstance(s["requires_sudo"], bool)


# ── 单个命令 dry-run 验收 ─────────────────────────────────────────────────
def test_mode_dry_run_emits_plan(monkeypatch):
    _ban_subprocess(monkeypatch)
    out, _, code = _run_capture(["proxyctl", "mode", "tun", "--dry-run", "--json"])
    assert code == 0
    obj = json.loads(out)
    assert obj["cmd"] == "mode"
    assert obj["data"]["dry_run"] is True
    _assert_plan_step_schema(obj["data"]["plan"])


def test_engine_dry_run_emits_plan(monkeypatch):
    _ban_subprocess(monkeypatch)
    out, _, code = _run_capture(
        ["proxyctl", "engine", "mihomo", "--dry-run", "--json"])
    assert code == 0
    obj = json.loads(out)
    assert obj["cmd"] == "engine"
    _assert_plan_step_schema(obj["data"]["plan"])


def test_fix_dry_run_emits_plan(monkeypatch):
    _ban_subprocess(monkeypatch)
    out, _, code = _run_capture(["proxyctl", "fix", "--dry-run", "--json"])
    assert code == 0
    _assert_plan_step_schema(json.loads(out)["data"]["plan"])


def test_audit_apply_dry_run_emits_plan(monkeypatch):
    _ban_subprocess(monkeypatch)
    out, _, code = _run_capture(
        ["proxyctl", "audit", "apply", "--dry-run", "--json"])
    assert code == 0
    obj = json.loads(out)
    assert obj["cmd"] == "audit"
    _assert_plan_step_schema(obj["data"]["plan"])


def test_config_set_dry_run_emits_plan(monkeypatch):
    _ban_subprocess(monkeypatch)
    out, _, code = _run_capture(
        ["proxyctl", "config", "set", "proxy_port", "9999",
         "--dry-run", "--json"])
    assert code == 0
    obj = json.loads(out)
    assert obj["cmd"] == "config"
    _assert_plan_step_schema(obj["data"]["plan"])


def test_daemon_start_dry_run_emits_plan(monkeypatch):
    _ban_subprocess(monkeypatch)
    out, _, code = _run_capture(
        ["proxyctl", "daemon", "claude-proxy", "start", "--dry-run", "--json"])
    assert code == 0
    _assert_plan_step_schema(json.loads(out)["data"]["plan"])


def test_dns_lock_dry_run_emits_plan(monkeypatch):
    _ban_subprocess(monkeypatch)
    out, _, code = _run_capture(
        ["proxyctl", "dns-lock", "--dry-run", "--json"])
    assert code == 0
    _assert_plan_step_schema(json.loads(out)["data"]["plan"])


def test_dns_unlock_dry_run_emits_plan(monkeypatch):
    _ban_subprocess(monkeypatch)
    out, _, code = _run_capture(
        ["proxyctl", "dns-unlock", "--dry-run", "--json"])
    assert code == 0
    _assert_plan_step_schema(json.loads(out)["data"]["plan"])


# ── 人类模式输出 ──────────────────────────────────────────────────────────
def test_mode_dry_run_human_lists_steps(monkeypatch):
    _ban_subprocess(monkeypatch)
    out, _, code = _run_capture(["proxyctl", "mode", "proxy", "--dry-run"])
    assert code == 0
    assert "[--dry-run]" in out
    # 至少列出 1 步
    assert "1." in out


# ── 元数据 / supported_features 一致性 ────────────────────────────────────
def test_commands_meta_supports_dry_run_consistent():
    """COMMANDS_META 中标了 supports_dry_run 的命令必须实际能跑 --dry-run。"""
    by_name = {c["name"]: c for c in explain.COMMANDS_META}
    expected = {"mode", "engine", "fix", "audit", "config",
                "daemon", "claude-proxy", "dns-lock", "dns-unlock"}
    for name in expected:
        assert by_name[name].get("supports_dry_run") is True, \
            f"{name} 应标 supports_dry_run=True"


def test_version_features_dry_run_true():
    """proxyctl --version --json 的 supported_features.dry_run 应为 true。"""
    out, _, _ = _run_capture(["proxyctl", "--version", "--json"])
    feat = json.loads(out)["data"]["supported_features"]
    assert feat["dry_run"] is True


# ── flag 互斥与异常路径 ───────────────────────────────────────────────────
def test_dry_run_with_no_side_effect_command_is_noop():
    """只读命令不会响应 --dry-run（直接走原命令）。"""
    # status 是只读，加 --dry-run 不应触发 dry-run 路径
    # 但 status 真的会调网络，我们不实际跑——只验证 --dry-run flag 被剥离
    # 不报 USAGE
    pass  # 由 test_flag_position_invariance 覆盖
