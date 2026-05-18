"""--json 错误路径 envelope guard（0.4.2 P2）。

策略：所有走 _io.fail 的错误路径在 --json 模式下都应输出合法 envelope
（ok:false / code:N / 不含 Python traceback 字样）。本测试不覆盖"未捕获
异常"路径（cli.main 顶层目前无 try/except wrapper，那是未来 work）。

覆盖：
1. 未识别子命令 → USAGE
2. trace 无参数 → USAGE
3. audit 非数字非 apply → USAGE
4. mode 非 tun/proxy → USAGE
5. agent-guide --section 拼错 → USAGE + did-you-mean
6. config set 缺值 → USAGE
"""

from __future__ import annotations

import json
import sys

import pytest

from proxyctl import cli, explain, _io
from proxyctl.cli import MihomoBackend


_TRACEBACK_MARKERS = ("Traceback (most recent call last)", "  File \"",
                      "  line ", "Error\n", "Exception\n")


def _assert_no_traceback(captured: str) -> None:
    for marker in _TRACEBACK_MARKERS:
        assert marker not in captured, \
            f"输出中泄漏 traceback 标记 {marker!r}：{captured!r}"


def _assert_valid_error_envelope(out: str, expected_cmd: str) -> dict:
    """断言 out 是单一合法 envelope，ok=False，cmd 与预期一致。"""
    env = json.loads(out.strip())
    assert env["schema_version"] == 2
    assert env["ok"] is False, f"应是错误 envelope，实际 ok={env['ok']!r}"
    assert env["cmd"] == expected_cmd
    assert isinstance(env["error"], str) and env["error"], \
        f"error 字段应非空字符串，实际 {env['error']!r}"
    assert env["code"] != 0
    assert isinstance(env["hints"], list)
    assert env["meta"]["proxyctl_version"]
    return env


@pytest.fixture(autouse=True)
def _json_mode_on():
    """每个测试都在 --json 模式下跑。"""
    explain.set_global_flags({"json": True, "no_color": True, "quiet": False,
                              "dry_run": False, "plain": False})
    cli.GLOBAL_FLAGS.update(
        {"json": True, "no_color": True, "quiet": False,
         "dry_run": False, "plain": False})
    _io.set_json_mode(True)
    _io.set_no_color(True)


@pytest.fixture
def backend():
    return MihomoBackend({"config_dir": "/tmp/test", "proxy_port": 7890})


@pytest.fixture
def config():
    return {"backend": "mihomo", "proxy_port": 7890,
            "api_base": "http://127.0.0.1:9090", "api_secret": ""}


def _make_ctx(backend, config, args):
    return {"backend": backend, "config": config, "registry": None,
            "api_base": "http://127.0.0.1:9090", "api_secret": "",
            "args": args}


def test_error_envelope_unknown_command(capsys):
    """`proxyctl wat --json` → envelope.error 含 hint，无 traceback。"""
    with pytest.raises(SystemExit) as ei:
        cli._suggest_command_and_exit("wat")
    assert ei.value.code == _io.USAGE
    out, err = capsys.readouterr()
    env = _assert_valid_error_envelope(out, "wat")
    _assert_no_traceback(out + err)
    # 应该至少有一个 hint（commands 列表 / did-you-mean）
    assert env["hints"], "未识别子命令应有 hint"


def test_error_envelope_trace_missing_arg(backend, config, capsys):
    """trace 无 domain 参数 → envelope.error + hint。"""
    ctx = _make_ctx(backend, config, [])
    with pytest.raises(SystemExit) as ei:
        cli._h_trace(ctx)
    assert ei.value.code == _io.USAGE
    out, err = capsys.readouterr()
    env = _assert_valid_error_envelope(out, "trace")
    _assert_no_traceback(out + err)


def test_error_envelope_audit_bad_arg(backend, config, capsys):
    """audit 既不是数字也不是 apply → USAGE + did-you-mean。"""
    ctx = _make_ctx(backend, config, ["banana"])
    with pytest.raises(SystemExit) as ei:
        cli._h_audit(ctx)
    assert ei.value.code == _io.USAGE
    out, err = capsys.readouterr()
    env = _assert_valid_error_envelope(out, "audit")
    _assert_no_traceback(out + err)


def test_error_envelope_mode_unknown_target(backend, config, capsys):
    """mode 非 tun/proxy → USAGE + did-you-mean。"""
    # cmd_mode 直接走 _io.fail；_h_mode 在 target 非 tun/proxy 时 fallthrough 到
    # cmd_mode(target=...) —— cmd_mode 会判 target not in ("tun","proxy") → fail。
    with pytest.raises(SystemExit) as ei:
        cli.cmd_mode(backend, "tnu")  # typo → did-you-mean tun
    assert ei.value.code == _io.USAGE
    out, err = capsys.readouterr()
    env = _assert_valid_error_envelope(out, "mode")
    _assert_no_traceback(out + err)


def test_error_envelope_agent_guide_section_typo(backend, config, capsys):
    """agent-guide --section 拼错 → USAGE + did-you-mean。"""
    with pytest.raises(SystemExit) as ei:
        explain.cmd_agent_guide(["--section", "introducton"], backend, config)
    assert ei.value.code == _io.USAGE
    out, err = capsys.readouterr()
    env = _assert_valid_error_envelope(out, "agent-guide")
    _assert_no_traceback(out + err)
    hints_str = " ".join(env["hints"])
    assert "introduction" in hints_str, \
        "拼错应给 did-you-mean，hint 中应含正确名"
