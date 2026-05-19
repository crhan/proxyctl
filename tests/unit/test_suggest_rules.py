"""测试 proxyctl.suggest_rules — controller / engine / data 规则（v0.5.0+）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from proxyctl import suggest_rules


def _ids(suggestions) -> list[str]:
    return [s["id"] for s in suggestions]


# ────────────────────────────────────────────────────────────────────────────
# inspect_engine_config
# ────────────────────────────────────────────────────────────────────────────

def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_inspect_config_loopback_with_secret(tmp_path):
    p = _write_yaml(tmp_path, (
        "external-controller: 127.0.0.1:9090\n"
        'secret: "abcdefghijklmnop12"\n'
    ))
    out = suggest_rules.inspect_engine_config(str(p))
    assert out["config_exists"] is True
    assert out["controller_host"] == "127.0.0.1"
    assert out["controller_port"] == 9090
    assert out["controller_secret"] == "abcdefghijklmnop12"


def test_inspect_config_public_bind(tmp_path):
    p = _write_yaml(tmp_path, "external-controller: 0.0.0.0:9090\nsecret: ''\n")
    out = suggest_rules.inspect_engine_config(str(p))
    assert out["controller_host"] == "0.0.0.0"
    assert out["controller_secret"] == ""


def test_inspect_config_short_form(tmp_path):
    p = _write_yaml(tmp_path, "external-controller: ':9091'\n")
    out = suggest_rules.inspect_engine_config(str(p))
    assert out["controller_host"] == "127.0.0.1"
    assert out["controller_port"] == 9091


def test_inspect_config_missing_file(tmp_path):
    out = suggest_rules.inspect_engine_config(str(tmp_path / "nope.yaml"))
    assert out["config_exists"] is False
    assert out["controller_host"] is None


# ────────────────────────────────────────────────────────────────────────────
# controller_rules — 3 条
# ────────────────────────────────────────────────────────────────────────────

def _ctrl_cfg(**overrides) -> dict:
    base = {
        "config_exists": True,
        "controller_host": "127.0.0.1",
        "controller_port": 9090,
        "controller_secret": "a-long-enough-secret-1234",
        "errors": [],
    }
    base.update(overrides)
    return base


def test_controller_rules_no_issues():
    """bind 127.0.0.1 + 强 secret → 无任何规则触发。"""
    assert suggest_rules.controller_rules(_ctrl_cfg()) == []


# ── v0.5.1：复合判定（bind 127.0.0.1 时 secret 强度无关，不报）─────────

def test_controller_localhost_weak_secret_NOT_reported():
    """bind 127.0.0.1 + 短 secret → 不报。哥 2026-05-19 提出的设计问题。"""
    out = suggest_rules.controller_rules(
        _ctrl_cfg(controller_host="127.0.0.1", controller_secret="short1"))
    assert out == [], f"expected []，实际: {_ids(out)}"


def test_controller_localhost_empty_secret_NOT_reported():
    """bind 127.0.0.1 + 空 secret → 不报。"""
    out = suggest_rules.controller_rules(
        _ctrl_cfg(controller_host="127.0.0.1", controller_secret=""))
    assert out == []


def test_controller_loopback_v6_weak_secret_NOT_reported():
    out = suggest_rules.controller_rules(
        _ctrl_cfg(controller_host="::1", controller_secret="x"))
    assert out == []


# ── public bind 才进入 secret 评估 ──────────────────────────────────

def test_controller_public_bind_strong_secret_only_warns_about_bind():
    """0.0.0.0 + 强 secret → 仅 public_bind warn，无 weak/empty。"""
    out = suggest_rules.controller_rules(
        _ctrl_cfg(controller_host="0.0.0.0",
                  controller_secret="a-very-long-strong-secret-1234567890"))
    assert _ids(out) == ["controller.public_bind"]
    assert out[0]["severity"] == "warn"


def test_controller_public_bind_empty_secret_double_warn():
    """0.0.0.0 + 空 secret → public_bind warn + empty_secret warn。"""
    out = suggest_rules.controller_rules(
        _ctrl_cfg(controller_host="0.0.0.0", controller_secret=""))
    ids = _ids(out)
    assert "controller.public_bind" in ids
    assert "controller.empty_secret" in ids
    empty = [x for x in out if x["id"] == "controller.empty_secret"][0]
    assert empty["severity"] == "warn"


def test_controller_public_bind_missing_secret_double_warn():
    """0.0.0.0 + 缺失 secret 字段 → 同空 secret。"""
    out = suggest_rules.controller_rules(
        _ctrl_cfg(controller_host="0.0.0.0", controller_secret=None))
    assert "controller.empty_secret" in _ids(out)


def test_controller_public_bind_weak_secret_advisory():
    """0.0.0.0 + 短 secret → public_bind warn + weak_secret advisory。"""
    out = suggest_rules.controller_rules(
        _ctrl_cfg(controller_host="0.0.0.0", controller_secret="short1"))
    ids = _ids(out)
    assert "controller.public_bind" in ids
    assert "controller.weak_secret" in ids
    weak = [x for x in out if x["id"] == "controller.weak_secret"][0]
    assert weak["severity"] == "advisory"
    assert weak["evidence"]["secret_length"] == 6


def test_controller_lan_ip_treated_as_public():
    out = suggest_rules.controller_rules(
        _ctrl_cfg(controller_host="192.168.1.10",
                  controller_secret="short"))
    assert "controller.public_bind" in _ids(out)
    assert "controller.weak_secret" in _ids(out)


def test_controller_no_controller_no_rules():
    """完全没配 external-controller 时不应骚扰用户。"""
    out = suggest_rules.controller_rules(_ctrl_cfg(
        controller_host=None, controller_port=None, controller_secret=None))
    assert out == []


def test_controller_config_missing_no_rules():
    """config 文件不存在时整组规则跳过。"""
    out = suggest_rules.controller_rules(_ctrl_cfg(config_exists=False))
    assert out == []


# ────────────────────────────────────────────────────────────────────────────
# engine_rules — engine.outdated
# ────────────────────────────────────────────────────────────────────────────

def test_engine_rules_skips_when_no_known():
    assert suggest_rules.engine_rules("1.18.10", None) == []


def test_engine_rules_skips_when_no_version():
    assert suggest_rules.engine_rules(None, {"safe_min_version": "1.0.0"}) == []


def test_engine_rules_safe_version_no_suggestion():
    out = suggest_rules.engine_rules(
        "1.19.20", {"safe_min_version": "1.18.0"})
    assert out == []


def test_engine_rules_below_safe_min_triggers_info():
    out = suggest_rules.engine_rules(
        "1.17.5", {"safe_min_version": "1.18.0"})
    s = out[0]
    assert s["id"] == "engine.outdated"
    assert s["severity"] == "info"
    assert s["evidence"]["current_version"] == "1.17.5"


def test_engine_rules_unsafe_version_triggers_warn():
    out = suggest_rules.engine_rules(
        "1.19.18",
        {"safe_min_version": "1.18.0",
         "unsafe_versions": ["1.19.18", "1.19.19"]})
    s = out[0]
    assert s["severity"] == "warn"


# ────────────────────────────────────────────────────────────────────────────
# load_known_versions — 契约文件读取
# ────────────────────────────────────────────────────────────────────────────

def test_load_known_versions_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXYCTL_KNOWN_VERSIONS_PATH",
                        str(tmp_path / "nope.json"))
    assert suggest_rules.load_known_versions() is None


def test_load_known_versions_happy(tmp_path, monkeypatch):
    p = tmp_path / "k.json"
    p.write_text(json.dumps({"safe_min_version": "1.18.0"}), encoding="utf-8")
    monkeypatch.setenv("PROXYCTL_KNOWN_VERSIONS_PATH", str(p))
    out = suggest_rules.load_known_versions()
    assert out == {"safe_min_version": "1.18.0"}


def test_load_known_versions_corrupt(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("not json{", encoding="utf-8")
    monkeypatch.setenv("PROXYCTL_KNOWN_VERSIONS_PATH", str(p))
    assert suggest_rules.load_known_versions() is None


# ────────────────────────────────────────────────────────────────────────────
# geo_rules
# ────────────────────────────────────────────────────────────────────────────

def test_geo_rules_no_dir():
    assert suggest_rules.geo_rules(None) == []
    assert suggest_rules.geo_rules("/nonexistent/dir") == []


def test_geo_rules_fresh_no_suggestion(tmp_path):
    (tmp_path / "geoip.dat").write_bytes(b"x")
    (tmp_path / "geosite.dat").write_bytes(b"x")
    assert suggest_rules.geo_rules(str(tmp_path)) == []


def test_geo_rules_stale_triggers(tmp_path):
    f = tmp_path / "geoip.dat"
    f.write_bytes(b"x")
    old = time.time() - 40 * 86400
    import os
    os.utime(f, (old, old))
    out = suggest_rules.geo_rules(str(tmp_path))
    assert len(out) == 1
    s = out[0]
    assert s["id"] == "data.geo_stale"
    assert s["severity"] == "info"
    stale_names = [e["name"] for e in s["evidence"]["stale_files"]]
    assert "geoip.dat" in stale_names


def test_geo_rules_partial_stale(tmp_path):
    """fresh geoip + stale geosite → 仅 geosite 出现在 evidence。"""
    import os
    fresh = tmp_path / "geoip.dat"
    fresh.write_bytes(b"x")
    stale = tmp_path / "geosite.dat"
    stale.write_bytes(b"x")
    old = time.time() - 40 * 86400
    os.utime(stale, (old, old))
    out = suggest_rules.geo_rules(str(tmp_path))
    names = [e["name"] for e in out[0]["evidence"]["stale_files"]]
    assert names == ["geosite.dat"]


# ────────────────────────────────────────────────────────────────────────────
# proxy_group_rules — mihomo /proxies API 派生
# ────────────────────────────────────────────────────────────────────────────

def _mk_node(delay: int) -> dict:
    return {
        "type": "Shadowsocks",
        "history": [{"time": "2026-05-19T00:00:00Z", "delay": delay}],
    }


def _mk_group(name: str, members: list[str], group_type: str = "URLTest") -> dict:
    return {"type": group_type, "all": members, "now": members[0] if members else ""}


def test_proxy_group_rules_skip_when_no_payload():
    assert suggest_rules.proxy_group_rules(None) == []
    assert suggest_rules.proxy_group_rules({}) == []
    assert suggest_rules.proxy_group_rules({"proxies": None}) == []


def test_proxy_group_rules_healthy_group_no_suggestion():
    payload = {"proxies": {
        "GROUP1": _mk_group("GROUP1", ["n1", "n2", "n3", "n4"]),
        "n1": _mk_node(200), "n2": _mk_node(180),
        "n3": _mk_node(250), "n4": _mk_node(220),
    }}
    assert suggest_rules.proxy_group_rules(payload) == []


def test_proxy_group_rules_mostly_dead_triggers():
    payload = {"proxies": {
        "GROUP1": _mk_group("GROUP1", ["n1", "n2", "n3", "n4"]),
        "n1": _mk_node(0), "n2": _mk_node(0),    # 死
        "n3": _mk_node(0), "n4": _mk_node(180),  # 1/4 活
    }}
    out = suggest_rules.proxy_group_rules(payload)
    assert len(out) == 1
    s = out[0]
    assert s["id"] == "proxy_group.mostly_dead"
    assert s["severity"] == "warn"
    assert s["evidence"]["group_name"] == "GROUP1"
    assert s["evidence"]["dead_count"] == 3
    assert s["evidence"]["total_count"] == 4


def test_proxy_group_rules_small_group_skipped():
    """< 3 节点的组不报（误报率高）。"""
    payload = {"proxies": {
        "GROUP1": _mk_group("GROUP1", ["n1", "n2"]),
        "n1": _mk_node(0), "n2": _mk_node(0),
    }}
    assert suggest_rules.proxy_group_rules(payload) == []


def test_proxy_group_rules_non_dispatch_type_skipped():
    """Shadowsocks 直接节点不参与分发，不该被判定为'组'。"""
    payload = {"proxies": {
        "n1": _mk_node(0), "n2": _mk_node(0), "n3": _mk_node(0),
    }}
    assert suggest_rules.proxy_group_rules(payload) == []


def test_proxy_group_rules_empty_history_counts_as_dead():
    payload = {"proxies": {
        "GROUP1": _mk_group("GROUP1", ["n1", "n2", "n3", "n4"]),
        "n1": {"type": "Shadowsocks", "history": []},  # 无历史 = 死
        "n2": {"type": "Shadowsocks", "history": []},
        "n3": {"type": "Shadowsocks", "history": []},
        "n4": _mk_node(180),
    }}
    out = suggest_rules.proxy_group_rules(payload)
    assert len(out) == 1
    assert out[0]["evidence"]["dead_count"] == 3


def test_proxy_group_rules_multiple_groups_separate_suggestions():
    """两个组各自挂掉 → 两条独立 suggestion，fingerprint 不同。"""
    payload = {"proxies": {
        "GROUP_A": _mk_group("GROUP_A", ["a1", "a2", "a3"]),
        "a1": _mk_node(0), "a2": _mk_node(0), "a3": _mk_node(0),
        "GROUP_B": _mk_group("GROUP_B", ["b1", "b2", "b3", "b4"]),
        "b1": _mk_node(0), "b2": _mk_node(0),
        "b3": _mk_node(0), "b4": _mk_node(100),
    }}
    out = suggest_rules.proxy_group_rules(payload)
    assert len(out) == 2
    group_names = {s["evidence"]["group_name"] for s in out}
    assert group_names == {"GROUP_A", "GROUP_B"}


# ────────────────────────────────────────────────────────────────────────────
# fetch_proxies — HTTP 静默降级
# ────────────────────────────────────────────────────────────────────────────

def test_fetch_proxies_unreachable_returns_none(tmp_path):
    """API 不通时返回 None，不抛异常。"""
    out = suggest_rules.fetch_proxies("http://127.0.0.1:1",  # 几乎不可能的端口
                                       api_secret="", timeout=0.1)
    assert out is None


def test_fetch_proxies_no_api_base_returns_none():
    assert suggest_rules.fetch_proxies("", "") is None
    assert suggest_rules.fetch_proxies(None, "") is None
