"""测试 proxyctl._io：退出码 / 颜色 / envelope / fail / with_lock / 信号。"""

from __future__ import annotations

import io
import json
import sys

import pytest

from proxyctl import _io


# ── 退出码 ─────────────────────────────────────────────────────────────────
def test_exit_code_constants():
    """语义码必须存在且唯一。"""
    codes = [_io.OK, _io.GENERIC, _io.USAGE, _io.NOT_FOUND, _io.PERMISSION,
             _io.ENGINE_DOWN, _io.CONFIG_ERR, _io.NETWORK_ERR, _io.LOCKED]
    assert codes == [0, 1, 2, 3, 4, 5, 6, 7, 8]
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


# ── envelope / emit_json ───────────────────────────────────────────────────
def test_envelope_shape_default():
    env = _io.envelope("status", data={"engine": "mihomo"})
    assert env["schema_version"] == 1
    assert env["cmd"] == "status"
    assert env["ok"] is True
    assert env["data"] == {"engine": "mihomo"}
    assert env["error"] is None
    assert env["code"] == _io.OK
    assert env["hint"] is None
    assert env["doc"] is None


def test_envelope_failure_shape():
    env = _io.envelope("trace", ok=False, error="boom",
                       code=_io.NETWORK_ERR, hint="check internet",
                       doc="troubleshooting")
    assert env["ok"] is False
    assert env["error"] == "boom"
    assert env["code"] == 7
    assert env["hint"] == "check internet"
    assert env["doc"] == "troubleshooting"


def test_emit_json_writes_to_stdout(capsys):
    _io.emit_json(_io.envelope("foo", data={"a": 1}))
    out = capsys.readouterr().out
    # 必须是合法 JSON
    obj = json.loads(out)
    assert obj["cmd"] == "foo"
    assert obj["data"]["a"] == 1


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


def test_fail_json_mode_emits_envelope_to_stdout(capsys):
    with pytest.raises(SystemExit) as exc:
        _io.fail("nope", hint="h", code=_io.NOT_FOUND,
                 cmd="config", as_json=True)
    assert exc.value.code == _io.NOT_FOUND
    cap = capsys.readouterr()
    obj = json.loads(cap.out)
    assert obj["ok"] is False
    assert obj["error"] == "nope"
    assert obj["code"] == _io.NOT_FOUND
    assert obj["cmd"] == "config"
    # 同时人类摘要也写到 stderr
    assert "nope" in cap.err


# ── with_lock 并发 ────────────────────────────────────────────────────────
def test_with_lock_grants_exclusive(tmp_path):
    """同一进程内串行获取应该 OK；持锁同时再次申请应 BlockingIOError。"""
    with _io.with_lock("testlock", lock_dir=str(tmp_path)):
        with pytest.raises(BlockingIOError):
            with _io.with_lock("testlock", lock_dir=str(tmp_path)):
                pass


def test_with_lock_released_after_with(tmp_path):
    with _io.with_lock("foo", lock_dir=str(tmp_path)):
        pass
    # 出 with 后应能再次拿
    with _io.with_lock("foo", lock_dir=str(tmp_path)):
        pass


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
