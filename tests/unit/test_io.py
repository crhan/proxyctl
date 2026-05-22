"""测试 proxyctl._io：退出码 / 颜色 / envelope v2 / fail / with_lock / 信号 / extract_flags / emit_tsv。"""

from __future__ import annotations

import io
import json
import re
import sys

import pytest

from proxyctl import _io


# ── 退出码 ─────────────────────────────────────────────────────────────────
def test_exit_code_constants():
    """语义码必须存在且唯一。"""
    codes = [_io.OK, _io.GENERIC, _io.USAGE, _io.NOT_FOUND, _io.PERMISSION,
             _io.ENGINE_DOWN, _io.CONFIG_ERR, _io.NETWORK_ERR, _io.LOCKED,
             _io.TIMEOUT, _io.DEPENDENCY_MISSING]
    assert codes == list(range(0, 11))
    assert len(set(codes)) == len(codes)
    for c in codes:
        assert c in _io.EXIT_CODE_HELP


# ── 颜色决策 ───────────────────────────────────────────────────────────────
def test_should_color_off_when_no_color_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert _io.should_color() is False


def test_should_color_off_when_term_dumb(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert _io.should_color() is False


def test_should_color_off_when_proxyctl_no_color_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("PROXYCTL_NO_COLOR", "1")
    assert _io.should_color() is False


def test_should_color_off_when_not_tty(monkeypatch, capsys):
    """stdout 是非 TTY 的 pytest 捕获流 → 自动关色。"""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PROXYCTL_NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert _io.should_color() is False


def test_set_no_color_patches_existing_modules(monkeypatch):
    """set_no_color(True) 应抹掉已加载的 RED/GREEN 等模块常量。"""
    from proxyctl import cli
    monkeypatch.setattr(cli, "RED", "\033[0;31m")
    monkeypatch.setattr(cli, "NC", "\033[0m")
    _io.set_no_color(True)
    try:
        assert cli.RED == ""
        assert cli.NC == ""
    finally:
        _io.set_no_color(False)  # 恢复供其它测试


# ── envelope v2 ───────────────────────────────────────────────────────────
def test_envelope_v2_shape_default():
    env = _io.envelope("status", data={"engine": "mihomo"})
    assert env["schema_version"] == 2
    assert env["cmd"] == "status"
    assert env["ok"] is True
    assert env["data"] == {"engine": "mihomo"}
    assert env["error"] is None
    assert env["code"] == _io.OK
    assert env["hints"] == []
    assert env["warnings"] == []
    assert env["doc"] is None
    assert "hint" not in env, "v2 no longer carries singular `hint` field"
    meta = env["meta"]
    assert isinstance(meta["ts"], str)
    assert meta["ts"].endswith("Z")
    assert "proxyctl_version" in meta
    assert isinstance(meta["request_id"], str) and len(meta["request_id"]) >= 8


def test_envelope_v2_failure_shape():
    env = _io.envelope("trace", ok=False, error="boom",
                       code=_io.NETWORK_ERR, hint="check internet",
                       doc="troubleshooting")
    assert env["ok"] is False
    assert env["error"] == "boom"
    assert env["code"] == 7
    assert env["hints"] == ["check internet"]  # `hint=` 自动包装
    assert env["doc"] == "troubleshooting"


def test_envelope_v2_multiple_hints_and_warnings():
    env = _io.envelope("audit", ok=False, error="x",
                       hints=["try a", "try b"], warnings=["W1", "W2"])
    assert env["hints"] == ["try a", "try b"]
    assert env["warnings"] == ["W1", "W2"]


def test_envelope_meta_elapsed_ms_when_t0_set(monkeypatch):
    _io.set_invocation_t0(0)  # 起点为 0
    try:
        env = _io.envelope("foo")
        # elapsed_ms 应为正整数（自 0 起的 monotonic_ns 转 ms）
        assert isinstance(env["meta"]["elapsed_ms"], int)
        assert env["meta"]["elapsed_ms"] >= 0
    finally:
        # 重置避免影响其他测试
        _io._T0_NS = None  # type: ignore[attr-defined]


def test_emit_json_writes_to_stdout(capsys):
    _io.emit_json(_io.envelope("foo", data={"a": 1}))
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["cmd"] == "foo"
    assert obj["data"]["a"] == 1
    assert obj["schema_version"] == 2


# ── set_json_mode / is_json_mode ──────────────────────────────────────────
def test_json_mode_getter_setter():
    _io.set_json_mode(False)
    assert _io.is_json_mode() is False
    _io.set_json_mode(True)
    assert _io.is_json_mode() is True
    _io.set_json_mode(False)


# ── request_id 锁定 ───────────────────────────────────────────────────────
def test_new_request_id_is_stable(monkeypatch):
    monkeypatch.setattr(_io, "_REQUEST_ID", None)
    rid1 = _io.new_request_id()
    rid2 = _io.new_request_id()
    assert rid1 == rid2
    assert len(rid1) >= 16


# ── fail() ────────────────────────────────────────────────────────────────
def test_fail_writes_to_stderr_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        _io.fail("oops", hint="try again", doc="troubleshooting",
                 code=_io.USAGE)
    assert exc.value.code == _io.USAGE
    err = capsys.readouterr().err
    assert "ERROR" in err
    assert "oops" in err
    assert "try again" in err
    assert "proxyctl explain troubleshooting" in err


def test_fail_writes_multiple_hints_and_warnings(capsys):
    with pytest.raises(SystemExit):
        _io.fail("nope", hints=["h1", "h2"], warnings=["w1"],
                 code=_io.USAGE)
    err = capsys.readouterr().err
    assert "h1" in err and "h2" in err
    assert "w1" in err


def test_fail_json_mode_emits_envelope_v2(capsys):
    with pytest.raises(SystemExit) as exc:
        _io.fail("nope", hint="h", code=_io.NOT_FOUND,
                 cmd="config", as_json=True)
    assert exc.value.code == _io.NOT_FOUND
    cap = capsys.readouterr()
    obj = json.loads(cap.out)
    assert obj["schema_version"] == 2
    assert obj["ok"] is False
    assert obj["error"] == "nope"
    assert obj["code"] == _io.NOT_FOUND
    assert obj["cmd"] == "config"
    assert obj["hints"] == ["h"]
    # 同时人类摘要也写到 stderr
    assert "nope" in cap.err


def test_fail_uses_global_json_mode_when_no_explicit_flag(capsys):
    _io.set_json_mode(True)
    try:
        with pytest.raises(SystemExit):
            _io.fail("auto", code=_io.USAGE, cmd="x")
        cap = capsys.readouterr()
        obj = json.loads(cap.out)
        assert obj["ok"] is False
        assert obj["error"] == "auto"
    finally:
        _io.set_json_mode(False)


# ── extract_flags ─────────────────────────────────────────────────────────
def test_extract_flags_bool():
    args, flags = _io.extract_flags(
        ["a", "--no-follow", "b"],
        known={"--no-follow": "bool"})
    assert args == ["a", "b"]
    assert flags == {"no_follow": True}


def test_extract_flags_value():
    args, flags = _io.extract_flags(
        ["log", "--tail", "50"],
        known={"--tail": "value"})
    assert args == ["log"]
    assert flags == {"tail": "50"}


def test_extract_flags_mixed_position():
    args, flags = _io.extract_flags(
        ["--tail", "50", "log", "--no-follow", "extra"],
        known={"--tail": "value", "--no-follow": "bool"})
    assert args == ["log", "extra"]
    assert flags == {"tail": "50", "no_follow": True}


def test_extract_flags_unknown_passed_through():
    args, flags = _io.extract_flags(
        ["--unknown", "x", "--tail", "9"],
        known={"--tail": "value"})
    assert args == ["--unknown", "x"]
    assert flags == {"tail": "9"}


def test_extract_flags_missing_value_at_eol():
    args, flags = _io.extract_flags(
        ["log", "--tail"],
        known={"--tail": "value"})
    assert args == ["log"]
    assert flags == {"tail": None}


# ── emit_tsv ──────────────────────────────────────────────────────────────
def test_emit_tsv_header_and_rows(capsys):
    _io.emit_tsv(
        [{"a": 1, "b": "x"}, {"a": 2, "b": "y\tz"}],
        cols=["a", "b"])
    out = capsys.readouterr().out
    lines = out.strip().split("\n")
    assert lines[0] == "a\tb"
    assert lines[1] == "1\tx"
    assert lines[2] == "2\ty z"  # tab 被替换成空格


def test_emit_tsv_no_ansi_no_box():
    """--plain 输出永远不带 ANSI / box-drawing。"""
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _io.emit_tsv([{"a": "data"}], cols=["a"])
    out = buf.getvalue()
    assert "\x1b[" not in out
    assert not any(c in out for c in "┌┐└┘─│")


# ── with_lock 并发 ────────────────────────────────────────────────────────
def test_with_lock_grants_exclusive(tmp_path):
    """同一进程内串行获取应该 OK；持锁同时再次申请应 LockedError。"""
    with _io.with_lock("testlock", lock_dir=str(tmp_path)):
        with pytest.raises(_io.LockedError) as exc:
            with _io.with_lock("testlock", lock_dir=str(tmp_path)):
                pass
        assert exc.value.lock_path.endswith(".lock.testlock")
        assert exc.value.lock_name == "testlock"


def test_with_lock_released_after_with(tmp_path):
    with _io.with_lock("foo", lock_dir=str(tmp_path)):
        pass
    with _io.with_lock("foo", lock_dir=str(tmp_path)):
        pass


def test_lock_paths_includes_known_names(tmp_path):
    paths = _io.lock_paths(lock_dir=str(tmp_path))
    assert set(paths) >= {"system", "config", "daemon", "traffic"}
    for name, path in paths.items():
        assert path.endswith(f".lock.{name}")


def test_held_lock_names_empty_when_none(tmp_path):
    assert _io.held_lock_names(lock_dir=str(tmp_path)) == []


def test_held_lock_names_detects_held(tmp_path):
    with _io.with_lock("hot", lock_dir=str(tmp_path)):
        held = _io.held_lock_names(lock_dir=str(tmp_path))
        assert "hot" in held


# ── agent 模式 ────────────────────────────────────────────────────────────
def test_agent_mode_active(monkeypatch):
    monkeypatch.delenv("PROXYCTL_AGENT", raising=False)
    assert _io.agent_mode_active() is False
    monkeypatch.setenv("PROXYCTL_AGENT", "1")
    assert _io.agent_mode_active() is True
    monkeypatch.setenv("PROXYCTL_AGENT", "TRUE")
    assert _io.agent_mode_active() is True
    monkeypatch.setenv("PROXYCTL_AGENT", "")
    assert _io.agent_mode_active() is False


# ── 信号 hook（不抛即可）──────────────────────────────────────────────────
def test_install_signal_handlers_does_not_raise():
    _io.install_signal_handlers()
