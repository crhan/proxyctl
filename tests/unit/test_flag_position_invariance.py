"""测试 clig.dev "flag 位置无关" 原则：

  `proxyctl --json cmd args` 与 `proxyctl cmd --json args` 与 `proxyctl cmd args --json`
  应产生相同行为。
"""

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


def _normalize_envelope_for_compare(out: str) -> dict:
    """剥离 envelope.meta（含 ts/elapsed_ms/request_id 易变字段）后比较。"""
    obj = json.loads(out)
    obj.pop("meta", None)
    return obj


# ── --json 位置无关 ──────────────────────────────────────────────────────
def test_version_json_position_invariant():
    a, _, ca = _run_capture(["proxyctl", "--version", "--json"])
    b, _, cb = _run_capture(["proxyctl", "--json", "--version"])
    assert ca == cb == 0
    assert _normalize_envelope_for_compare(a) == _normalize_envelope_for_compare(b)


# ── --dry-run 位置无关 ───────────────────────────────────────────────────
def test_dry_run_position_invariant_for_mode():
    a, _, ca = _run_capture(["proxyctl", "mode", "tun", "--dry-run", "--json"])
    b, _, cb = _run_capture(["proxyctl", "--dry-run", "--json", "mode", "tun"])
    c, _, cc = _run_capture(["proxyctl", "--json", "mode", "--dry-run", "tun"])
    assert ca == cb == cc == 0
    assert (_normalize_envelope_for_compare(a)
            == _normalize_envelope_for_compare(b)
            == _normalize_envelope_for_compare(c))


def test_dry_run_position_invariant_for_engine():
    a, _, ca = _run_capture(
        ["proxyctl", "engine", "mihomo", "--dry-run", "--json"])
    b, _, cb = _run_capture(
        ["proxyctl", "--dry-run", "engine", "--json", "mihomo"])
    assert ca == cb == 0
    assert (_normalize_envelope_for_compare(a)
            == _normalize_envelope_for_compare(b))


def test_dry_run_position_invariant_for_audit_apply():
    a, _, ca = _run_capture(
        ["proxyctl", "audit", "apply", "--dry-run", "--json"])
    b, _, cb = _run_capture(
        ["proxyctl", "--json", "audit", "--dry-run", "apply"])
    assert ca == cb == 0
    assert (_normalize_envelope_for_compare(a)
            == _normalize_envelope_for_compare(b))


# ── 子命令 flag 位置无关（log --tail / --no-follow）───────────────────────
def test_log_tail_position_invariant(monkeypatch, tmp_path):
    """log --tail N --no-follow ≡ log --no-follow --tail N"""
    # 准备一个假日志文件
    log = tmp_path / "fake.log"
    log.write_text("line 1\nline 2\nline 3\n")

    # 让 cmd_log 用我们的临时文件
    monkeypatch.setattr("proxyctl.engine.mihomo.MihomoBackend.log_file",
                        str(log))

    a, _, ca = _run_capture(
        ["proxyctl", "log", "--tail", "2", "--no-follow", "--json"])
    b, _, cb = _run_capture(
        ["proxyctl", "log", "--no-follow", "--tail", "2", "--json"])
    c, _, cc = _run_capture(
        ["proxyctl", "--json", "log", "--no-follow", "--tail", "2"])
    assert ca == cb == cc == 0
    assert a == b == c  # NDJSON 完全一致


def test_log_tail_value_picked_up_regardless_of_position(monkeypatch, tmp_path):
    log = tmp_path / "fake.log"
    log.write_text("a\nb\nc\nd\ne\n")
    monkeypatch.setattr("proxyctl.engine.mihomo.MihomoBackend.log_file",
                        str(log))

    # NDJSON v2：首行 meta header + N 数据行 = N+1 行
    # 在 --json 之前
    a, _, _ = _run_capture(
        ["proxyctl", "log", "--tail", "3", "--no-follow", "--json"])
    lines_a = [ln for ln in a.split("\n") if ln.strip()]
    assert len(lines_a) == 4  # 1 header + 3 data
    # 在 --json 之后
    b, _, _ = _run_capture(
        ["proxyctl", "log", "--json", "--no-follow", "--tail", "3"])
    lines_b = [ln for ln in b.split("\n") if ln.strip()]
    assert len(lines_b) == 4


# ── 全局 flag 都接受 ──────────────────────────────────────────────────────
def test_no_color_flag_position_invariant():
    """--no-color 在任何位置都被识别。"""
    a, _, ca = _run_capture(["proxyctl", "--no-color", "--version"])
    b, _, cb = _run_capture(["proxyctl", "--version", "--no-color"])
    assert ca == cb == 0
    assert a == b


# ── version-features ─────────────────────────────────────────────────────
def test_version_features_flag_position_invariant_true():
    out, _, _ = _run_capture(["proxyctl", "--version", "--json"])
    feat = json.loads(out)["data"]["supported_features"]
    assert feat["flag_position_invariant"] is True
