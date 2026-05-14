"""测试 cli.py 内的 helper 函数（subprocess wrapper + 平台分支 + get_mode）。"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from proxyctl import cli


# ────────────────────────────────────────────────────────────────────────────
# run / run_out
# ────────────────────────────────────────────────────────────────────────────

def test_run_no_sudo(fake_subprocess):
    fake_subprocess.set_default(returncode=0)
    cli.run(["ls"])
    assert fake_subprocess.calls[-1][0] == "ls"


def test_run_with_sudo(fake_subprocess):
    fake_subprocess.set_default(returncode=0)
    cli.run(["ls"], sudo=True)
    assert fake_subprocess.calls[-1][:2] == ["sudo", "ls"]


def test_run_out_returns_stdout(fake_subprocess):
    fake_subprocess.set_default(stdout="hello world\n", returncode=0)
    assert cli.run_out(["echo"]) == "hello world"


def test_run_out_returns_empty_on_failure(fake_subprocess):
    fake_subprocess.set_default(stdout="anything", returncode=1)
    assert cli.run_out(["fail"]) == ""


# ────────────────────────────────────────────────────────────────────────────
# wait_port
# ────────────────────────────────────────────────────────────────────────────

def test_wait_port_immediate_success():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert cli.wait_port(port, timeout=2.0) is True
    finally:
        s.close()


def test_wait_port_timeout():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    start = time.monotonic()
    assert cli.wait_port(port, timeout=0.6) is False
    # 应该在 timeout 附近退出（容忍 1.0 内）
    assert time.monotonic() - start < 1.5


# ────────────────────────────────────────────────────────────────────────────
# list_network_services（subprocess wrapper）
# ────────────────────────────────────────────────────────────────────────────

def test_list_network_services_skips_disabled(fake_subprocess):
    fake_subprocess.set_default(stdout=(
        "An asterisk (*) denotes that a network service is disabled.\n"
        "Wi-Fi\n"
        "*Bluetooth PAN\n"
        "Thunderbolt Bridge\n"
    ), returncode=0)
    out = cli.list_network_services()
    assert "Wi-Fi" in out
    assert "Thunderbolt Bridge" in out
    assert all(not s.startswith("*") for s in out)


# ────────────────────────────────────────────────────────────────────────────
# launchctl_running / service_*
# ────────────────────────────────────────────────────────────────────────────

def test_launchctl_running_true(fake_subprocess):
    fake_subprocess.set_default(returncode=0)
    assert cli.launchctl_running("svc") is True


def test_launchctl_running_false(fake_subprocess):
    fake_subprocess.set_default(returncode=37)
    assert cli.launchctl_running("svc") is False


class _Bk:
    name = "mihomo"
    label = "system/com.mihomo.tun"
    plist = "/Library/LaunchDaemons/com.mihomo.tun.plist"
    unit  = "mihomo.service"
    config_file = "/tmp/no/config.yaml"


def test_service_running_macos(monkeypatch, fake_subprocess):
    monkeypatch.setattr(cli, "IS_MACOS", True)
    fake_subprocess.set_default(returncode=0)
    assert cli.service_running(_Bk()) is True


def test_service_running_linux(monkeypatch, fake_subprocess):
    monkeypatch.setattr(cli, "IS_MACOS", False)
    fake_subprocess.set_default(returncode=0)
    assert cli.service_running(_Bk()) is True
    assert fake_subprocess.calls[-1][:3] == ["systemctl", "--user", "is-active"]


def test_service_stop_macos(monkeypatch, fake_subprocess):
    monkeypatch.setattr(cli, "IS_MACOS", True)
    fake_subprocess.set_default(returncode=0)
    cli.service_stop(_Bk())
    assert fake_subprocess.calls[-1][:2] == ["sudo", "launchctl"]


def test_service_stop_linux(monkeypatch, fake_subprocess):
    monkeypatch.setattr(cli, "IS_MACOS", False)
    fake_subprocess.set_default(returncode=0)
    cli.service_stop(_Bk())
    assert fake_subprocess.calls[-1][:3] == ["systemctl", "--user", "stop"]


def test_service_restart_macos(monkeypatch, fake_subprocess):
    monkeypatch.setattr(cli, "IS_MACOS", True)
    fake_subprocess.set_default(returncode=0)
    cli.service_restart(_Bk())
    assert "kickstart" in fake_subprocess.calls[-1]


def test_service_restart_linux(monkeypatch, fake_subprocess):
    monkeypatch.setattr(cli, "IS_MACOS", False)
    fake_subprocess.set_default(returncode=0)
    cli.service_restart(_Bk())
    assert fake_subprocess.calls[-1][:3] == ["systemctl", "--user", "restart"]


def test_scutil_exec_passes_stdin(fake_subprocess):
    fake_subprocess.set_default(returncode=0)
    cli.scutil_exec("show State:/")
    assert fake_subprocess.calls[-1] == ["sudo", "scutil"]


# ────────────────────────────────────────────────────────────────────────────
# get_mode（与 engine 内 get_mode 等价的 cli 层版本）
# ────────────────────────────────────────────────────────────────────────────

def test_cli_get_mode_mihomo_tun(tmp_path: Path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("""
tun:
  enable: true
  auto-route: true
dns:
  enhanced-mode: fake-ip
""")
    b = SimpleNamespace(name="mihomo", config_file=str(cfg))
    assert cli.get_mode(b) == "tun"


def test_cli_get_mode_mihomo_proxy(tmp_path: Path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("""
tun:
  enable: false
dns:
  enhanced-mode: redir-host
""")
    b = SimpleNamespace(name="mihomo", config_file=str(cfg))
    assert cli.get_mode(b) == "proxy"


def test_cli_get_mode_singbox_tun(tmp_path: Path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({
        "inbounds": [{"type": "tun", "auto_route": True}],
        "dns": {"rules": [{"server": "fakeip-dns"}]},
    }))
    b = SimpleNamespace(name="singbox", config_file=str(cfg))
    assert cli.get_mode(b) == "tun"


def test_cli_get_mode_missing_file(tmp_path: Path):
    b = SimpleNamespace(name="mihomo", config_file=str(tmp_path / "nope.yaml"))
    assert cli.get_mode(b) == "unknown"
