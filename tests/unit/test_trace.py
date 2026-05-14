"""测试 trace.py 纯函数 + 配置解析。

不测 _section_* 大块（耦合 stdout + subprocess），由 cli 集成测试覆盖入口。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proxyctl import trace


# ────────────────────────────────────────────────────────────────────────────
# _is_ip
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("s,expected", [
    ("1.2.3.4", True),
    ("255.255.255.255", True),
    ("::1", True),
    ("2001:db8::1", True),
    ("example.com", False),
    ("256.1.1.1", False),
    ("", False),
    ("not-an-ip", False),
])
def test_is_ip(s, expected):
    assert trace._is_ip(s) is expected


# ────────────────────────────────────────────────────────────────────────────
# _parse_input: 输入识别
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("example.com",                       ("https", "example.com", None, "/")),
    ("example.com:8080",                  ("https", "example.com", 8080, "/")),
    ("http://example.com",                ("http",  "example.com", None, "/")),
    ("http://example.com:80/path",        ("http",  "example.com", 80,   "/path")),
    ("https://example.com:443/a/b?c=d",   ("https", "example.com", 443,  "/a/b?c=d")),
    ("ws://example.com:80",               ("http",  "example.com", 80,   "/")),
    ("wss://example.com:443",             ("https", "example.com", 443,  "/")),
    ("example.com/some/path",             ("https", "example.com", None, "/some/path")),
    ("example.com:bad_port",              ("https", "example.com", None, "/")),   # 无法解析端口时 None
])
def test_parse_input(raw, expected):
    assert trace._parse_input(raw) == expected


# ────────────────────────────────────────────────────────────────────────────
# _detect_mode: 读 mihomo 配置
# ────────────────────────────────────────────────────────────────────────────

def _setup_mihomo_config(home: Path, content: str) -> Path:
    """在 fake HOME 下放一份 mihomo 配置。"""
    cfg = home / ".config" / "mihomo" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(content)
    return cfg


def test_detect_mode_defaults_when_no_config(_isolate_home: Path):
    """无配置时返回默认值。"""
    d = trace._detect_mode()
    assert d == {"tun_enabled": False, "enhanced_mode": "redir-host", "mixed_port": 7890}


def test_detect_mode_parses_tun_and_fakeip(_isolate_home: Path):
    _setup_mihomo_config(_isolate_home, """
tun:
  enable: true
  stack: system
dns:
  enhanced-mode: fake-ip
mixed-port: 17890
""")
    d = trace._detect_mode()
    assert d["tun_enabled"] is True
    assert d["enhanced_mode"] == "fake-ip"
    assert d["mixed_port"] == 17890


def test_detect_mode_partial_fields(_isolate_home: Path):
    _setup_mihomo_config(_isolate_home, "tun:\n  enable: false\n")
    d = trace._detect_mode()
    assert d["tun_enabled"] is False
    assert d["enhanced_mode"] == "redir-host"  # 缺省落回默认


# ────────────────────────────────────────────────────────────────────────────
# _api_get: urllib mock
# ────────────────────────────────────────────────────────────────────────────

def test_api_get_returns_dict(monkeypatch):
    payload = {"Answer": [{"type": 1, "data": "1.2.3.4"}]}
    fake_body = json.dumps(payload).encode()

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return fake_body

    class _Opener:
        def open(self, req, timeout=5):
            return _Resp()

    monkeypatch.setattr(trace.urllib.request, "build_opener", lambda *a, **kw: _Opener())
    out = trace._api_get("http://127.0.0.1:9090", "/dns/query?name=x", "secret")
    assert out == payload


def test_api_get_swallows_errors(monkeypatch):
    class _Opener:
        def open(self, req, timeout=5):
            raise RuntimeError("boom")

    monkeypatch.setattr(trace.urllib.request, "build_opener", lambda *a, **kw: _Opener())
    out = trace._api_get("http://x", "/y", "sec")
    assert out == {}
