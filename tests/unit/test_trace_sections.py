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
# no-resolve IP 规则在域名连接下的跳过逻辑
# ────────────────────────────────────────────────────────────────────────────

def test_section_rules_noresolve_skipped_on_domain(monkeypatch, capsys):
    """域名连接（noresolve_can_match=False）下，no-resolve 的 IP 规则被跳过，落兜底。"""
    _mock_rules(monkeypatch, [
        {"type": "IPCIDR", "payload": "100.64.0.0/10", "proxy": "DIRECT", "index": 1},
        {"type": "Match", "payload": "", "proxy": "proxy", "index": 2},
    ])
    rule, proxy = trace._section_rules(
        "home.ts.net", ["100.110.226.97"], "x", "y",
        noresolve_can_match=False, noresolve_payloads={"100.64.0.0/10"},
    )
    # IP 落在网段内，但 no-resolve + 域名连接 → 跳过 → 命中 Match
    assert proxy == "proxy"
    out = capsys.readouterr().out
    assert "no-resolve" in out


def test_section_rules_noresolve_matches_when_ip_present(monkeypatch):
    """连接带真实 IP（noresolve_can_match=True，如 TUN 模式 / 目标即 IP）时正常命中。"""
    _mock_rules(monkeypatch, [
        {"type": "IPCIDR", "payload": "100.64.0.0/10", "proxy": "DIRECT", "index": 1},
        {"type": "Match", "payload": "", "proxy": "proxy", "index": 2},
    ])
    rule, proxy = trace._section_rules(
        "100.110.226.97", ["100.110.226.97"], "x", "y",
        noresolve_can_match=True, noresolve_payloads={"100.64.0.0/10"},
    )
    assert proxy == "DIRECT"


def test_section_rules_noresolve_default_backward_compat(monkeypatch):
    """默认 noresolve_can_match=True，不传 payloads → 老行为不变（IP 规则照常命中）。"""
    _mock_rules(monkeypatch, [
        {"type": "IPCIDR", "payload": "100.64.0.0/10", "proxy": "DIRECT", "index": 1},
    ])
    rule, proxy = trace._section_rules("x", ["100.110.226.97"], "x", "y")
    assert proxy == "DIRECT"


