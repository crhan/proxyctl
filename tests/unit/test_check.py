"""测试 check.py — 连通性测试 + 出口探测的 helper 函数。

这些函数都是 subprocess wrapper，关键是验证：
- 入参 → curl 命令构造是否正确
- 返回值 (ok, line) 二元组的语义
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from proxyctl import check


# ────────────────────────────────────────────────────────────────────────────
# _port_listening：用一个临时 TCP server 来真测
# ────────────────────────────────────────────────────────────────────────────

def test_port_listening_true():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert check._port_listening(port) is True
    finally:
        s.close()


def test_port_listening_false():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    # 端口已经释放，应该 false
    assert check._port_listening(port) is False


# ────────────────────────────────────────────────────────────────────────────
# _test_url：mock subprocess，验证 curl 命令拼接 & 各种 http code 分类
# ────────────────────────────────────────────────────────────────────────────

def test_test_url_proxy_mode_builds_socks(fake_subprocess):
    fake_subprocess.set_default(stdout="200")
    ok, line = check._test_url("https://x", "google", mode="proxy")
    assert ok
    last_cmd = fake_subprocess.calls[-1]
    assert "--http1.1" in last_cmd
    assert "--proxy" in last_cmd
    assert "socks5h://127.0.0.1:7890" in last_cmd
    assert "200" in line


def test_test_url_direct_mode_bypasses_proxy(fake_subprocess):
    fake_subprocess.set_default(stdout="204")
    ok, line = check._test_url("https://baidu", "baidu", mode="direct")
    assert ok
    last_cmd = fake_subprocess.calls[-1]
    assert "--noproxy" in last_cmd
    assert "*" in last_cmd


def test_test_url_4xx_treated_as_reachable(fake_subprocess):
    """4xx 也算"链路通了"，因为服务端返回了响应。"""
    fake_subprocess.set_default(stdout="403")
    ok, _ = check._test_url("https://x", "x")
    assert ok


def test_test_url_5xx_yellow_but_ok(fake_subprocess):
    """5xx 视为服务端错误，链路依然 ok（绿/黄色不影响成败）。"""
    fake_subprocess.set_default(stdout="503")
    ok, line = check._test_url("https://x", "x")
    assert ok
    assert "server error" in line


def test_test_url_000_timeout(fake_subprocess):
    fake_subprocess.set_default(stdout="000")
    ok, line = check._test_url("https://x", "x")
    assert ok is False
    assert "timeout" in line


def test_test_url_empty_stdout_means_failure(fake_subprocess):
    fake_subprocess.set_default(stdout="")
    ok, _ = check._test_url("https://x", "x")
    assert ok is False


def test_test_url_000_includes_curl_error(fake_subprocess):
    fake_subprocess.set_default(
        stdout="000",
        stderr=("curl: (16) Remote peer returned unexpected data while we "
                "expected SETTINGS frame.\n"),
    )
    ok, line = check._test_url("https://x", "x")
    assert ok is False
    assert "Remote peer returned unexpected data" in line


def test_test_url_unknown_code(fake_subprocess):
    """非 2/3/4/5xx 返回（如 100）→ 黄色 ?."""
    fake_subprocess.set_default(stdout="100")
    ok, line = check._test_url("https://x", "x")
    assert ok is False
    assert "?" in line


def test_test_url_strips_proxy_env(monkeypatch, fake_subprocess):
    """env 里不应留 http_proxy 之类（避免影响 curl）。"""
    monkeypatch.setenv("HTTP_PROXY", "http://bad")
    fake_subprocess.set_default(stdout="200")

    captured_env = {}

    real_run = fake_subprocess._resolve  # noqa: SLF001

    import subprocess as _sp

    def fake_run(cmd, *args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return real_run(cmd)

    monkeypatch.setattr(_sp, "run", fake_run)
    check._test_url("https://x", "x")
    assert "HTTP_PROXY" not in captured_env
    assert "http_proxy" not in captured_env


def test_append_route_column_prefers_route_chain():
    line = check._append_route_column(
        "  ✓ anthropic https://x 401",
        "TW-Residential-01",
        "claude → TW-Residential-01",
    )
    assert "via" in line
    assert "claude → TW-Residential-01" in line
    assert "线路" not in line


def test_append_route_column_falls_back_to_route_line():
    line = check._append_route_column("  ✓ baidu https://x 200", "direct")
    assert "via" in line
    assert "direct" in line


def test_append_route_column_aligns_via_by_visible_width():
    short_status = check._append_route_column(
        "  ✓ corp-dns           30.64.127.127                                ok",
        "-",
    )
    long_status = check._append_route_column(
        "  ✓ anthropic          https://api.anthropic.com/v1/models          401",
        "TW-Residential-01",
        "claude → TW-Residential-01",
    )
    assert check._strip_ansi(short_status).index("via") == \
        check._strip_ansi(long_status).index("via")


# ────────────────────────────────────────────────────────────────────────────
# _test_tcp：纯 socket 连接
# ────────────────────────────────────────────────────────────────────────────

def test_test_tcp_ok():
    """连一个临时监听 socket。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        ok, line = check._test_tcp("127.0.0.1", port, "loopback")
        assert ok
        assert "ok" in line
    finally:
        s.close()


