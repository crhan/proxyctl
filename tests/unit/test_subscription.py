"""测试 subscription.py — 订阅状态契约文件读取与渲染（v0.4.4+）。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from proxyctl import subscription


def _write(tmp_path: Path, data) -> Path:
    """写一份 subscription.json 并把 PROXYCTL_SUBSCRIPTION_PATH 指过去。"""
    p = tmp_path / "sub.json"
    p.write_text(json.dumps(data) if isinstance(data, (dict, list)) else data,
                 encoding="utf-8")
    return p


def _ok_sub(**overrides) -> dict:
    """标准的健康订阅快照（v0.4.4 schema v1）。"""
    base = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "fetch_ok": True,
        "fetch_http_status": 200,
        "fetch_channel": "proxy-7890",
        "fetch_error": None,
        "url_host": "config.n2ray.dev",
        "expire_at": "2026-12-31T23:59:59+08:00",
        "expire_days_left": 200,
        "traffic_upload_bytes": 100,
        "traffic_download_bytes": 1024 * 1024,
        "traffic_used_bytes": 1024 * 1024 + 100,
        "traffic_total_bytes": 1024 * 1024 * 1024 * 500,
        "traffic_used_pct": 0.05,
        "info_nodes": ["官网:next.n2ray.dev", "到期:2026-12-31"],
        "node_count": 21,
        "relay_node_count": 21,
        "http_relay_sub_ok": True,
    }
    base.update(overrides)
    return base


# ────────────────────────────────────────────────────────────────────────────
# load()
# ────────────────────────────────────────────────────────────────────────────

def test_load_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXYCTL_SUBSCRIPTION_PATH",
                        str(tmp_path / "no-such-file.json"))
    assert subscription.load() is None


def test_load_corrupt_json_returns_none(tmp_path, monkeypatch):
    p = _write(tmp_path, "not json{{")
    monkeypatch.setenv("PROXYCTL_SUBSCRIPTION_PATH", str(p))
    assert subscription.load() is None


def test_load_non_dict_returns_none(tmp_path, monkeypatch):
    p = _write(tmp_path, ["array", "not", "dict"])
    monkeypatch.setenv("PROXYCTL_SUBSCRIPTION_PATH", str(p))
    assert subscription.load() is None


def test_load_happy_path(tmp_path, monkeypatch):
    p = _write(tmp_path, _ok_sub())
    monkeypatch.setenv("PROXYCTL_SUBSCRIPTION_PATH", str(p))
    sub = subscription.load()
    assert sub is not None
    assert sub["expire_days_left"] == 200
    assert sub["url_host"] == "config.n2ray.dev"


# ────────────────────────────────────────────────────────────────────────────
# severity() / summarize_hints()
# ────────────────────────────────────────────────────────────────────────────

def test_severity_ok():
    assert subscription.severity(_ok_sub()) == "ok"


def test_severity_warn_when_expire_le_7d():
    assert subscription.severity(_ok_sub(expire_days_left=5)) == "warn"


def test_severity_warn_when_traffic_ge_80pct():
    assert subscription.severity(_ok_sub(traffic_used_pct=85.0)) == "warn"


def test_severity_critical_when_expired():
    assert subscription.severity(_ok_sub(expire_days_left=-2)) == "critical"


def test_severity_critical_when_traffic_full():
    assert subscription.severity(_ok_sub(traffic_used_pct=101.5)) == "critical"


def test_severity_critical_when_fetch_failed():
    assert subscription.severity(_ok_sub(fetch_ok=False)) == "critical"


def test_summarize_hints_ok_returns_empty():
    assert subscription.summarize_hints(_ok_sub()) == []


def test_summarize_hints_expired():
    hints = subscription.summarize_hints(_ok_sub(expire_days_left=-3))
    assert any("EXPIRED 3d ago" in h for h in hints)


def test_summarize_hints_expire_warning():
    hints = subscription.summarize_hints(_ok_sub(expire_days_left=5))
    assert any("expires in 5d" in h for h in hints)


def test_summarize_hints_traffic_warning():
    hints = subscription.summarize_hints(_ok_sub(traffic_used_pct=85.5))
    assert any("traffic at 85.5%" in h for h in hints)


def test_summarize_hints_fetch_failed_short_circuit():
    """fetch_ok=false 时只报 fetch_error，不再追加 expire/traffic（数据可能过期）。"""
    sub = _ok_sub(fetch_ok=False, fetch_error="HTTP 404",
                  expire_days_left=2, traffic_used_pct=99.0)
    hints = subscription.summarize_hints(sub)
    assert len(hints) == 1
    assert "HTTP 404" in hints[0]


# ────────────────────────────────────────────────────────────────────────────
# format_line() / fmt_bytes()
# ────────────────────────────────────────────────────────────────────────────

def test_fmt_bytes_gigabyte():
    assert subscription.fmt_bytes(536870912000).startswith("500.00G")


def test_fmt_bytes_zero_and_none():
    assert subscription.fmt_bytes(0) == "?"
    assert subscription.fmt_bytes(None) == "?"


def test_format_line_happy():
    line = subscription.format_line(_ok_sub())
    assert "expire 2026-12-31" in line
    assert "200d left" in line
    assert "config.n2ray.dev" in line
    assert "500.00G" in line


def test_format_line_expired():
    line = subscription.format_line(_ok_sub(expire_days_left=-5))
    assert "EXPIRED 5d ago" in line


def test_format_line_fetch_failed():
    line = subscription.format_line(
        _ok_sub(fetch_ok=False, fetch_http_status=404, fetch_error="HTTP 404"))
    assert "fetch failed" in line
    assert "404" in line


# ────────────────────────────────────────────────────────────────────────────
# updated_at_human()
# ────────────────────────────────────────────────────────────────────────────

def test_updated_at_human_recent():
    sub = _ok_sub(updated_at=datetime.now(timezone.utc).astimezone().isoformat())
    rel = subscription.updated_at_human(sub)
    assert rel is not None
    # 应是秒级或分钟级（"updated Ns ago" 或 "updated 0m ago"）
    assert "ago" in rel


def test_updated_at_human_hours():
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).astimezone()
    sub = _ok_sub(updated_at=past.isoformat())
    rel = subscription.updated_at_human(sub)
    assert rel == "updated 3h ago"


def test_updated_at_human_bad_input():
    assert subscription.updated_at_human({"updated_at": "not-a-date"}) is None
    assert subscription.updated_at_human({}) is None