def test_load_noresolve_payloads(tmp_path):
    """从 mihomo 配置 rules 段解析出带 no-resolve 的 IP payload。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "dns:\n"
        "  enable: true\n"
        "rules:\n"
        "- DOMAIN-SUFFIX,ts.net,DIRECT\n"
        "- IP-CIDR,100.64.0.0/10,DIRECT,no-resolve\n"
        "- IP-CIDR,8.8.8.8/32,proxy\n"
        "- IP-CIDR6,fd00::/8,DIRECT,no-resolve\n"
        "- MATCH,proxy\n"
        "proxies:\n"
        "- name: x\n"
    )
    payloads = trace._load_noresolve_payloads(str(cfg))
    assert payloads == {"100.64.0.0/10", "fd00::/8"}


def test_load_noresolve_payloads_missing_file():
    """配置文件不存在时返回空集合，不抛。"""
    assert trace._load_noresolve_payloads("/nonexistent/config.yaml") == set()


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


# ────────────────────────────────────────────────────────────────────────────
# _section_connections: 活跃连接按链路聚合 + 与预测对比
# ────────────────────────────────────────────────────────────────────────────

def _mock_connections(monkeypatch, conns: list):
    def fake_api_get(api, path, secret):
        if path == "/connections":
            return {"connections": conns}
        return {}

    monkeypatch.setattr(trace, "_api_get", fake_api_get)


def _conn(host, chains, up=1, down=1, ip="", port="443", sniff=""):
    return {
        "metadata": {"host": host, "sniffHost": sniff,
                     "destinationIP": ip, "destinationPort": port},
        "chains": chains, "upload": up, "download": down,
    }


# mihomo chains 顺序：[末端节点, ..., 入口组]，proxy 链含组名 'proxy'
_PROXY_CHAIN = ["电信专用(直连)", "proxy-tuic", "proxy"]


def test_section_connections_all_match(monkeypatch, capsys):
    """全部走预测出口 → ✓ 一致，相同链路聚合成一组计数。"""
    _mock_connections(monkeypatch, [
        _conn("aicodewith.com", _PROXY_CHAIN, up=590, down=3500),
        _conn("aicodewith.com", _PROXY_CHAIN, up=100, down=200),
    ])
    trace._section_connections("aicodewith.com", [], "proxy", "http://x", "s")
    out = trace._strip_ansi(capsys.readouterr().out)
    assert "应走(规则预测): proxy" in out
    assert "2 条" in out  # 两条相同链路聚合
    assert "✓ 结论: 全部 2 条都走 proxy" in out


def test_section_connections_mixed_old_and_new(monkeypatch, capsys):
    """改规则后的过渡态：新连接走 proxy、旧连接仍走 DIRECT —— 给出明确结论而非自相矛盾。"""
    _mock_connections(monkeypatch, [
        _conn("aicodewith.com", _PROXY_CHAIN, up=590, down=3500),
        _conn("aicodewith.com", ["DIRECT"], up=73400, down=18200),
    ])
    trace._section_connections("aicodewith.com", [], "proxy", "http://x", "s")
    out = trace._strip_ansi(capsys.readouterr().out)
    assert "1 条新连接已走 proxy" in out
    assert "仍走旧链路" in out
    assert "刷新页面后即全部走 proxy" in out


def test_section_connections_none_match(monkeypatch, capsys):
    """全部走 DIRECT、没有一条走预测出口 → 提示规则可能未生效。"""
    _mock_connections(monkeypatch, [
        _conn("aicodewith.com", ["DIRECT"], up=100, down=200),
    ])
    trace._section_connections("aicodewith.com", [], "proxy", "http://x", "s")
    out = trace._strip_ansi(capsys.readouterr().out)
    assert "都没走 proxy" in out
    assert "规则可能未生效" in out


def test_section_connections_host_match_excludes_shared_ip(monkeypatch, capsys):
    """有 host 精确匹配时，不把同 IP 的其他站点连接（Cloudflare 共享 IP）误算进来。"""
    _mock_connections(monkeypatch, [
        _conn("aicodewith.com", _PROXY_CHAIN, ip="104.26.4.164"),
        # 同一个 Cloudflare IP 上的别站，host 不匹配 → 应被排除
        _conn("other-site.com", ["DIRECT"], up=999, down=999, ip="104.26.4.164"),
    ])
    trace._section_connections(
        "aicodewith.com", ["104.26.4.164"], "proxy", "http://x", "s")
    out = trace._strip_ansi(capsys.readouterr().out)
    assert "活跃连接 1 条" in out
    assert "✓ 结论: 全部 1 条都走 proxy" in out


def test_section_connections_falls_back_to_ip_when_no_host(monkeypatch, capsys):
    """fake-ip 模式 metadata 无 host 时，回退按 destinationIP 匹配。"""
    _mock_connections(monkeypatch, [
        _conn("", _PROXY_CHAIN, ip="198.18.0.5"),
    ])
    trace._section_connections(
        "aicodewith.com", ["198.18.0.5"], "proxy", "http://x", "s")
    out = trace._strip_ansi(capsys.readouterr().out)
    assert "活跃连接 1 条" in out


def test_section_connections_ip_fallback_excludes_hosted_other_site(
        monkeypatch, capsys):
    """回归（codex P2）：目标站没有 host 匹配连接时，同 IP 上 host 指向别站的
    连接不能被 IP 回退误收 —— 否则共享 IP 误报在这个边角依然复现。"""
    _mock_connections(monkeypatch, [
        # 同一个 Cloudflare 共享 IP，但 host 非空且指向别的站点
        _conn("other-site.com", ["DIRECT"], up=999, down=999, ip="104.26.4.164"),
    ])
    # 隔离日志兜底，避免读到真实 mihomo.log 造成测试不稳定
    monkeypatch.setattr(trace, "_grep_log_connections", lambda domain: [])
    trace._section_connections(
        "aicodewith.com", ["104.26.4.164"], "proxy", "http://x", "s")
    out = trace._strip_ansi(capsys.readouterr().out)
    # 别站连接被排除 → 视为无活跃连接，绝不把别站的 DIRECT 链路算成本域名
    assert "无活跃连接" in out
    assert "DIRECT" not in out
    assert "other-site" not in out


def test_section_connections_sniffhost_match_included(monkeypatch, capsys):
    """sniffer 模式：host 为空但 sniffHost 指向目标域名 → 应识别为目标连接。"""
    _mock_connections(monkeypatch, [
        _conn("", _PROXY_CHAIN, ip="104.26.4.164", sniff="aicodewith.com"),
    ])
    trace._section_connections(
        "aicodewith.com", ["104.26.4.164"], "proxy", "http://x", "s")
    out = trace._strip_ansi(capsys.readouterr().out)
    assert "活跃连接 1 条" in out
    assert "✓ 结论: 全部 1 条都走 proxy" in out


def test_section_connections_ip_fallback_excludes_sniffhost_other_site(
        monkeypatch, capsys):
    """回归（codex P2#2）：host 为空但 sniffHost 指向别站的同 IP 连接，
    不能被 IP 回退误收 —— sniffHost 必须先折进有效 host 再决定是否回退。"""
    _mock_connections(monkeypatch, [
        _conn("", ["DIRECT"], up=999, down=999, ip="104.26.4.164",
              sniff="other-site.com"),
    ])
    monkeypatch.setattr(trace, "_grep_log_connections", lambda domain: [])
    trace._section_connections(
        "aicodewith.com", ["104.26.4.164"], "proxy", "http://x", "s")
    out = trace._strip_ansi(capsys.readouterr().out)
    assert "无活跃连接" in out
    assert "DIRECT" not in out
    assert "other-site" not in out


def test_section_connections_sniffhost_match_when_host_is_ip(monkeypatch, capsys):
    """回归（codex P2#3）：host 被填成目的 IP、真实域名在 sniffHost 时，
    host 非空也不能遮蔽 sniffHost —— 两字段独立判断，目标连接仍应纳入。"""
    _mock_connections(monkeypatch, [
        _conn("104.26.4.164", _PROXY_CHAIN, ip="104.26.4.164",
              sniff="aicodewith.com"),
    ])
    trace._section_connections(
        "aicodewith.com", ["104.26.4.164"], "proxy", "http://x", "s")
    out = trace._strip_ansi(capsys.readouterr().out)
    assert "活跃连接 1 条" in out
    assert "✓ 结论: 全部 1 条都走 proxy" in out
