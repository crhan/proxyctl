"""端到端集成测试，防 0.4.0 期间发现的 bug 类型再次出现。

每个测试都走 cli 真实路径（不 mock 整个函数），保证字段名 / 平台分支 /
平台 hardcode 这类 0.4.x 类 bug 能在 CI 一次性被抓出来。
"""

from __future__ import annotations

import json
import re

import pytest

from proxyctl import _io, cli, explain


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ── Bug: cmd_discovery 在非 macOS 平台 hardcode engine_up=False ───────────
def test_cmd_discovery_linux_uses_service_running(monkeypatch, capsys):
    """v0.4.0 期间发现：cli.py:1494 写死 ``engine_up = launchctl_running(...)
    if IS_MACOS else False``，导致 Linux 用户：
      - 人类 banner 永远显示 ✗ engine=mihomo（即使 systemd 服务在跑）
      - JSON discovery envelope ``data.engine.running`` 永远 false
        → agent 探测拿到错值

    修复：调 ``service_running(backend)`` 走 IS_MACOS / systemctl 两分支。
    本测试验证：Linux + service_running=True → engine_up 也 True。
    """
    monkeypatch.setattr(cli, "IS_MACOS", False)
    monkeypatch.setattr(cli, "service_running", lambda b: True)
    backend = cli.MihomoBackend({"config_dir": "/tmp/t", "proxy_port": 7890})
    config = {"proxy_port": 7892}

    # JSON 路径：discovery envelope
    cli.GLOBAL_FLAGS["json"] = True
    explain.set_global_flags({"json": True, "no_color": True, "quiet": False,
                              "dry_run": False, "plain": False})
    with pytest.raises(SystemExit):
        cli.cmd_discovery(backend, config)
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["engine"]["running"] is True, \
        "Linux 下 service_running=True 时 discovery.engine.running 应为 True"
    assert env["data"]["engine"]["name"] == "mihomo"
    assert env["data"]["engine"]["port"] == 7892


def test_cmd_discovery_linux_banner_shows_check_when_running(monkeypatch,
                                                              capsys):
    """对应人类模式：banner 在 Linux + service_running=True 时显示 ✓。"""
    monkeypatch.setattr(cli, "IS_MACOS", False)
    monkeypatch.setattr(cli, "service_running", lambda b: True)
    backend = cli.MihomoBackend({"config_dir": "/tmp/t", "proxy_port": 7890})
    config = {"proxy_port": 7892}

    explain.set_global_flags({"json": False, "no_color": True, "quiet": False,
                              "dry_run": False, "plain": False})
    cli.cmd_discovery(backend, config)  # 人类模式：不退出
    err = _strip_ansi(capsys.readouterr().err)
    assert "✓ engine=mihomo" in err, \
        f"Linux 引擎 running 时 banner 应显示 ✓；实际：{err!r}"
    assert "✗ engine=mihomo" not in err


def test_cmd_discovery_linux_banner_shows_cross_when_stopped(monkeypatch,
                                                              capsys):
    """对应人类模式反向：Linux + service_running=False → banner 显示 ✗。"""
    monkeypatch.setattr(cli, "IS_MACOS", False)
    monkeypatch.setattr(cli, "service_running", lambda b: False)
    backend = cli.MihomoBackend({"config_dir": "/tmp/t", "proxy_port": 7890})

    explain.set_global_flags({"json": False, "no_color": True, "quiet": False,
                              "dry_run": False, "plain": False})
    cli.cmd_discovery(backend, {"proxy_port": 7890})
    err = _strip_ansi(capsys.readouterr().err)
    assert "✗ engine=mihomo" in err
