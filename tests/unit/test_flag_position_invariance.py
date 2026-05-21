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


def test_short_dry_run_flag_matches_long_form_for_mode():
    """-n is the clig.dev-friendly short alias for --dry-run."""
    long_out, _, long_code = _run_capture(["proxyctl", "mode", "tun", "--dry-run", "--json"])
    short_out, _, short_code = _run_capture(["proxyctl", "mode", "tun", "-n", "--json"])
    assert long_code == short_code == 0
    assert _normalize_envelope_for_compare(long_out) == _normalize_envelope_for_compare(short_out)


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

    # 让 cmd_log 用我们的临时文件。
    # 注意：cli.main 实际用 proxyctl.cli.MihomoBackend（cli.py:115 的实现），
    # 不是 proxyctl.engine.mihomo.MihomoBackend（engine 模块那份是双胞胎、
    # 当前未在 main 路径使用）。给错类会导致测试只在本机偶然 PASS
    # （依赖 ~/.config/mihomo/mihomo.log 真实存在）、在 CI 上挂。
    monkeypatch.setattr("proxyctl.cli.MihomoBackend.log_file",
                        str(log))

    a, _, ca = _run_capture(
        ["proxyctl", "log", "--tail", "2", "--no-follow", "--json"])
    b, _, cb = _run_capture(
        ["proxyctl", "log", "--no-follow", "--tail", "2", "--json"])
    c, _, cc = _run_capture(
        ["proxyctl", "--json", "log", "--no-follow", "--tail", "2"])
    assert ca == cb == cc == 0
    assert a == b == c  # NDJSON 完全一致


def test_log_tail_monkeypatch_targets_correct_class_no_real_logfile_required(
    monkeypatch, tmp_path
):
    """v0.4.3 回归：测试不能依赖 ~/.config/mihomo/mihomo.log 真实存在。

    历史 bug：测试 monkeypatch 错的类（engine.mihomo.MihomoBackend），
    本机有真 log 文件兜底所以通过，CI 没有就挂。修法是改 patch 到
    cli.MihomoBackend。本测试通过指向不存在的目录确保 patch 真生效。
    """
    log = tmp_path / "doesnt_exist_yet" / "fake.log"
    log.parent.mkdir()
    log.write_text("x\ny\nz\n")
    monkeypatch.setattr("proxyctl.cli.MihomoBackend.log_file", str(log))

    a, _, ca = _run_capture(
        ["proxyctl", "log", "--tail", "2", "--no-follow", "--json"])
    assert ca == 0
    # 必含 fake.log 路径作为 source（证明 patch 生效），且不报"日志文件不存在"
    assert "doesnt_exist_yet" in a
    assert "日志文件不存在" not in a


def test_log_tail_value_picked_up_regardless_of_position(monkeypatch, tmp_path):
    log = tmp_path / "fake.log"
    log.write_text("a\nb\nc\nd\ne\n")
    monkeypatch.setattr("proxyctl.cli.MihomoBackend.log_file",
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
