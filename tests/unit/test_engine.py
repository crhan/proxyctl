"""测试 engine.* 后端实现。

后端有两层：
1. 路径属性（label/plist/config_file/...）— 纯静态
2. get_mode/get_api_url — 从配置文件文本里解析

通过 tmp_path 写小配置 fixture，避开 subprocess。
check_config 调子进程，单独用 fake_subprocess。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from proxyctl.engine.base import Backend
from proxyctl.engine.mihomo import MihomoBackend
from proxyctl.engine.singbox import SingboxBackend


# ────────────────────────────────────────────────────────────────────────────
# 基类：实例化抽象类要失败
# ────────────────────────────────────────────────────────────────────────────

def test_backend_is_abstract():
    with pytest.raises(TypeError):
        Backend("x", "/tmp")   # type: ignore[abstract]


# ────────────────────────────────────────────────────────────────────────────
# Mihomo: 路径
# ────────────────────────────────────────────────────────────────────────────

def test_mihomo_paths(tmp_path: Path):
    b = MihomoBackend(str(tmp_path))
    assert b.name == "mihomo"
    assert b.config_dir == str(tmp_path)
    assert b.label == "system/com.mihomo.tun"
    assert b.plist == "/Library/LaunchDaemons/com.mihomo.tun.plist"
    assert b.config_file == str(tmp_path / "mihomo" / "config.yaml")
    assert b.cache_file == str(tmp_path / "mihomo" / "cache.db")
    assert b.log_file == str(tmp_path / "mihomo" / "mihomo.log")
    assert repr(b).startswith("MihomoBackend(")


# ────────────────────────────────────────────────────────────────────────────
# Mihomo: get_mode 各分支
# ────────────────────────────────────────────────────────────────────────────

def _write_mihomo(tmp_path: Path, content: str) -> MihomoBackend:
    d = tmp_path / "mihomo"
    d.mkdir()
    (d / "config.yaml").write_text(content)
    return MihomoBackend(str(tmp_path))


def test_mihomo_get_mode_missing_file(tmp_path: Path):
    b = MihomoBackend(str(tmp_path))
    assert b.get_mode() == "unknown"


def test_mihomo_get_mode_tun(tmp_path: Path):
    b = _write_mihomo(tmp_path, """
tun:
  enable: true
  auto-route: true
  stack: system
dns:
  enhanced-mode: fake-ip
""")
    assert b.get_mode() == "tun"


def test_mihomo_get_mode_proxy(tmp_path: Path):
    b = _write_mihomo(tmp_path, """
tun:
  enable: false
  auto-route: false
dns:
  enhanced-mode: redir-host
""")
    assert b.get_mode() == "proxy"


def test_mihomo_get_mode_mixed(tmp_path: Path):
    """auto-route 开但 fake-ip 关 → mixed。"""
    b = _write_mihomo(tmp_path, """
tun:
  enable: true
  auto-route: true
dns:
  enhanced-mode: redir-host
