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


# ─────────────────────────────────────────────────────────────
# get_engine_version (v0.4.7+) — 跑 `<backend> -v` 解析引擎版本
# ─────────────────────────────────────────────────────────────

def _make_fake_binary(tmp_path: Path, name: str, stdout: str,
                     returncode: int = 0) -> Path:
    """构造一个可执行 shell 脚本，假装是引擎 binary。"""
    p = tmp_path / name
    p.write_text(f"#!/bin/sh\ncat <<'EOF'\n{stdout}\nEOF\nexit {returncode}\n")
    p.chmod(0o755)
    return p


def _isolate_path(monkeypatch, *binary_paths: Path):
    """把 PATH 限到只含给定 binary 所在目录 + 必要系统路径，避免命中真 mihomo。"""
    dirs = list({str(p.parent) for p in binary_paths})
    cli.get_engine_version.cache_clear()
    monkeypatch.setenv("PATH", ":".join(dirs) + ":/usr/bin:/bin")


def test_get_engine_version_mihomo_parse_happy(tmp_path: Path, monkeypatch):
    """mihomo -v 标准输出能完整解析 version / platform / go_version / build_date。"""
    bin_ = _make_fake_binary(tmp_path, "mihomo",
        "Mihomo Meta 1.19.25 darwin arm64 with go1.26.3 2026-05-16T14:37:07Z\n"
        "Use tags: with_gvisor")
    _isolate_path(monkeypatch, bin_)

    info = cli.get_engine_version("mihomo")
    assert info is not None
    assert info["version"] == "1.19.25"
    assert info["platform"] == "darwin arm64"
    assert info["go_version"] == "1.26.3"
    assert info["build_date"] == "2026-05-16"
    assert info["binary"].endswith("/mihomo")
    assert "Mihomo Meta 1.19.25" in info["raw"]


def test_get_engine_version_binary_not_found(monkeypatch):
    """binary 找不到 → 返回 None，不抛异常。"""
    cli.get_engine_version.cache_clear()
    monkeypatch.setenv("PATH", "/nonexistent/dir/only")
    assert cli.get_engine_version("nope-engine-xyz") is None


def test_get_engine_version_nonzero_returncode(tmp_path: Path, monkeypatch):
    """binary 存在但 -v 退出非 0 → 返回 None。"""
    bin_ = _make_fake_binary(tmp_path, "weird-engine", "some output", returncode=2)
    _isolate_path(monkeypatch, bin_)
    assert cli.get_engine_version("weird-engine") is None


def test_get_engine_version_unparseable_output_keeps_raw(tmp_path: Path, monkeypatch):
    """输出格式不认得 → 返回 dict 但 version=None，raw 保留。

    设计意图：让 agent 能拿到原始字符串，不至于解析失败丢失全部信息。
    """
    bin_ = _make_fake_binary(tmp_path, "exotic-engine", "completely different format")
    _isolate_path(monkeypatch, bin_)
    info = cli.get_engine_version("exotic-engine")
    assert info is not None
    assert info["version"] is None
    assert "completely different" in info["raw"]


def test_get_engine_version_lru_cache_reuses_result(tmp_path: Path, monkeypatch):
    """走 lru_cache，第二次不重跑 subprocess（避免 status 浪费）。"""
    bin_ = _make_fake_binary(tmp_path, "mihomo",
        "Mihomo Meta 9.9.9 linux x86_64 with go1.99.0 2099-01-01T00:00:00Z")
    _isolate_path(monkeypatch, bin_)

    first = cli.get_engine_version("mihomo")
    bin_.unlink()  # 删 binary，第二次仍应命中缓存
    second = cli.get_engine_version("mihomo")
    assert first == second
    assert second["version"] == "9.9.9"


def test_get_engine_version_returns_dict_when_called(tmp_path: Path, monkeypatch):
    """烟雾测：返回 dict 含必要字段。"""
    bin_ = _make_fake_binary(tmp_path, "mihomo",
        "Mihomo Meta 1.0.0 darwin arm64 with go1.20.0 2024-01-01T00:00:00Z")
    _isolate_path(monkeypatch, bin_)
    info = cli.get_engine_version("mihomo")
    assert info is not None
    for k in ("binary", "raw", "version", "platform", "go_version", "build_date"):
        assert k in info, f"missing key {k}"


# v0.5.2 回归：兼容两种 mihomo -v 输出格式（version v 前缀 + Unix `date` 多字段）
# 真实 bug：
#   - status/doctor 行渲染 `f"v{ver['version']}"`，若 version 已含 v 则输出 vv1.19.10
#   - build_date 旧 regex 用 (\S+) 只吞第一个 token，把 "Sat May 31 ..." 截成 "Sat"
@pytest.mark.parametrize("raw,expected", [
    (
        "Mihomo Meta 1.19.25 darwin arm64 with go1.26.3 2026-05-16T14:37:07Z",
        {"version": "1.19.25", "platform": "darwin arm64",
         "go_version": "1.26.3", "build_date": "2026-05-16"},
    ),
    (
        "Mihomo Meta v1.19.10 linux amd64 with go1.24.3 Sat May 31 07:51:30 UTC 2025",
        {"version": "1.19.10", "platform": "linux amd64",
         "go_version": "1.24.3", "build_date": "Sat May 31 07:51:30 UTC 2025"},
    ),
])
def test_get_engine_version_handles_both_mihomo_formats(
    tmp_path: Path, monkeypatch, raw, expected,
):
    bin_ = _make_fake_binary(tmp_path, "mihomo", raw)
    _isolate_path(monkeypatch, bin_)
    info = cli.get_engine_version("mihomo")
    assert info is not None, "parser dropped a known mihomo -v format"
    for k, v in expected.items():
        assert info[k] == v, (
            f"key {k}: expected {v!r}, got {info[k]!r} (raw={raw!r})"
        )
    # 反向断言：version 永不携带 v 前缀（渲染层会自加 v；带前缀就 vv）
    assert not info["version"].startswith("v"), (
        f"version must be normalized without 'v' prefix, got {info['version']!r}"
    )
