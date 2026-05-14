"""测试 status.py — 数据采集 helper（subprocess wrapper）。

策略：每个 helper 是个 subprocess 包装，全部 mock 掉 subprocess.run。
不测 cmd_status（聚合 + 大量 print），交给集成测试。
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from proxyctl import status


# ────────────────────────────────────────────────────────────────────────────
# _port_listening
# ────────────────────────────────────────────────────────────────────────────

def test_port_listening_true():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert status._port_listening(port) is True
    finally:
        s.close()


def test_port_listening_false():
    """随机端口大概率没人监听。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert status._port_listening(port) is False


# ────────────────────────────────────────────────────────────────────────────
# launchctl wrappers
# ────────────────────────────────────────────────────────────────────────────

def test_launchctl_pid_extracts_value(fake_subprocess):
    fake_subprocess.set_default(stdout="state = running\n\tpid = 12345\n\tcount = 1\n")
    assert status._launchctl_pid("foo") == "12345"


def test_launchctl_pid_no_match_returns_empty(fake_subprocess):
    fake_subprocess.set_default(stdout="no info here")
    assert status._launchctl_pid("foo") == ""


def test_launchctl_runs_extracts(fake_subprocess):
    fake_subprocess.set_default(stdout="\truns = 17\n")
    assert status._launchctl_runs("foo") == "17"


def test_launchctl_running_via_returncode(fake_subprocess):
    fake_subprocess.set_default(returncode=0)
    assert status._launchctl_running("foo") is True

    fake_subprocess.set_default(returncode=37)
    assert status._launchctl_running("foo") is False


def test_launchctl_pid_sudo_flag(fake_subprocess):
    fake_subprocess.set_default(stdout="pid = 42")
    status._launchctl_pid("svc", sudo=True)
    assert fake_subprocess.calls[-1][0] == "sudo"


# ────────────────────────────────────────────────────────────────────────────
# _ifconfig_ip
# ────────────────────────────────────────────────────────────────────────────

def test_ifconfig_ip_parses(fake_subprocess):
    fake_subprocess.set_default(stdout=(
        "utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1500\n"
        "\tinet 198.18.0.1 netmask 0xffffff00 \n"
    ))
    assert status._ifconfig_ip("utun4") == "198.18.0.1"


def test_ifconfig_ip_no_inet(fake_subprocess):
    fake_subprocess.set_default(stdout="ether ab:cd:ef:00:11:22\n")
    assert status._ifconfig_ip("en0") == ""


# ────────────────────────────────────────────────────────────────────────────
# _gather_engine (mac launchctl 路径)
# ────────────────────────────────────────────────────────────────────────────

class _FakeEngine:
    name = "mihomo"
    label = "system/com.mihomo.tun"
    unit = "mihomo.service"
    config = "/no/such/file"


def test_gather_engine_running(monkeypatch, fake_subprocess):
    """模拟 macOS 路径：launchctl 返回 pid，ps 返回 etime。"""
    monkeypatch.setattr(status, "IS_LINUX", False)
    monkeypatch.setattr(status, "IS_MACOS", True)

    fake_subprocess.set_prefix_result(["launchctl", "print", "system/com.mihomo.tun"],
                                       stdout="\tpid = 99\n\truns = 3\n", returncode=0)
    fake_subprocess.set_prefix_result(["ps", "-o", "etime=", "-p", "99"],
                                       stdout="1-02:03:04\n", returncode=0)
    info = status._gather_engine(_FakeEngine())
    assert info["pid"] == "99"
    assert info["runs"] == "3"
    assert info["daemon_up"] is True
    assert info["etime"] == "1-02:03:04"


def test_gather_engine_stopped(monkeypatch, fake_subprocess):
    monkeypatch.setattr(status, "IS_LINUX", False)
    monkeypatch.setattr(status, "IS_MACOS", True)
    fake_subprocess.set_default(stdout="", returncode=0)   # 都返空
    info = status._gather_engine(_FakeEngine())
    assert info["pid"] == ""
    assert info["daemon_up"] is False
    assert info["etime"] == ""


def test_gather_engine_linux(monkeypatch, fake_subprocess):
    monkeypatch.setattr(status, "IS_LINUX", True)
    monkeypatch.setattr(status, "IS_MACOS", False)
    fake_subprocess.set_prefix_result(
        ["systemctl", "--user", "show", "mihomo.service", "-p", "MainPID", "--value"],
        stdout="4242\n", returncode=0)
    fake_subprocess.set_prefix_result(
        ["ps", "-o", "etime=", "-p", "4242"],
        stdout="00:42\n", returncode=0)

    info = status._gather_engine(_FakeEngine())
    assert info["pid"] == "4242"
    assert info["daemon_up"] is True
    assert info["etime"] == "00:42"