""")
    assert b.get_mode() == "mixed"


# ────────────────────────────────────────────────────────────────────────────
# Mihomo: get_api_url 分支
# ────────────────────────────────────────────────────────────────────────────

def test_mihomo_api_url_default_when_no_config(tmp_path: Path):
    b = MihomoBackend(str(tmp_path))
    assert b.get_api_url() == "http://127.0.0.1:9090"


def test_mihomo_api_url_port_only(tmp_path: Path):
    b = _write_mihomo(tmp_path, "external-controller: :7777\n")
    assert b.get_api_url() == "http://127.0.0.1:7777"


def test_mihomo_api_url_bare_host(tmp_path: Path):
    b = _write_mihomo(tmp_path, "external-controller: 10.0.0.1:9090\n")
    assert b.get_api_url() == "http://10.0.0.1:9090"


def test_mihomo_api_url_explicit_http(tmp_path: Path):
    b = _write_mihomo(tmp_path, "external-controller: http://10.0.0.1:9090\n")
    assert b.get_api_url() == "http://10.0.0.1:9090"


def test_mihomo_api_url_missing_key(tmp_path: Path):
    """配置文件存在但无 external-controller key → 回落默认值。"""
    b = _write_mihomo(tmp_path, "log-level: info\n")
    assert b.get_api_url() == "http://127.0.0.1:9090"


# ────────────────────────────────────────────────────────────────────────────
# Mihomo: check_config 走 subprocess
# ────────────────────────────────────────────────────────────────────────────

def test_mihomo_check_config_ok(tmp_path: Path, fake_subprocess):
    b = MihomoBackend(str(tmp_path))
    fake_subprocess.set_default(returncode=0)
    assert b.check_config() is True


def test_mihomo_check_config_fail(tmp_path: Path, fake_subprocess):
    b = MihomoBackend(str(tmp_path))
    fake_subprocess.set_default(returncode=1, stderr="bad config")
    assert b.check_config() is False


def test_mihomo_check_config_subprocess_exception(tmp_path: Path, monkeypatch):
    """mihomo 二进制不存在 → 返回 False，不抛异常。"""
    import subprocess as _sp

    def boom(*a, **kw):
        raise FileNotFoundError("mihomo not installed")

    monkeypatch.setattr(_sp, "run", boom)
    b = MihomoBackend(str(tmp_path))
    assert b.check_config() is False


# ────────────────────────────────────────────────────────────────────────────
# Sing-box: 路径
# ────────────────────────────────────────────────────────────────────────────

def test_singbox_paths(tmp_path: Path):
    b = SingboxBackend(str(tmp_path))
    assert b.name == "singbox"
    assert b.label == "system/com.singbox.tun"
    assert b.config_file == str(tmp_path / "sing-box" / "config.json")
    assert b.log_file == str(tmp_path / "sing-box" / "sing-box.log")


# ────────────────────────────────────────────────────────────────────────────
# Sing-box: get_mode
# ────────────────────────────────────────────────────────────────────────────

def _write_singbox(tmp_path: Path, cfg: dict) -> SingboxBackend:
    d = tmp_path / "sing-box"
    d.mkdir()
    (d / "config.json").write_text(json.dumps(cfg))
    return SingboxBackend(str(tmp_path))


def test_singbox_get_mode_missing(tmp_path: Path):
    assert SingboxBackend(str(tmp_path)).get_mode() == "unknown"


def test_singbox_get_mode_tun(tmp_path: Path):
    b = _write_singbox(tmp_path, {
        "inbounds": [{"type": "tun", "auto_route": True}],
        "dns": {"rules": [{"server": "fakeip-dns"}]},
    })
    assert b.get_mode() == "tun"


def test_singbox_get_mode_proxy(tmp_path: Path):
    b = _write_singbox(tmp_path, {
        "inbounds": [{"type": "tun", "auto_route": False}],
        "dns": {"rules": []},
    })
    assert b.get_mode() == "proxy"


def test_singbox_get_mode_mixed(tmp_path: Path):
    """auto_route 默认 True + 无 fakeip 规则 → mixed。"""
    b = _write_singbox(tmp_path, {
        "inbounds": [{"type": "tun"}],
        "dns": {"rules": []},
    })
    assert b.get_mode() == "mixed"


def test_singbox_get_mode_invalid_json(tmp_path: Path):
    d = tmp_path / "sing-box"
    d.mkdir()
    (d / "config.json").write_text("{ not json")
    assert SingboxBackend(str(tmp_path)).get_mode() == "unknown"


# ────────────────────────────────────────────────────────────────────────────
# Sing-box: get_api_url
# ────────────────────────────────────────────────────────────────────────────

def test_singbox_api_url_default(tmp_path: Path):
    assert SingboxBackend(str(tmp_path)).get_api_url() == "http://127.0.0.1:9090"


def test_singbox_api_url_port_only(tmp_path: Path):
    b = _write_singbox(tmp_path, {
        "experimental": {"clash_api": {"external_controller": ":8888"}},
    })
    assert b.get_api_url() == "http://127.0.0.1:8888"


def test_singbox_api_url_bare_host(tmp_path: Path):
    b = _write_singbox(tmp_path, {
        "experimental": {"clash_api": {"external_controller": "10.0.0.1:9090"}},
    })
    assert b.get_api_url() == "http://10.0.0.1:9090"


def test_singbox_api_url_explicit_http(tmp_path: Path):
    b = _write_singbox(tmp_path, {
        "experimental": {"clash_api": {"external_controller": "http://example:9090"}},
    })
    assert b.get_api_url() == "http://example:9090"


def test_singbox_check_config_ok(tmp_path: Path, fake_subprocess):
    fake_subprocess.set_default(returncode=0)
    assert SingboxBackend(str(tmp_path)).check_config() is True


def test_singbox_check_config_fail(tmp_path: Path, fake_subprocess):
    fake_subprocess.set_default(returncode=2, stderr="schema mismatch")
    assert SingboxBackend(str(tmp_path)).check_config() is False
