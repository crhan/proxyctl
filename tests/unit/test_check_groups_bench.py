"""测试 check._proxy_groups_section, cmd_bench, _fetch_probe — 涉及 Clash API。

这些函数都是 API + 打印，重点验证：
- API 不可达/响应错误时不抛异常
- group 类型分支不抛异常
- bench 的 group/member 过滤
- _fetch_probe 的 extract_re / mode 分支
"""

from __future__ import annotations

import json

import pytest

from proxyctl import check
from proxyctl.core.plugin import OutboundProbe


# ────────────────────────────────────────────────────────────────────────────
# _proxy_groups_section
# ────────────────────────────────────────────────────────────────────────────

def test_proxy_groups_section_api_unreachable(fake_subprocess, capsys):
    fake_subprocess.set_default(stdout="", returncode=0)
    out = check._proxy_groups_section("http://x", "secret")
    assert out is False
    assert "不可达" in capsys.readouterr().out


def test_proxy_groups_section_bad_json(fake_subprocess, capsys):
    fake_subprocess.set_default(stdout="not json")
    out = check._proxy_groups_section("http://x", "secret")
    assert out is False
    assert "解析失败" in capsys.readouterr().out


def test_proxy_groups_section_selector_with_subgroups(fake_subprocess, capsys):
    """完整的多组+子组结构，跑过所有分支不报错。"""
    payload = {"proxies": {
        "proxy": {
            "type": "Selector", "now": "auto",
            "all": ["auto", "node-direct"],
        },
        "auto": {
            "type": "URLTest", "now": "hk1",
            "all": ["hk1", "us1"],
        },
        "hk1": {
            "type": "Shadowsocks",
            "history": [{"delay": 50, "time": "2026-05-14T03:00:00Z"}],
        },
        "us1": {
            "type": "Shadowsocks",
            "history": [{"delay": 0, "time": "2026-05-14T03:00:00Z"}],
        },
        "node-direct": {"type": "Direct", "history": []},
    }}
    fake_subprocess.set_default(stdout=json.dumps(payload))
    assert check._proxy_groups_section("http://x", "s", groups=["proxy"]) is True
    out = capsys.readouterr().out
    assert "proxy" in out
    assert "hk1" in out


def test_proxy_groups_section_unknown_group_skipped(fake_subprocess, capsys):
    payload = {"proxies": {}}
    fake_subprocess.set_default(stdout=json.dumps(payload))
    assert check._proxy_groups_section("http://x", "s", groups=["ghost"]) is True


def test_proxy_groups_section_fallback_type(fake_subprocess):
    payload = {"proxies": {
        "fb": {"type": "Fallback", "now": "n1", "all": ["n1"]},
        "n1": {"type": "Shadowsocks",
               "history": [{"delay": 100, "time": "bad-time"}]},
    }}
    fake_subprocess.set_default(stdout=json.dumps(payload))
    assert check._proxy_groups_section("http://x", "s", groups=["fb"]) is True


def test_proxy_groups_section_default_group_when_none(fake_subprocess):
    """groups=None 时回落 ["proxy"]。"""
    payload = {"proxies": {
        "proxy": {"type": "Selector", "now": "n", "all": ["n"]},
        "n": {"type": "Direct", "history": []},
    }}
    fake_subprocess.set_default(stdout=json.dumps(payload))
    assert check._proxy_groups_section("http://x", "s") is True


def test_rule_target_groups_from_mihomo_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
proxy-groups:
  - name: proxy
    type: select
  - name: claude
    type: fallback
  - name: residential-sg
    type: url-test
rules:
  - DOMAIN-SUFFIX,anthropic.com,claude
  - DOMAIN-SUFFIX,github.com,proxy
  - IP-CIDR,1.1.1.1/32,DIRECT,no-resolve
  - MATCH,proxy