def test_test_tcp_fail():
    """连一个已关闭端口 → 失败。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    ok, line = check._test_tcp("127.0.0.1", port, "dead")
    assert ok is False
    assert "unreachable" in line


# ────────────────────────────────────────────────────────────────────────────
# _test_dns：dig 子进程
# ────────────────────────────────────────────────────────────────────────────

def test_test_dns_ok(fake_subprocess):
    fake_subprocess.set_default(stdout="1.2.3.4\n", returncode=0)
    ok, line = check._test_dns("corp-dns", "10.0.0.53", "wiki.corp")
    assert ok
    assert "ok" in line


def test_test_dns_empty_output_fail(fake_subprocess):
    fake_subprocess.set_default(stdout="", returncode=0)
    ok, line = check._test_dns("corp-dns", "10.0.0.53", "wiki.corp")
    assert ok is False
    assert "timeout" in line


def test_test_dns_nonzero_fail(fake_subprocess):
    fake_subprocess.set_default(stdout="some output", returncode=9)
    ok, _ = check._test_dns("x", "y", "z")
    assert ok is False


# ────────────────────────────────────────────────────────────────────────────
# _fmt_ip: 给 IP 加 geo 标签
# ────────────────────────────────────────────────────────────────────────────

def test_fmt_ip_with_country():
    s = check._fmt_ip("1.2.3.4", "US")
    assert "1.2.3.4" in s
    assert "US" in s


def test_fmt_ip_empty_geo():
    """空 geo 不该把 [] 渲染出来。"""
    s = check._fmt_ip("1.2.3.4", "")
    # 不强制具体格式，只验不带方括号空值
    assert "[]" not in s


# ────────────────────────────────────────────────────────────────────────────
# _ipgeo: 文件缓存语义
# ────────────────────────────────────────────────────────────────────────────

def test_ipgeo_empty_ip_returns_empty():
    assert check._ipgeo("", "/nowhere", "secret") == ""


def test_ipgeo_uses_line_cache(tmp_path: Path, fake_subprocess):
    """cache 是 line 格式 'ip|city,country|org'。"""
    cache = tmp_path / "geo.txt"
    cache.write_text("1.1.1.1|Sydney,AU|Cloudflare\n")
    fake_subprocess.set_default(stdout="ignored")  # cache 命中不调 subprocess
    assert check._ipgeo("1.1.1.1", str(cache), "secret") == "Sydney,AU|Cloudflare"


def test_ipgeo_fetches_and_appends_cache(tmp_path: Path, fake_subprocess):
    cache = tmp_path / "geo.txt"
    fake_subprocess.set_default(
        stdout='{"status": "success", "city": "Tokyo", "countryCode": "JP", '
               '"isp": "AS123 Foo Inc"}',
        returncode=0,
    )
    out = check._ipgeo("2.2.2.2", str(cache), "secret")
    # org 去掉 ASN 前缀；loc 用 city,country
    assert out == "Tokyo,JP|Foo Inc"
    content = cache.read_text()
    assert content.startswith("2.2.2.2|Tokyo,JP|Foo Inc")
    last = fake_subprocess.calls[-1]
    assert "--noproxy" in last
    assert "--proxy" not in last


def test_ipgeo_bad_json_returns_empty(tmp_path: Path, fake_subprocess):
    cache = tmp_path / "geo.txt"
    fake_subprocess.set_default(stdout="not json", returncode=0)
    assert check._ipgeo("3.3.3.3", str(cache), "secret") == ""


def test_ipgeo_keeps_non_asn_isp_prefix(tmp_path: Path, fake_subprocess):
    cache = tmp_path / "geo.txt"
    fake_subprocess.set_default(
        stdout='{"status": "success", "city": "Shanghai", '
               '"countryCode": "CN", "isp": "Hangzhou Alibaba Advertising Co"}',
        returncode=0,
    )
    assert check._ipgeo("4.4.4.4", str(cache), "secret") == (
        "Shanghai,CN|Hangzhou Alibaba Advertising Co"
    )


# ────────────────────────────────────────────────────────────────────────────
# _collect_fail_hints — v0.4.3 体感改进：envelope.ok=False 时给 agent 摘要
# ────────────────────────────────────────────────────────────────────────────

def _make_collector(*, daemon_up=True, fail_ports=None,
                    connectivity=None, split_ok=None):
    """构造与 cmd_check.collector 同构的最小快照。"""
    coll: dict = {
        "stages": {
            "basic": {
                "daemon_up": daemon_up,
                "ports": {"ok": [], "fail": fail_ports or []},
            },
            "connectivity": connectivity or [],
        }
    }
    if split_ok is not None:
        coll["stages"]["split_routing"] = {"ok": split_ok}
    return coll


def test_collect_fail_hints_returns_empty_when_pass():
    coll = _make_collector(connectivity=[{"name": "a", "ok": True}])
    assert check._collect_fail_hints(coll, dns_bad=False, failed=False) == []


def test_collect_fail_hints_connectivity_failed_names_listed():
    coll = _make_collector(connectivity=[
        {"name": "discord", "ok": False, "message": "timeout"},
        {"name": "google",  "ok": True,  "message": "..."},
        {"name": "github",  "ok": False, "message": "timeout"},
    ])
    hints = check._collect_fail_hints(coll, dns_bad=False, failed=True)
    # 必须含两个失败名（顺序保留）
    joined = " | ".join(hints)
    assert "connectivity failed" in joined
    assert "discord" in joined and "github" in joined
    assert "google" not in joined


def test_collect_fail_hints_dns_bad_appends_fix_hint():
    coll = _make_collector(connectivity=[{"name": "a", "ok": True}])
    hints = check._collect_fail_hints(coll, dns_bad=True, failed=True)
    assert any("DNS" in h and "proxyctl fix" in h for h in hints)


def test_collect_fail_hints_missing_ports_and_engine_down():
    coll = _make_collector(daemon_up=False, fail_ports=["proxy:7890"])
    hints = check._collect_fail_hints(coll, dns_bad=False, failed=True)
    assert any("missing ports" in h and "7890" in h for h in hints)
    assert any("engine not running" in h for h in hints)


def test_collect_fail_hints_split_routing_inactive():
    coll = _make_collector(connectivity=[{"name": "a", "ok": True}],
                           split_ok=False)
    hints = check._collect_fail_hints(coll, dns_bad=False, failed=True)
    assert any("split routing inactive" in h for h in hints)


def test_collect_fail_hints_aggregates_multiple_categories():
    """同时多种失败时 hints 全部聚合，agent 一眼看全。"""
    coll = _make_collector(
        daemon_up=True,
        fail_ports=["api:9090"],
        connectivity=[
            {"name": "x", "ok": False, "message": "timeout"},
        ],
        split_ok=False,
    )
    hints = check._collect_fail_hints(coll, dns_bad=True, failed=True)
    joined = " | ".join(hints)
    assert "missing ports" in joined
    assert "connectivity failed" in joined
    assert "split routing" in joined
    assert "DNS" in joined


def test_collect_fail_hints_dead_groups_listed():
    """proxy_group.mostly_dead 在 check 失败 hints 里聚合（0.5.3）。"""
    coll = _make_collector(connectivity=[{"name": "a", "ok": True}])
    coll["stages"]["dead_groups"] = [
        {"name": "GLOBAL", "dead_count": 15, "total_count": 15, "dead_pct": 100.0},
        {"name": "AUTO", "dead_count": 8, "total_count": 10, "dead_pct": 80.0},
    ]
    hints = check._collect_fail_hints(coll, dns_bad=False, failed=True)
    joined = " | ".join(hints)
    assert "proxy groups mostly dead" in joined
    assert "GLOBAL(15/15)" in joined
    assert "AUTO(8/10)" in joined
    assert "proxyctl bench" in joined


def test_collect_fail_hints_no_dead_groups_no_hint():
    coll = _make_collector(connectivity=[{"name": "a", "ok": False}])
    coll["stages"]["dead_groups"] = []
    hints = check._collect_fail_hints(coll, dns_bad=False, failed=True)
    assert not any("mostly dead" in h for h in hints)
