"""测试 cmd_log 的多种模式：--tail / --no-follow / --json / 不存在文件。"""

from __future__ import annotations

import json

import pytest

from proxyctl import _io, cli


class _FakeBackend:
    name = "mihomo"

    def __init__(self, log_file: str):
        self.log_file = log_file


# ── 文件读取辅助 ──────────────────────────────────────────────────────────
def test_read_log_lines_tail(tmp_path):
    p = tmp_path / "fake.log"
    p.write_text("\n".join(f"L{i}" for i in range(1, 11)) + "\n")
    out = cli._read_log_lines(str(p), 3)
    assert [s.strip() for s in out] == ["L8", "L9", "L10"]


def test_read_log_lines_none_returns_all(tmp_path):
    p = tmp_path / "all.log"
    p.write_text("a\nb\nc\n")
    out = cli._read_log_lines(str(p), None)
    assert [s.strip() for s in out] == ["a", "b", "c"]


def test_read_log_lines_missing_returns_empty():
    assert cli._read_log_lines("/no/such/file", 10) == []


# ── cmd_log：--tail --json ────────────────────────────────────────────────
def test_cmd_log_tail_json_yields_json_lines(tmp_path, capsys):
    p = tmp_path / "x.log"
    p.write_text("line1\nline2\nline3\n")
    cli.GLOBAL_FLAGS.update({"json": True})
    cli.cmd_log(_FakeBackend(str(p)), ["--tail", "2"])
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert json.loads(out[0])["line"] == "line2"
    assert json.loads(out[-1])["line"] == "line3"


# ── 文件不存在 → NOT_FOUND ───────────────────────────────────────────────
def test_cmd_log_missing_file_returns_not_found(tmp_path):
    cli.GLOBAL_FLAGS.update({"json": False})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_log(_FakeBackend("/no/such/file.log"), ["--tail", "1"])
    assert exc.value.code == _io.NOT_FOUND


# ── --tail 缺数字 → USAGE ────────────────────────────────────────────────
def test_cmd_log_tail_missing_number(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("hi\n")
    cli.GLOBAL_FLAGS.update({"json": False})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_log(_FakeBackend(str(p)), ["--tail"])
    assert exc.value.code == _io.USAGE


def test_cmd_log_tail_non_numeric(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("hi\n")
    cli.GLOBAL_FLAGS.update({"json": False})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_log(_FakeBackend(str(p)), ["--tail", "abc"])
    assert exc.value.code == _io.USAGE