""",
        encoding="utf-8",
    )

    assert check._rule_target_groups_from_config(str(cfg)) == ["claude", "proxy"]


def test_merge_check_groups_adds_rule_targets():
    assert check._merge_check_groups(["proxy"], ["claude", "proxy"]) == [
        "proxy",
        "claude",
    ]


def test_proxy_groups_section_shows_sibling_subgroups(fake_subprocess, capsys):
    """v0.5.5：fallback/selector 含多个子组时，每个子组都要打 summary 行
    （让用户看到 now 之外的兄弟子组存在），但只对 now 子组展开叶子。
    混在子组里的真节点成员也要打出来。"""
    payload = {"proxies": {
        "claude": {
            "type": "Fallback", "now": "rsi-us",
            "all": ["rsi-sg", "rsi-us", "local-13659"],
        },
        "rsi-sg": {"type": "URLTest", "now": "SG-01",
                   "all": ["SG-01", "SG-02"]},
        "rsi-us": {"type": "URLTest", "now": "US-01",
                   "all": ["US-01", "US-02"]},
        "SG-01": {"type": "Shadowsocks",
                  "history": [{"delay": 80, "time": "2026-05-21T00:00:00Z"}]},
        "SG-02": {"type": "Shadowsocks",
                  "history": [{"delay": 0, "time": "2026-05-21T00:00:00Z"}]},
        "US-01": {"type": "Shadowsocks",
                  "history": [{"delay": 200, "time": "2026-05-21T00:00:00Z"}]},
        "US-02": {"type": "Shadowsocks",
                  "history": [{"delay": 0, "time": "2026-05-21T00:00:00Z"}]},
        "local-13659": {"type": "Direct", "history": []},
    }}
    fake_subprocess.set_default(stdout=json.dumps(payload))
    assert check._proxy_groups_section("http://x", "s",
                                       groups=["claude"]) is True
    import re
    plain = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    # 兄弟子组必须可见
    assert "rsi-sg(url)" in plain, "兄弟子组 summary 行缺失"
    assert "rsi-us(url)" in plain
    # now 子组的叶子展开
    assert "US-01:200" in plain
    assert "US-02:✗" in plain
    # 非 now 子组的叶子不应该展开（精简意图保留）
    assert "SG-01:80" not in plain
    assert "SG-02:✗" not in plain
    # 真节点成员也要可见
    assert "local-13659" in plain


def test_proxy_groups_section_dedup_subgroup_across_groups(fake_subprocess, capsys):
    """v0.5.4：多个组的 now 指向同一子组时，该子组节点列表只展开一次。"""
    payload = {"proxies": {
        "GA": {"type": "Selector", "now": "SHARED", "all": ["SHARED"]},
        "GB": {"type": "Selector", "now": "SHARED", "all": ["SHARED"]},
        "SHARED": {"type": "URLTest", "now": "n1", "all": ["n1", "n2", "n3"]},
        "n1": {"type": "Shadowsocks",
                "history": [{"delay": 100, "time": "2026-05-19T00:00:00Z"}]},
        "n2": {"type": "Shadowsocks",
                "history": [{"delay": 120, "time": "2026-05-19T00:00:00Z"}]},
        "n3": {"type": "Shadowsocks",
                "history": [{"delay": 140, "time": "2026-05-19T00:00:00Z"}]},
    }}
    fake_subprocess.set_default(stdout=json.dumps(payload))
    assert check._proxy_groups_section("http://x", "s", groups=["GA", "GB"]) is True
    out = capsys.readouterr().out
    # 节点 n1 只在 SHARED 第一次展开时出现，第二次折叠成 (详见上方)
    import re
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    assert plain.count("n1:100") == 1
    assert "(详见上方)" in plain


# ────────────────────────────────────────────────────────────────────────────
# cmd_bench
# ────────────────────────────────────────────────────────────────────────────

def test_cmd_bench_api_unreachable(fake_subprocess, capsys):
    fake_subprocess.set_default(stdout="", returncode=0)
    # 应该平静返回（不抛错）
    check.cmd_bench("http://x", "secret")
    assert "不可达" in capsys.readouterr().out


def test_cmd_bench_api_bad_json(fake_subprocess, capsys):
    fake_subprocess.set_default(stdout="bad", returncode=0)
    check.cmd_bench("http://x", "secret")
    assert "解析失败" in capsys.readouterr().out


def test_cmd_bench_no_groups_match(fake_subprocess, capsys):
    """指定的组都不存在。"""
    payload = {"proxies": {"other": {"type": "Direct", "all": ["n"]}}}
    fake_subprocess.set_default(stdout=json.dumps(payload))
    check.cmd_bench("http://x", "secret", groups=["nonexistent"])
    out = capsys.readouterr().out
    assert "不存在" in out or "无可测组" in out


def test_cmd_bench_empty_group(fake_subprocess, capsys):
    """空组或穿透叶子后仍为空 → 报"无可测叶子节点"（v0.5.3 文案变化）。"""
    payload = {"proxies": {"empty": {"type": "Selector", "all": []}}}
    fake_subprocess.set_default(stdout=json.dumps(payload))
    check.cmd_bench("http://x", "secret", groups=["empty"])
    out = capsys.readouterr().out
    assert "无可测叶子节点" in out


def test_cmd_bench_pseudo_only_group_skipped(fake_subprocess, capsys):
    """v0.5.3：只含 DIRECT/REJECT 的组应跳过（穿透后无可测叶子）。"""
    payload = {"proxies": {
        "proxy": {"type": "Selector", "now": "DIRECT", "all": ["DIRECT", "REJECT"]},
        "DIRECT": {"type": "Direct", "history": []},
        "REJECT": {"type": "Reject", "history": []},
    }}
    fake_subprocess.set_default(stdout=json.dumps(payload))
    check.cmd_bench("http://x", "secret", groups=["proxy"])
    assert "无可测叶子节点" in capsys.readouterr().out


def test_cmd_bench_dispatches_to_proxy_section(fake_subprocess, capsys, monkeypatch):
    """有可测组时应该走完测速 + 调用 _proxy_groups_section。"""
    payload = {"proxies": {
        "proxy": {"type": "Selector", "now": "n1", "all": ["n1", "n2"]},
        "n1": {"type": "Shadowsocks",
                "history": [{"delay": 50, "time": "2026-05-14T03:00:00Z"}]},
        "n2": {"type": "Shadowsocks",
                "history": [{"delay": 0, "time": "2026-05-14T03:00:00Z"}]},
    }}
    fake_subprocess.set_default(stdout=json.dumps(payload), returncode=0)

    called = {"count": 0}

    def fake_section(*a, **kw):
        called["count"] += 1
        return True

    monkeypatch.setattr(check, "_proxy_groups_section", fake_section)
    check.cmd_bench("http://x", "secret", groups=["proxy"])
    assert called["count"] == 1


def test_cmd_bench_dedup_across_groups(fake_subprocess, capsys, monkeypatch):
    """v0.5.3：多组共享同一节点时只测一次，输出含"去重省 N 次"。"""
    payload = {"proxies": {
        # GA / GB 都含 n1,n2,n3；GB 还有 n4
        "GA": {"type": "Selector", "now": "n1", "all": ["n1", "n2", "n3"]},
        "GB": {"type": "Selector", "now": "n1", "all": ["n1", "n2", "n3", "n4"]},
        "n1": {"type": "Shadowsocks", "history": []},
        "n2": {"type": "Shadowsocks", "history": []},
        "n3": {"type": "Shadowsocks", "history": []},
        "n4": {"type": "Shadowsocks", "history": []},
    }}
    fake_subprocess.set_default(stdout=json.dumps(payload), returncode=0)
    monkeypatch.setattr(check, "_proxy_groups_section", lambda *a, **k: True)
    check.cmd_bench("http://x", "secret", groups=["GA", "GB"])
    out = capsys.readouterr().out
    # 应该 4 个唯一节点（n1,n2,n3,n4），去重前 7 个，省 3 次
    assert "节点: " in out and "4" in out
    assert "去重省 3 次" in out


# ────────────────────────────────────────────────────────────────────────────
# _fetch_probe
# ────────────────────────────────────────────────────────────────────────────

def test_fetch_probe_proxy_mode(fake_subprocess):
    fake_subprocess.set_default(stdout="1.2.3.4\n", returncode=0)
    out = check._fetch_probe(OutboundProbe(name="x", mode="proxy"), {})
    assert out == "1.2.3.4"
    last = fake_subprocess.calls[-1]
    assert "--proxy" in last


def test_fetch_probe_direct_mode(fake_subprocess):
    fake_subprocess.set_default(stdout="5.6.7.8\n")
    check._fetch_probe(OutboundProbe(name="x", mode="direct"), {})
    last = fake_subprocess.calls[-1]
    assert "--noproxy" in last


def test_fetch_probe_extract_re(fake_subprocess):
    fake_subprocess.set_default(stdout='{"ip": "9.9.9.9", "other": "x"}')
    out = check._fetch_probe(
        OutboundProbe(name="x", extract_re=r"\d+\.\d+\.\d+\.\d+"),
        {})
    assert out == "9.9.9.9"


def test_fetch_probe_extract_re_no_match(fake_subprocess):
    fake_subprocess.set_default(stdout="nothing")
    out = check._fetch_probe(
        OutboundProbe(name="x", extract_re=r"\d+\.\d+\.\d+\.\d+"),
        {})
    assert out == ""


def test_fetch_probe_timeout(monkeypatch):
    import subprocess as _sp

    def boom(*a, **kw):
        raise _sp.TimeoutExpired("curl", 5)

    monkeypatch.setattr(_sp, "run", boom)
    out = check._fetch_probe(OutboundProbe(name="x"), {})
    assert out == ""


def test_target_uses_expected_proxy(fake_subprocess):
    fake_subprocess.set_default(stdout=json.dumps({
        "connections": [{
            "metadata": {"host": "api.anthropic.com"},
            "chains": ["SG-Residential-01", "residential-sg", "claude"],
        }]
    }))
    ok, msg = check._target_uses_expected_proxy(
        "http://127.0.0.1:9090", "", "https://api.anthropic.com", "claude")
    assert ok is True
    assert "claude" in msg
    assert "SG-Residential-01" in msg


def test_target_route_extracts_leaf_line(fake_subprocess):
    fake_subprocess.set_default(stdout=json.dumps({
        "connections": [{
            "metadata": {"host": "api.anthropic.com"},
            "chains": ["TW-Residential-01", "residential-tw", "claude"],
        }]
    }))
    route = check._target_route(
        "http://127.0.0.1:9090", "", "https://api.anthropic.com/v1/models")
    assert route["found"] is True
    assert route["line"] == "TW-Residential-01"
    assert route["group"] == "claude"
    assert route["chain"] == "claude → residential-tw → TW-Residential-01"


def test_target_route_no_active_connection(fake_subprocess):
    fake_subprocess.set_default(stdout=json.dumps({"connections": []}))
    route = check._target_route(
        "http://127.0.0.1:9090", "", "https://api.anthropic.com")
    assert route == {"found": False, "line": "?", "group": "", "chain": ""}


def test_target_uses_expected_proxy_mismatch(fake_subprocess):
    fake_subprocess.set_default(stdout=json.dumps({
        "connections": [{
            "metadata": {"host": "api.anthropic.com"},
            "chains": ["日本1", "proxy"],
        }]
    }))
    ok, msg = check._target_uses_expected_proxy(
        "http://127.0.0.1:9090", "", "https://api.anthropic.com", "claude")
    assert ok is False
    assert "expected claude" in msg
