"""测试 `proxyctl help` 和 `proxyctl help <cmd>` 子命令分发，
以及它们与 `--help` / `<cmd> --help` 完全等价。"""

from __future__ import annotations

import io
import re
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

from proxyctl import cli, _io


def _run_capture(argv: list[str]) -> tuple[str, str, int]:
    """运行 cli.main()，捕获 stdout/stderr/exit_code。"""
    _io.set_no_color(True)
    # 重置全局调用状态，避免测试间互染
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


# ── 顶层 help ─────────────────────────────────────────────────────────────
def test_top_level_help_succeeds():
    """proxyctl --help → exit 0，输出含 AGENT / 用法 / 命令分组。"""
    out, _, code = _run_capture(["proxyctl", "--help"])
    assert code == 0
    assert "AGENT 接入" in out
    assert "proxyctl agent-guide" in out
    assert "proxyctl commands --json" in out
    assert "用法" in out


def test_help_no_args_equals_dash_help():
    """proxyctl help ≡ proxyctl --help（同一函数）。"""
    out1, _, c1 = _run_capture(["proxyctl", "help"])
    out2, _, c2 = _run_capture(["proxyctl", "--help"])
    assert c1 == c2 == 0
    assert out1 == out2


def test_help_h_equals_dash_help():
    """proxyctl -h ≡ proxyctl --help。"""
    out1, _, c1 = _run_capture(["proxyctl", "-h"])
    out2, _, c2 = _run_capture(["proxyctl", "--help"])
    assert c1 == c2 == 0
    assert out1 == out2


def test_help_lists_all_groups():
    """顶层 help 必须列出所有 group 名（lifecycle / diagnostic / config / ...）。"""
    out, _, _ = _run_capture(["proxyctl", "--help"])
    for group in ("lifecycle", "diagnostic", "config", "maintenance",
                  "daemon", "tool", "agent"):
        assert group in out, f"--help 缺少 group: {group}"


def test_help_lists_core_commands():
    """顶层 help 必须列出每个 COMMANDS_META 命令。"""
    from proxyctl import explain
    out, _, _ = _run_capture(["proxyctl", "--help"])
    for c in explain.COMMANDS_META:
        # 命令名应作为单词出现（前后空格/可打印边界）
        assert c["name"] in out, f"--help 缺少命令: {c['name']}"


def test_help_includes_env_vars():
    """顶层 help 必须列出 PROXYCTL_AGENT / NO_COLOR 等环境变量。"""
    out, _, _ = _run_capture(["proxyctl", "--help"])
    assert "PROXYCTL_AGENT" in out
    assert "NO_COLOR" in out


def test_help_includes_global_flags():
    """顶层 help 必须文档化 --json / --plain / --dry-run / --no-color / --quiet。"""
    out, _, _ = _run_capture(["proxyctl", "--help"])
    for flag in ("--json", "--plain", "--dry-run", "--no-color", "--quiet"):
        assert flag in out, f"--help 缺少 flag: {flag}"


# ── 单命令 help ───────────────────────────────────────────────────────────
def test_subcommand_help_via_help_cmd():
    """proxyctl help mode → 输出 mode 元数据 + 用法 + 示例。"""
    out, _, code = _run_capture(["proxyctl", "help", "mode"])
    assert code == 0
    assert "proxyctl mode" in out
    assert "用法" in out


def test_subcommand_help_via_dash_help_equals_help_cmd():
    """proxyctl mode --help ≡ proxyctl help mode（同一函数渲染）。"""
    out1, _, c1 = _run_capture(["proxyctl", "help", "mode"])
    out2, _, c2 = _run_capture(["proxyctl", "mode", "--help"])
    assert c1 == c2 == 0
    assert out1 == out2


def test_subcommand_help_unknown_returns_usage():
    """proxyctl help nope → USAGE(2) + did-you-mean。"""
    out, err, code = _run_capture(["proxyctl", "help", "nopecommand"])
    assert code == 2
    assert "未识别子命令" in err
    assert "proxyctl commands" in err  # hint


def test_subcommand_help_json_outputs_envelope():
    """proxyctl --json help mode → envelope，data=meta。"""
    import json
    out, _, code = _run_capture(["proxyctl", "--json", "help", "mode"])
    assert code == 0
    obj = json.loads(out)
    assert obj["schema_version"] == 2
    assert obj["cmd"] == "mode"
    assert obj["data"]["name"] == "mode"
    assert obj["data"]["group"] == "config"


def test_subcommand_help_for_all_meta_commands():
    """每个 COMMANDS_META 命令都能成功被 `help <name>` 渲染。"""
    from proxyctl import explain
    for c in explain.COMMANDS_META:
        out, err, code = _run_capture(["proxyctl", "help", c["name"]])
        assert code == 0, \
            f"help {c['name']} failed: code={code} err={err!r}"
        assert c["name"] in out, f"help {c['name']} 输出缺命令名"
