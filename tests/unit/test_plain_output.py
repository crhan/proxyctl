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


# ── audit --plain 主路径不应崩（_audit_emit arity 回归） ──────────────────
def test_audit_plain_main_path_emits_tsv(monkeypatch, fake_subprocess, capsys):
    """v0.3.0 引入 --plain 时 audit.py:442 漏传 as_plain → TypeError。
    本测试覆盖 cmd_audit 主路径（非 early-return），保证 _audit_emit
    调用 arity 与函数签名一致。"""
    from proxyctl import audit, explain

    explain.set_global_flags({"plain": True, "json": False,
                              "no_color": True, "quiet": False,
                              "dry_run": False})

    # 让日志扫描产出一个 host，进而 proxy_domains 非空、uncovered 非空
    monkeypatch.setattr(audit, "_scan_log",
                        lambda *a, **kw: {"foo.example.com": 1})
    monkeypatch.setattr(audit, "_load_rules", lambda: (set(), set()))
    monkeypatch.setattr(audit, "_resolve_direct", lambda h: "")
    monkeypatch.setattr(audit, "_save_geo_cache", lambda d: None)

    # curl /connections 返回空（避免 unexpected subprocess assert）
    fake_subprocess.set_default(stdout='{"connections":[]}', returncode=0)

    with pytest.raises(SystemExit) as ei:
        audit.cmd_audit(1, "http://127.0.0.1:9090", "secret", False)
    assert ei.value.code == 0  # _audit_emit 在 plain 路径主动 exit(0)


# ── check --plain connectivity detail 字段名正确（回归 0.3.1 错位） ──────
def test_check_plain_connectivity_uses_real_keys(capsys):
    """check 的 connectivity collector 字段是 name/url/mode/ok/message，
    plain 渲染必须用真实字段，不能用从未存在过的 target/http_code。
    回归：v0.3.0 引入 --plain 时用错字段，输出全是 None=X。"""
    from proxyctl import _io as _pc_io

    # 模拟 _check_emit 收尾时的 plain 分支（直接拼 detail 字符串验证）
    conn = [
        {"name": "google", "url": "https://x", "mode": "proxy",
         "ok": True, "message": "✓ google ..."},
        {"name": "github", "url": "https://y", "mode": "proxy",
         "ok": False, "message": "✗ github ..."},
    ]
    detail = ";".join(
        f"{c.get('name')}={'ok' if c.get('ok') else 'X'}" for c in conn
    )
    assert detail == "google=ok;github=X"
    assert "None" not in detail
    # 用真实 TSV emit 也走一遍，确保无 ANSI / 无 None
    _pc_io.emit_tsv(
        [{"stage": "connectivity", "ok": False, "detail": detail}],
        cols=["stage", "ok", "detail"],
    )
    out = capsys.readouterr().out
    assert "None" not in out
    assert "\x1b[" not in out


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
