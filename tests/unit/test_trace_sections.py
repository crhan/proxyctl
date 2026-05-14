"""测试 trace.py 各 _section_* 大段和 _grep_log_connections。

这些函数都做 API 查询 + 打印，单测重点：
- API 不可达 → 平稳输出错误，不抛
- 规则匹配各 type 分支
- 连通性测试的 stdout 解析
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from proxyctl import trace


# ────────────────────────────────────────────────────────────────────────────
# _section_rules: 各规则类型匹配
# ────────────────────────────────────────────────────────────────────────────

def _mock_rules(monkeypatch, rules: list):
    def fake_api_get(api, path, secret):
        if path == "/rules":
            return {"rules": rules}
        return {}

    monkeypatch.setattr(trace, "_api_get", fake_api_get)


def test_section_rules_domain_suffix_match(monkeypatch, capsys):
    _mock_rules(monkeypatch, [
        {"type": "DomainSuffix", "payload": "github.com", "proxy": "auto",
         "index": 1, "extra": {"hitCount": 42}},
    ])
    rule, proxy = trace._section_rules("api.github.com", [], "http://x", "s")
    assert proxy == "auto"
    assert rule["index"] == 1
    out = capsys.readouterr().out
    assert "DomainSuffix" in out
    assert "github.com" in out


def test_section_rules_domain_keyword(monkeypatch):
    _mock_rules(monkeypatch, [
        {"type": "DomainKeyword", "payload": "google", "proxy": "proxy", "index": 5},
    ])
    rule, proxy = trace._section_rules("translate.google.com", [], "x", "y")
    assert proxy == "proxy"


def test_section_rules_exact_domain(monkeypatch):
    _mock_rules(monkeypatch, [
        {"type": "Domain", "payload": "exact.example.com", "proxy": "DIRECT", "index": 2},
    ])
    rule, proxy = trace._section_rules("exact.example.com", [], "x", "y")
    assert proxy == "DIRECT"


def test_section_rules_exact_domain_no_subdomain_match(monkeypatch):
    """Domain 是精确匹配，子域不算。"""
    _mock_rules(monkeypatch, [
        {"type": "Domain", "payload": "exact.example.com", "proxy": "DIRECT", "index": 2},
    ])
    rule, proxy = trace._section_rules("sub.exact.example.com", [], "x", "y")
    assert rule is None


def test_section_rules_ipcidr_match(monkeypatch):
    _mock_rules(monkeypatch, [
        {"type": "IPCIDR", "payload": "192.168.0.0/16", "proxy": "DIRECT", "index": 3},
    ])
    rule, proxy = trace._section_rules("x", ["192.168.1.1"], "x", "y")
    assert proxy == "DIRECT"


def test_section_rules_ipcidr_invalid_payload_skipped(monkeypatch):
    _mock_rules(monkeypatch, [
        {"type": "IPCIDR", "payload": "not-a-network", "proxy": "DIRECT", "index": 3},
        {"type": "Match", "payload": "", "proxy": "auto", "index": 4},
    ])
    rule, proxy = trace._section_rules("x", ["1.2.3.4"], "x", "y")
    # 第一条解析失败被跳过，命中第二条 Match
    assert proxy == "auto"


def test_section_rules_match_fallback(monkeypatch):
    _mock_rules(monkeypatch, [
        {"type": "Match", "payload": "", "proxy": "fallback", "index": 99},
    ])
    rule, proxy = trace._section_rules("x", [], "x", "y")
    assert proxy == "fallback"


def test_section_rules_no_match_warns(monkeypatch, capsys):
    _mock_rules(monkeypatch, [
        {"type": "DomainSuffix", "payload": "ghost.com", "proxy": "x", "index": 1},
    ])
    rule, proxy = trace._section_rules("real.com", [], "x", "y")
    assert rule is None
    assert "未匹配" in capsys.readouterr().out


def test_section_rules_api_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(trace, "_api_get", lambda *a, **kw: {})
    rule, proxy = trace._section_rules("x", [], "x", "y")
    assert rule is None
    assert "无法获取" in capsys.readouterr().out


def test_section_rules_skips_unsupported_types(monkeypatch):
    """GeoSite/GeoIP 是客户端无法精确匹配的，不命中。"""
    _mock_rules(monkeypatch, [
        {"type": "GeoSite", "payload": "google", "proxy": "x", "index": 1},
        {"type": "DomainSuffix", "payload": "google.com", "proxy": "auto", "index": 2},
    ])
    rule, proxy = trace._section_rules("google.com", [], "x", "y")
    # 命中 DomainSuffix 而不是 GeoSite
    assert proxy == "auto"


# ────────────────────────────────────────────────────────────────────────────
# _section_connectivity: stdout 解析
# ────────────────────────────────────────────────────────────────────────────

def test_section_connectivity_ok_real_ip(fake_subprocess):
    fake_subprocess.set_default(stdout="200 0.012 0.045 1.2.3.4", returncode=0)
    lines, ip = trace._section_connectivity("https", "x.com", None, "/")
    assert ip == "1.2.3.4"
    body = "\n".join(lines)
    assert "200" in body
    assert "1.2.3.4" in body


def test_section_connectivity_ok_local_loopback_tun(fake_subprocess):
    fake_subprocess.set_default(stdout="200 0.01 0.02 127.0.0.1", returncode=0)
    mode = {"tun_enabled": True, "enhanced_mode": "fake-ip", "mixed_port": 7890}
    lines, ip = trace._section_connectivity("https", "x.com", None, "/", mode=mode)
    body = "\n".join(lines)
    assert "via TUN" in body


def test_section_connectivity_ok_local_loopback_proxy(fake_subprocess):
    fake_subprocess.set_default(stdout="200 0.01 0.02 127.0.0.1", returncode=0)
    mode = {"tun_enabled": False, "enhanced_mode": "redir-host", "mixed_port": 7890}
    lines, _ = trace._section_connectivity("http", "x.com", 80, "/")
    # 没传 mode → 普通分支
    body = "\n".join(lines)
    assert "127.0.0.1" in body

    lines2, _ = trace._section_connectivity("http", "x.com", 80, "/", mode=mode)
    body2 = "\n".join(lines2)
    assert "via HTTP 代理" in body2


def test_section_connectivity_curl_failure(fake_subprocess):
    fake_subprocess.set_default(
        stdout="", stderr="curl: (7) Failed to connect", returncode=7)
    lines, ip = trace._section_connectivity("https", "x.com", None, "/")
    assert ip == ""
    assert any("✗ 连接失败" in l for l in lines)
    assert any("curl: (7)" in l for l in lines)


def test_section_connectivity_timeout(monkeypatch):
    import subprocess as _sp

    def boom(*a, **kw):
        raise _sp.TimeoutExpired("curl", 8)

    monkeypatch.setattr(_sp, "run", boom)
    lines, _ = trace._section_connectivity("https", "x.com", None, "/")
    assert any("超时" in l for l in lines)


def test_section_connectivity_exception(monkeypatch):
    import subprocess as _sp

    def boom(*a, **kw):
        raise RuntimeError("strange")

    monkeypatch.setattr(_sp, "run", boom)
    lines, _ = trace._section_connectivity("https", "x.com", None, "/")
    assert any("strange" in l for l in lines)


def test_section_connectivity_includes_port(fake_subprocess):
    fake_subprocess.set_default(stdout="200 0.01 0.02 1.2.3.4")
    lines, _ = trace._section_connectivity("https", "x.com", 8443, "/")
    assert any("x.com:8443" in l for l in lines)


# ────────────────────────────────────────────────────────────────────────────
# _section_dns
# ────────────────────────────────────────────────────────────────────────────

def test_section_dns_via_clash_api(monkeypatch, capsys):
    """API 返回 A 记录，无 CNAME。"""
    def fake_api(api, path, secret):
        return {"Answer": [
            {"type": 1, "data": "1.2.3.4", "TTL": 60},
        ]}

    monkeypatch.setattr(trace, "_api_get", fake_api)
    ips = trace._section_dns("example.com", "http://x", "s")
    assert "1.2.3.4" in ips
    out = capsys.readouterr().out
    assert "example.com" in out
    assert "1.2.3.4" in out


def test_section_dns_fakeip_tag(monkeypatch, capsys):
    """fakeip_active=True + IP 在 198.18.0.0/16 → 标 fakeip。"""
    def fake_api(api, path, secret):
        return {"Answer": [{"type": 1, "data": "198.18.0.5", "TTL": 1}]}

    monkeypatch.setattr(trace, "_api_get", fake_api)
    ips = trace._section_dns("x.com", "http://x", "s", fakeip_active=True)
    assert ips == ["198.18.0.5"]
    assert "fakeip" in capsys.readouterr().out


def test_section_dns_with_cname(monkeypatch, capsys):
    def fake_api(api, path, secret):
        return {"Answer": [
            {"type": 5, "data": "real.example.com."},
            {"type": 1, "data": "5.5.5.5", "TTL": 30},
        ]}

    monkeypatch.setattr(trace, "_api_get", fake_api)
    trace._section_dns("x.com", "http://x", "s")
    out = capsys.readouterr().out
    assert "CNAME" in out
    assert "real.example.com" in out


# ────────────────────────────────────────────────────────────────────────────
# _grep_log_connections
# ────────────────────────────────────────────────────────────────────────────

def test_grep_log_connections_no_files(_isolate_home: Path):
    """两个 log 文件都不存在 → 空列表。"""
    out = trace._grep_log_connections("example.com")
    assert out == []


def test_grep_log_connections_parses_mihomo(_isolate_home: Path):
    log = _isolate_home / ".config" / "mihomo" / "mihomo.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "07:42:13 INF [TCP] 1.2.3.4:1024 --> github.com:443 match Match using proxy[hk1]\n"
        "07:42:14 INF [TCP] 1.2.3.4:1025 --> github.com:443 match Match using proxy[hk1]\n"
        "07:42:15 INF [TCP] 1.2.3.4:1026 --> github.com:443 match Match using proxy[hk2]\n"
    )
    out = trace._grep_log_connections("github.com")
    assert isinstance(out, list)
    # 至少能找到一些
    assert len(out) >= 1
