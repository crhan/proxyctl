"""防退化：关色路径下任何命令的输出都不应包含 ANSI 转义序列。

这是行为测试，不是静态 grep。原因：每个模块都有 RED = '\\033[0;31m' 字面量
（这是合法的——_io.set_no_color 运行期会 monkey-patch 抹空），所以静态 grep
会大量误伤。直接断言"运行行为"更准确。

策略：
  1. 把 NO_COLOR=1 + sys.stdout 非 TTY → _io.set_no_color(True) 自动触发
  2. 跑一组只读命令，捕获 stdout/stderr
  3. 全部断言不含 '\\x1b['
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _run(args: list, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    """以子进程方式跑 proxyctl（拿到真实的 sys.argv 重置 + main() 流程）。"""
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["PROXYCTL_NO_COLOR"] = "1"
    if env_overrides:
        env.update(env_overrides)
    # 用 uv run 保证拿到 venv 内的 proxyctl 入口
    return subprocess.run(
        ["uv", "run", "proxyctl", *args],
        capture_output=True, text=True, env=env, timeout=15,
    )


@pytest.mark.parametrize("args", [
    ["--help"],
    ["--version"],
    ["agent-guide"],
    ["explain"],
    ["explain", "rules"],
    ["explain", "config"],
    ["explain", "exit-codes"],
    ["commands"],
    ["plugins"],
    ["doctor"],
    ["config", "path"],
    ["config", "get", "proxy_port"],
    ["status", "--help"],
    ["mode", "--help"],
    ["audit", "--help"],
])
def test_no_ansi_in_human_output_when_no_color(args):
    r = _run(args)
    assert not ANSI.search(r.stdout), \
        f"args={args}: stdout 仍含 ANSI:\n{r.stdout[:300]!r}"
    assert not ANSI.search(r.stderr), \
        f"args={args}: stderr 仍含 ANSI:\n{r.stderr[:300]!r}"


@pytest.mark.parametrize("args", [
    ["--json", "commands"],
    ["--json", "explain", "rules"],
    ["--json", "agent-guide"],
    ["--json", "config", "path"],
    ["--json", "config", "get", "proxy_port"],
])
def test_no_ansi_in_json_mode(args):
    """--json 模式必须自动关色（即使 NO_COLOR 不在）。"""
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    env.pop("PROXYCTL_NO_COLOR", None)
    r = subprocess.run(["uv", "run", "proxyctl", *args],
                       capture_output=True, text=True, env=env, timeout=15)
    assert not ANSI.search(r.stdout), \
        f"args={args}: --json stdout 仍含 ANSI"


def test_error_path_no_ansi():
    """错误路径（USAGE / NOT_FOUND）也必须遵守关色。"""
    r = _run(["statusssssss"])  # 拼写错 → USAGE(2)
    assert r.returncode == 2
    assert not ANSI.search(r.stderr), \
        f"未识别命令 stderr 仍含 ANSI:\n{r.stderr[:300]!r}"