def test_gather_engine_linux_stopped(monkeypatch, fake_subprocess):
    monkeypatch.setattr(status, "IS_LINUX", True)
    monkeypatch.setattr(status, "IS_MACOS", False)
    fake_subprocess.set_default(stdout="0\n", returncode=0)   # systemd 输出 0 = 停了
    info = status._gather_engine(_FakeEngine())
    assert info["daemon_up"] is False
    assert info["etime"] == ""


# ────────────────────────────────────────────────────────────────────────────
# _gather_ports
# ────────────────────────────────────────────────────────────────────────────

def test_gather_ports_no_claude_proxy(monkeypatch):
    monkeypatch.setattr(status, "IS_MACOS", True)
    monkeypatch.setattr(status, "_port_listening", lambda p: False)
    monkeypatch.setattr(status, "_launchctl_running", lambda *a, **k: False)
    d = status._gather_ports("com.proxyctl.claude-proxy")
    assert d["cp_running"] is False
    assert d["cp_port"] is False


def test_gather_ports_with_claude_proxy(monkeypatch):
    monkeypatch.setattr(status, "IS_MACOS", True)
    monkeypatch.setattr(status, "_port_listening", lambda p: p == 7891)
    monkeypatch.setattr(status, "_launchctl_running", lambda *a, **k: True)
    monkeypatch.setattr(status, "_launchctl_pid", lambda *a, **k: "789")
    d = status._gather_ports("com.proxyctl.claude-proxy")
    assert d["cp_running"] is True
    assert d["cp_pid"] == "789"
    assert d["cp_port"] is True


# ────────────────────────────────────────────────────────────────────────────
# _gather_network 平台分支
# ────────────────────────────────────────────────────────────────────────────

def test_gather_network_macos(monkeypatch, fake_subprocess):
    monkeypatch.setattr(status, "IS_MACOS", True)
    fake_subprocess.set_prefix_result(
        ["route", "-n", "get", "default"],
        stdout="   interface: en0\n", returncode=0)
    fake_subprocess.set_prefix_result(
        ["ifconfig", "en0"],
        stdout="\tinet 192.168.1.5 netmask 0xffffff00\n", returncode=0)
    d = status._gather_network(_FakeEngine())
    assert d["default_iface"] == "en0"
    assert d["default_ip"] == "192.168.1.5"


def test_gather_network_linux(monkeypatch, fake_subprocess):
    monkeypatch.setattr(status, "IS_MACOS", False)
    fake_subprocess.set_prefix_result(
        ["ip", "route", "show", "default"],
        stdout="default via 10.0.0.1 dev eth0 src 10.0.0.5\n", returncode=0)
    fake_subprocess.set_prefix_result(
        ["ip", "-4", "addr", "show", "eth0"],
        stdout="    inet 10.0.0.5/24 scope global eth0\n", returncode=0)
    d = status._gather_network(_FakeEngine())
    assert d["default_iface"] == "eth0"
    assert d["default_ip"] == "10.0.0.5"


# ────────────────────────────────────────────────────────────────────────────
# _gather_tun — Linux 路径（直接返回空）
# ────────────────────────────────────────────────────────────────────────────

def test_gather_tun_linux_returns_empty(monkeypatch):
    monkeypatch.setattr(status, "IS_MACOS", False)
    d = status._gather_tun(_FakeEngine(), daemon_up=True)
    assert d["tun_iface"] == ""
    assert d["fakeip"] == "off"
    assert d["excludes"] == []


def test_gather_tun_macos_daemon_down(monkeypatch):
    """macOS 路径但 daemon 没起 → 不调任何 subprocess。"""
    monkeypatch.setattr(status, "IS_MACOS", True)
    engine = SimpleNamespace(name="mihomo", config="/no/such/file")
    d = status._gather_tun(engine, daemon_up=False)
    assert d["tun_iface"] == ""
    assert d["fakeip"] == ""    # 配置打不开 → 走异常分支


# ────────────────────────────────────────────────────────────────────────────
# _gather_proxy_settings：Linux 直接返空
# ────────────────────────────────────────────────────────────────────────────

def test_gather_proxy_settings_linux(monkeypatch):
    monkeypatch.setattr(status, "IS_MACOS", False)
    assert status._gather_proxy_settings() == {"active_svc": "", "info": {}}


# ────────────────────────────────────────────────────────────────────────────
# _gather_dns：Linux
# ────────────────────────────────────────────────────────────────────────────

def test_gather_dns_linux(monkeypatch):
    monkeypatch.setattr(status, "IS_MACOS", False)
    monkeypatch.setattr(status, "_port_listening", lambda p: p == 53)
    d = status._gather_dns("any.label")
    assert d["dns_up"] is True
    assert d["lock_up"] is False
    assert d["sys_dns"] == ""
    assert d["overrides"] == []
