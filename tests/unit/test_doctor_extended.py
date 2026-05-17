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


def _patch_doctor_probes(monkeypatch, *, engine_up=False):
    monkeypatch.setattr("proxyctl.cli.service_running",
                        lambda *a, **k: engine_up)
    monkeypatch.setattr(explain, "_tcp_open", lambda *a, **k: False)
    monkeypatch.setattr(explain, "_dns_points_to_loopback", lambda: False)
    monkeypatch.setattr(explain,
                        "_system_proxy_points_to_loopback", lambda p: False)
    monkeypatch.setattr(explain, "_quick_connectivity",
                        lambda *a, **k: False)
    # get_mode 不依赖外部，但避免误调 launchctl
    monkeypatch.setattr("proxyctl.cli.get_mode", lambda *a, **k: "proxy")


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
    """所有探测都失败 → score=0 → healthy=False。"""
    _patch_doctor_probes(monkeypatch, engine_up=False)
    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit):
        explain.cmd_doctor([], backend, config)
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["score"] == 0
    assert data["healthy"] is False


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
