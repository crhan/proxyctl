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
    assert suggest_rules.controller_rules(_ctrl_cfg()) == []


def test_controller_empty_secret_triggers_warn():
    out = suggest_rules.controller_rules(_ctrl_cfg(controller_secret=""))
    s = [x for x in out if x["id"] == "controller.empty_secret"][0]
    assert s["severity"] == "warn"
    assert s["evidence"]["secret_set"] is False


def test_controller_missing_secret_treated_as_empty():
    out = suggest_rules.controller_rules(_ctrl_cfg(controller_secret=None))
    assert "controller.empty_secret" in _ids(out)


def test_controller_weak_secret_triggers_advisory():
    out = suggest_rules.controller_rules(_ctrl_cfg(controller_secret="short1"))
    s = [x for x in out if x["id"] == "controller.weak_secret"][0]
    assert s["severity"] == "advisory"
    assert s["evidence"]["secret_length"] == 6


def test_controller_public_bind_triggers_warn():
    out = suggest_rules.controller_rules(_ctrl_cfg(controller_host="0.0.0.0"))
    s = [x for x in out if x["id"] == "controller.public_bind"][0]
    assert s["severity"] == "warn"


def test_controller_localhost_no_public_bind():
    out = suggest_rules.controller_rules(_ctrl_cfg(controller_host="127.0.0.1"))
    assert "controller.public_bind" not in _ids(out)
    out = suggest_rules.controller_rules(_ctrl_cfg(controller_host="::1"))
    assert "controller.public_bind" not in _ids(out)


def test_controller_lan_ip_treated_as_public():
    out = suggest_rules.controller_rules(_ctrl_cfg(controller_host="192.168.1.10"))
    assert "controller.public_bind" in _ids(out)


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
