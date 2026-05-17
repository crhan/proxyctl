"""测试 --plain TSV 输出：audit + check 命令，以及 --plain 与 --json 互斥。"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

from proxyctl import _io, explain


# ── emit_tsv 自测 ────────────────────────────────────────────────────────
def test_emit_tsv_no_ansi(capsys):
    _io.emit_tsv([{"a": "x", "b": "y"}], cols=["a", "b"])
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert "\t" in out
    lines = out.strip().split("\n")
    assert lines[0] == "a\tb"


def test_emit_tsv_no_box_drawing(capsys):
    _io.emit_tsv([{"x": 1, "y": "ok"}], cols=["x", "y"])
    out = capsys.readouterr().out
    forbidden = "┌┐└┘─│┃━╞╗╔╝╚"
    for ch in forbidden:
        assert ch not in out


def test_emit_tsv_replaces_tab_in_values(capsys):
    _io.emit_tsv([{"k": "a\tb"}], cols=["k"])
    out = capsys.readouterr().out
    # 第二行（数据）不应包含原始 tab（除作分隔符）
    data_line = out.strip().split("\n")[1]
    assert data_line == "a b"


def test_emit_tsv_handles_missing_keys(capsys):
    _io.emit_tsv([{"a": 1}], cols=["a", "b", "c"])
    out = capsys.readouterr().out
    # 不能 strip()，否则会吃掉行尾的 trailing tabs
    lines = out.split("\n")
    assert lines[0] == "a\tb\tc"
    assert lines[1] == "1\t\t"  # b/c 缺失 → 空字符串占位


# ── flags topic 注册 ──────────────────────────────────────────────────────
def test_explain_flags_topic_registered():
    assert "flags" in explain.TOPICS


def test_explain_flags_topic_content():
    explain.set_global_flags({"json": True})
    f = io.StringIO()
    with redirect_stdout(f):
        explain.cmd_explain(["flags"], backend=None, config={})
    import json
    obj = json.loads(f.getvalue())
    data = obj["data"]
    assert data["topic"] == "flags"
    assert "--plain" in data["edit"]
    assert "--dry-run" in data["edit"]
    assert "--json" in data["edit"]


# ── --plain 与 --json 互斥 ────────────────────────────────────────────────
def test_plain_and_json_are_mutually_exclusive_for_audit(monkeypatch, tmp_path):
    """audit --json --plain → USAGE(2)。"""
    from proxyctl import cli
    _io.set_no_color(True)
    _io._JSON_MODE = False  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "argv",
                        ["proxyctl", "audit", "--plain", "--json"])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        with pytest.raises(SystemExit) as ei:
            cli.main()
    assert ei.value.code == _io.USAGE
    # 错误消息（JSON envelope 或 stderr）含互斥提示
    combined = out.getvalue() + err.getvalue()
    assert "互斥" in combined


# ── --version --json supported_features.plain ────────────────────────────
def test_version_features_plain_true():
    from proxyctl import cli
    import json
    _io.set_no_color(True)
    _io._JSON_MODE = False  # type: ignore[attr-defined]
    _io._REQUEST_ID = None  # type: ignore[attr-defined]
    sys.argv = ["proxyctl", "--version", "--json"]
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            cli.main()
        except SystemExit:
            pass
    feat = json.loads(out.getvalue())["data"]["supported_features"]
    assert feat["plain"] is True
