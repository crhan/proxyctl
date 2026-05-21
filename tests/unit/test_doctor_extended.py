"""测试 doctor --json 的 0.3.0 扩展字段（engine/mode/lock_path 等 informational）。"""

from __future__ import annotations

import json

import pytest

from proxyctl import explain
from proxyctl.cli import MihomoBackend


@pytest.fixture
def backend():
    return MihomoBackend({"config_dir": "/tmp/test", "proxy_port": 7890})


@pytest.fixture
def config():
    return {
        "backend": "mihomo",
        "proxy_port": 7890,
        "api_base": "http://127.0.0.1:9090",
        "api_secret": "",
        "extra_daemons": {},
        "corp_dns": {},
    }


def _patch_doctor_probes(monkeypatch, *, engine_up=False, mode="proxy"):
    monkeypatch.setattr("proxyctl.cli.service_running",
                        lambda *a, **k: engine_up)
    monkeypatch.setattr(explain, "_tcp_open", lambda *a, **k: False)
    monkeypatch.setattr(explain, "_dns_points_to_loopback", lambda: False)
    monkeypatch.setattr(explain,
                        "_system_proxy_points_to_loopback", lambda p: False)
    monkeypatch.setattr(explain, "_quick_connectivity",
                        lambda *a, **k: False)
    # get_mode 不依赖外部，但避免误调 launchctl
    monkeypatch.setattr("proxyctl.cli.get_mode", lambda *a, **k: mode)


def test_doctor_includes_informational_fields(backend, config, capsys,
                                              monkeypatch):
    _patch_doctor_probes(monkeypatch)
    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit):
        explain.cmd_doctor([], backend, config)
    data = json.loads(capsys.readouterr().out)["data"]
    # 原 5 项布尔保留
    for k in ("engine_up", "port_listen", "dns_ok",
              "system_proxy_ok", "connectivity_ok"):
        assert k in data
    # 0.3.0 新增 informational 字段
    assert data["engine"] == "mihomo"
    assert data["mode"] in ("proxy", "tun", "unknown")
    assert data["port"] == 7890
    assert isinstance(data["config_path"], str)
    assert data["config_path"].endswith("config.yaml")
    assert isinstance(data["engine_config_path"], str)
    assert isinstance(data["lock_held"], list)
    assert isinstance(data["lock_path"], dict)
    assert "system" in data["lock_path"]
    assert "config" in data["lock_path"]
    assert "daemon" in data["lock_path"]
    # score 仍只数核心 5 项
    assert data["score"] <= data["max"]
    assert data["max"] == 5
    # 0.3.3：healthy 字段免去 agent 自己算 score == max
    assert "healthy" in data
    assert isinstance(data["healthy"], bool)
    assert data["healthy"] == (data["score"] == data["max"])


def test_doctor_healthy_false_when_some_fail(backend, config, capsys,
                                              monkeypatch):
    """所有探测都失败 → score=0 → healthy=False。

    用 mode="tun" 避免 v0.5.4 起 proxy 模式 dns_ok 永远 True 的 mode-aware
    短路（_dns_check_ok），让本测试能验证 5 项全失败时的 score=0 假设。
    """
    _patch_doctor_probes(monkeypatch, engine_up=False, mode="tun")
    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit):
        explain.cmd_doctor([], backend, config)
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["score"] == 0
    assert data["healthy"] is False


def test_doctor_dns_ok_true_in_proxy_mode(backend, config, capsys, monkeypatch):
    """v0.5.4：proxy 模式下 dns_ok 永远 True（不读 /etc/resolv.conf）。

    回归 Linux + systemd-resolved（127.0.0.53 stub）+ proxy 模式下
    doctor 误报 dns_ok=False 的 bug。
    """
    _patch_doctor_probes(monkeypatch, engine_up=True, mode="proxy")
    # 即便底层探针仍 mock 成 False，mode-aware 应让 dns_ok = True
    calls = {"n": 0}
    def fake_loopback():
        calls["n"] += 1
        return False
    monkeypatch.setattr(explain, "_dns_points_to_loopback", fake_loopback)
    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit):
        explain.cmd_doctor([], backend, config)
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["dns_ok"] is True, "proxy 模式 dns_ok 应为 True（mode-aware 短路）"
    assert calls["n"] == 0, "proxy 模式下不应调 _dns_points_to_loopback"


def test_doctor_dns_ok_in_tun_mode_still_reads_loopback(backend, config,
                                                         capsys, monkeypatch):
    """v0.5.4：tun 模式仍按旧逻辑读 /etc/resolv.conf。"""
    _patch_doctor_probes(monkeypatch, engine_up=True, mode="tun")
    calls = {"n": 0, "ret": True}
    def fake_loopback():
        calls["n"] += 1
        return calls["ret"]
    monkeypatch.setattr(explain, "_dns_points_to_loopback", fake_loopback)
    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit):
        explain.cmd_doctor([], backend, config)
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["dns_ok"] is True
    assert calls["n"] == 1, "tun 模式应调一次 _dns_points_to_loopback"


def test_doctor_lock_path_strings_end_with_lock_name(backend, config,
                                                     capsys, monkeypatch):
    _patch_doctor_probes(monkeypatch)
    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit):
        explain.cmd_doctor([], backend, config)
    data = json.loads(capsys.readouterr().out)["data"]
    for name, path in data["lock_path"].items():
        assert path.endswith(f".lock.{name}"), \
            f"lock_path[{name!r}] = {path!r} 不符合命名"
