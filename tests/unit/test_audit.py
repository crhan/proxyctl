"""测试 audit.py 纯逻辑 + 文件 IO 部分。

模块级常量（SB_LOG/MH_LOG/SB_CONFIG/MH_CONFIG/IPGEO_CACHE_FILE）在 import 时
就计算好。每个测试通过 monkeypatch 重新指向 tmp_path，避免接触真实文件。

不测的部分：cmd_audit（end-to-end，包括 curl 网络调用），会在 cli 集成测试里覆盖。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from proxyctl import audit


# ────────────────────────────────────────────────────────────────────────────
# _is_valid_domain
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host,expected", [
    ("example.com", True),
    ("api.example.co.uk", True),
    ("192.168.1.1", False),       # 纯 IPv4
    ("localhost", False),          # 无点
    ("123.456", False),            # TLD 全数字
    ("www.gstatic.com", False),    # 黑名单
    ("cp.cloudflare.com", False),  # 黑名单
])
def test_is_valid_domain(host, expected):
    assert audit._is_valid_domain(host) is expected


# ────────────────────────────────────────────────────────────────────────────
# _is_covered: 后缀匹配
# ────────────────────────────────────────────────────────────────────────────

def test_is_covered_exact():
    assert audit._is_covered("example.com", {"example.com"}) is True


def test_is_covered_subdomain():
    """api.example.com 应被 example.com 后缀规则覆盖。"""
    assert audit._is_covered("api.example.com", {"example.com"}) is True


def test_is_covered_no_match():
    assert audit._is_covered("api.example.com", {"other.com"}) is False


def test_is_covered_does_not_match_unrelated_suffix():
    """myexample.com 不应被 example.com 规则覆盖（必须是 .example.com）。"""
    assert audit._is_covered("myexample.com", {"example.com"}) is False


# ────────────────────────────────────────────────────────────────────────────
# _scan_log: 双引擎日志解析
# ────────────────────────────────────────────────────────────────────────────

def test_scan_log_missing_file(tmp_path: Path):
    out = audit._scan_log(str(tmp_path / "no.log"), "mihomo", 1)
    assert out == {}


def test_scan_log_mihomo_ok_rule(tmp_path: Path):
    log = tmp_path / "mihomo.log"
    log.write_text(
        "08:00:01 INF [TCP] 1.2.3.4:1024 --> example.com:443 match Match using auto[hk]\n"
        "08:00:02 INF [TCP] 1.2.3.4:1025 --> direct.cn:443 match Match using DIRECT\n"
        "08:00:03 INF [TCP] 1.2.3.4:1026 --> reject.x:443 match Match using REJECT\n"
        "08:00:04 INF [TCP] 1.2.3.4:1027 --> github.com:443 match Match using proxy[us]\n"
    )
    out = audit._scan_log(str(log), "mihomo", 1)
    # DIRECT / REJECT 不计入；其余两个被识别
    assert set(out.keys()) == {"example.com", "github.com"}
    assert out["example.com"] == 1


def test_scan_log_mihomo_err_rule(tmp_path: Path):
    log = tmp_path / "mihomo.log"
    log.write_text(
        "08:00:01 ERR [TCP] dial proxy 1.2.3.4:1024 --> stuck.com:443 error: timeout\n"
    )
    out = audit._scan_log(str(log), "mihomo", 1)
    assert out == {"stuck.com": 1}


def test_scan_log_singbox(tmp_path: Path):
    log = tmp_path / "sb.err"
    log.write_text(
        "08:00:01 INFO outbound/tuic[hk]: outbound connection to a.com:443\n"
        "08:00:02 INFO outbound/shadowsocks[us]: outbound connection to b.com:443\n"
        "08:00:03 INFO outbound/direct[]: outbound connection to direct.cn:443\n"
    )
    out = audit._scan_log(str(log), "singbox", 1)
    assert out == {"a.com": 1, "b.com": 1}


def test_scan_log_ignores_invalid_hosts(tmp_path: Path):
    log = tmp_path / "mihomo.log"
    log.write_text(
        "[TCP] x --> 1.2.3.4:443 match Match using proxy[hk]\n"           # 纯 IP
        "[TCP] x --> www.gstatic.com:443 match Match using proxy[hk]\n"   # 黑名单
    )
    out = audit._scan_log(str(log), "mihomo", 1)
    assert out == {}


# ────────────────────────────────────────────────────────────────────────────
# _load_geo_cache / _save_geo_cache
# ────────────────────────────────────────────────────────────────────────────

def test_geo_cache_roundtrip(tmp_path: Path, monkeypatch):
    cache = tmp_path / "cache.json"
    monkeypatch.setattr(audit, "IPGEO_CACHE_FILE", str(cache))

    audit._save_geo_cache({"1.1.1.1": "US", "8.8.8.8": "US"})
    loaded = audit._load_geo_cache()
    assert loaded == {"1.1.1.1": "US", "8.8.8.8": "US"}


def test_load_geo_cache_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(audit, "IPGEO_CACHE_FILE", str(tmp_path / "nope"))
    assert audit._load_geo_cache() == {}


# ────────────────────────────────────────────────────────────────────────────
# _ip_country / _resolve_direct: subprocess + 缓存
# ────────────────────────────────────────────────────────────────────────────

def test_ip_country_uses_cache(monkeypatch, fake_subprocess):
    monkeypatch.setattr(audit, "_geo_cache", {"1.1.1.1": "US"})
    fake_subprocess.set_default(stdout="CN", returncode=0)   # 永远不会被调
    assert audit._ip_country("1.1.1.1") == "US"
    assert fake_subprocess.calls == []   # 缓存命中，不调 subprocess


def test_ip_country_fetches_and_caches(monkeypatch, fake_subprocess):
    monkeypatch.setattr(audit, "_geo_cache", {"1.1.1.1": "US"})  # 非空避免 _load
    fake_subprocess.set_default(stdout="CN\n", returncode=0)
    assert audit._ip_country("2.2.2.2") == "CN"


def test_ip_country_handles_bad_response(monkeypatch, fake_subprocess):
    monkeypatch.setattr(audit, "_geo_cache", {"x": "y"})
    fake_subprocess.set_default(stdout="<HTML 503>", returncode=0)
    assert audit._ip_country("3.3.3.3") == ""


def test_is_cn_ip(monkeypatch):
    monkeypatch.setattr(audit, "_ip_country", lambda ip: "CN" if ip == "9.9.9.9" else "US")
    assert audit._is_cn_ip("9.9.9.9") is True
    assert audit._is_cn_ip("1.1.1.1") is False


def test_resolve_direct_parses_doh(fake_subprocess):
    doh_resp = json.dumps({"Answer": [
        {"type": 5, "data": "x.example.com."},
        {"type": 1, "data": "203.0.113.7"},
    ]})
    fake_subprocess.set_default(stdout=doh_resp, returncode=0)
    assert audit._resolve_direct("example.com") == "203.0.113.7"


def test_resolve_direct_no_a_record(fake_subprocess):
    fake_subprocess.set_default(stdout='{"Answer":[]}', returncode=0)
    assert audit._resolve_direct("example.com") == ""


def test_resolve_direct_bad_json(fake_subprocess):
    fake_subprocess.set_default(stdout="not json", returncode=0)
    assert audit._resolve_direct("example.com") == ""


# ────────────────────────────────────────────────────────────────────────────
# _load_rules: 解析双 config 规则
# ────────────────────────────────────────────────────────────────────────────

def test_load_rules_from_singbox(tmp_path: Path, monkeypatch):
    sb_cfg = tmp_path / "sb.json"
    sb_cfg.write_text(json.dumps({
        "route": {"rules": [
            {"outbound": "direct", "domain_suffix": [".cn", "baidu.com"]},
            {"outbound": "proxy", "domain_suffix": ["github.com"]},
            {"outbound": "block", "domain_suffix": ["ad.com"]},
        ]}
    }))
    monkeypatch.setattr(audit, "SB_CONFIG", str(sb_cfg))
    monkeypatch.setattr(audit, "MH_CONFIG", str(tmp_path / "missing"))

    direct, proxy = audit._load_rules()
    assert direct == {"cn", "baidu.com"}
    assert proxy == {"github.com"}


def test_load_rules_from_mihomo(tmp_path: Path, monkeypatch):
    mh_cfg = tmp_path / "mh.yaml"
    mh_cfg.write_text(
        "rules:\n"
        "  - DOMAIN-SUFFIX,cn,DIRECT\n"
        "  - DOMAIN-SUFFIX,github.com,proxy\n"
        "  - DOMAIN-SUFFIX,claude.ai,claude\n"
        "  - RULE-SET,custom-direct,DIRECT\n"
    )
    monkeypatch.setattr(audit, "SB_CONFIG", str(tmp_path / "missing"))
    monkeypatch.setattr(audit, "MH_CONFIG", str(mh_cfg))

    direct, proxy = audit._load_rules()
    assert direct == {"cn"}
    assert proxy == {"github.com", "claude.ai"}


def test_load_rules_merges_two_engines(tmp_path: Path, monkeypatch):
    """sing-box 和 mihomo 规则应该合并。"""
    sb_cfg = tmp_path / "sb.json"
    sb_cfg.write_text(json.dumps({
        "route": {"rules": [
            {"outbound": "direct", "domain_suffix": ["baidu.com"]},
        ]}
    }))
    mh_cfg = tmp_path / "mh.yaml"
    mh_cfg.write_text("rules:\n  - DOMAIN-SUFFIX,qq.com,DIRECT\n")
    monkeypatch.setattr(audit, "SB_CONFIG", str(sb_cfg))
    monkeypatch.setattr(audit, "MH_CONFIG", str(mh_cfg))

    direct, _ = audit._load_rules()
    assert direct == {"baidu.com", "qq.com"}


def test_load_rules_robust_to_missing_configs(tmp_path: Path, monkeypatch):
    """两个 config 都不存在 → 空集合，不抛错。"""
    monkeypatch.setattr(audit, "SB_CONFIG", str(tmp_path / "x"))
    monkeypatch.setattr(audit, "MH_CONFIG", str(tmp_path / "y"))
    direct, proxy = audit._load_rules()
    assert direct == set()
    assert proxy == set()


# ────────────────────────────────────────────────────────────────────────────
# _apply_to_configs: 写入双 config
# ────────────────────────────────────────────────────────────────────────────

def test_apply_to_configs_singbox_appends(tmp_path: Path, monkeypatch):
    sb_cfg = tmp_path / "sb.json"
    sb_cfg.write_text(json.dumps({
        "route": {"rules": [
            {"outbound": "direct", "domain_suffix": [".cn"]},
        ]}
    }))
    monkeypatch.setattr(audit, "SB_CONFIG", str(sb_cfg))
    monkeypatch.setattr(audit, "MH_CONFIG", str(tmp_path / "missing"))

    msg = audit._apply_to_configs(["newsite.cn", "another.cn"])
    data = json.loads(sb_cfg.read_text())
    suffixes = data["route"]["rules"][0]["domain_suffix"]
    assert "newsite.cn" in suffixes
    assert "another.cn" in suffixes
    assert any("sing-box" in m for m in msg)


def test_apply_to_configs_skips_existing(tmp_path: Path, monkeypatch):
    """已经在 list 里的不重复添加。"""
    sb_cfg = tmp_path / "sb.json"
    sb_cfg.write_text(json.dumps({
        "route": {"rules": [
            {"outbound": "direct", "domain_suffix": ["existing.cn"]},
        ]}
    }))
    monkeypatch.setattr(audit, "SB_CONFIG", str(sb_cfg))
    monkeypatch.setattr(audit, "MH_CONFIG", str(tmp_path / "missing"))

    audit._apply_to_configs(["existing.cn"])
    data = json.loads(sb_cfg.read_text())
    assert data["route"]["rules"][0]["domain_suffix"].count("existing.cn") == 1


def test_apply_to_configs_mihomo_inserts(tmp_path: Path, monkeypatch):
    mh_cfg = tmp_path / "mh.yaml"
    mh_cfg.write_text(
        "rules:\n"
        "  - DOMAIN-SUFFIX,com,proxy\n"
        "  # .cn 后缀\n"
        "  - DOMAIN-SUFFIX,cn,DIRECT\n"
    )
    monkeypatch.setattr(audit, "SB_CONFIG", str(tmp_path / "missing"))
    monkeypatch.setattr(audit, "MH_CONFIG", str(mh_cfg))

    msg = audit._apply_to_configs(["new.cn"])
    content = mh_cfg.read_text()
    assert "DOMAIN-SUFFIX,new.cn,DIRECT" in content
    assert any("mihomo" in m for m in msg)


def test_apply_to_configs_handles_failure(tmp_path: Path, monkeypatch):
    """SB_CONFIG 路径指向目录而非文件 → 失败但不抛异常。"""
    monkeypatch.setattr(audit, "SB_CONFIG", str(tmp_path))  # 是目录，open() 失败
    monkeypatch.setattr(audit, "MH_CONFIG", str(tmp_path / "missing"))
    msg = audit._apply_to_configs(["x.cn"])
    assert any("sing-box" in m and "失败" in m for m in msg)
