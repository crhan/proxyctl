"""测试 proxyctl.suggest — Suggestion 引擎框架（v0.5.0+）。

每条规则的语义测试在对应数据模块（test_subscription.py 等）已覆盖。
本文件聚焦框架行为：fingerprint / first_seen / 排序 / state 持久化 / 缺失输入兜底。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from proxyctl import suggest


def _ok_sub(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "fetch_ok": True,
        "url_host": "config.example.com",
        "expire_at": "2026-12-31T23:59:59+08:00",
        "expire_days_left": 200,
        "traffic_used_pct": 0.05,
    }
    base.update(overrides)
    return base


@pytest.fixture
def _state(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    monkeypatch.setenv("PROXYCTL_SUGGEST_STATE_PATH", str(p))
    return p


# ────────────────────────────────────────────────────────────────────────────
# 缺失输入兜底
# ────────────────────────────────────────────────────────────────────────────

def test_build_no_sub_silent_by_default(_state):
    """sub=None + sub_explicit_missing=False → 不输出 subscription.missing。"""
    assert suggest.build_suggestions(sub=None) == []


def test_build_no_sub_explicit_missing_emits_missing(_state):
    out = suggest.build_suggestions(sub=None, sub_explicit_missing=True)
    assert any(s["id"] == "subscription.missing" for s in out)


def test_build_healthy_sub_returns_empty(_state):
    assert suggest.build_suggestions(sub=_ok_sub()) == []


# ────────────────────────────────────────────────────────────────────────────
# fingerprint 稳定性
# ────────────────────────────────────────────────────────────────────────────

def test_fingerprint_stable_across_evidence_change(_state):
    """同 id、不同 evidence（如 traffic 73% → 88%）应得到同 fingerprint。"""
    fp1 = suggest._compute_fingerprint("subscription.traffic_high")
    fp2 = suggest._compute_fingerprint("subscription.traffic_high")
    assert fp1 == fp2
    assert len(fp1) == 12


def test_fingerprint_differs_between_ids(_state):
    fp_a = suggest._compute_fingerprint("subscription.expired")
    fp_b = suggest._compute_fingerprint("subscription.traffic_high")
    assert fp_a != fp_b


# ────────────────────────────────────────────────────────────────────────────
# first_seen 持久化
# ────────────────────────────────────────────────────────────────────────────

def test_first_seen_set_on_first_appearance(_state):
    out = suggest.build_suggestions(sub=_ok_sub(expire_days_left=5))
    s = [x for x in out if x["id"] == "subscription.expiring_soon"][0]
    assert "first_seen" in s
    assert s["first_seen"].endswith("Z")


def test_first_seen_stable_across_calls(_state):
    """第二次 build 同一问题，first_seen 不变（持续问题不重置时间戳）。"""
    out1 = suggest.build_suggestions(sub=_ok_sub(expire_days_left=5))
    first_ts = [x for x in out1 if x["id"] == "subscription.expiring_soon"][0]["first_seen"]

    out2 = suggest.build_suggestions(sub=_ok_sub(expire_days_left=3))  # 同 id, evidence 变
    second_ts = [x for x in out2 if x["id"] == "subscription.expiring_soon"][0]["first_seen"]
    assert first_ts == second_ts


def test_state_file_persisted_atomically(_state):
    suggest.build_suggestions(sub=_ok_sub(expire_days_left=-1))
    assert _state.is_file()
    data = json.loads(_state.read_text(encoding="utf-8"))
    fp = suggest._compute_fingerprint("subscription.expired")
    assert fp in data


def test_state_persist_off(_state):
    suggest.build_suggestions(sub=_ok_sub(expire_days_left=-1),
                              persist_state=False)
    assert not _state.is_file()


def test_corrupt_state_file_silently_recovers(_state):
    _state.write_text("not json{{", encoding="utf-8")
    out = suggest.build_suggestions(sub=_ok_sub(expire_days_left=5))
    # 没崩，仍输出 suggestion，first_seen 用 now 兜底
    assert any(s["id"] == "subscription.expiring_soon" for s in out)


# ────────────────────────────────────────────────────────────────────────────
# 排序契约：severity desc, id asc
# ────────────────────────────────────────────────────────────────────────────

def test_sort_warn_before_advisory_before_info(_state):
    past = (datetime.now(timezone.utc) - timedelta(hours=30)).astimezone()
    out = suggest.build_suggestions(sub=_ok_sub(
        expire_days_left=-3,           # warn
        traffic_used_pct=75.0,         # advisory
        updated_at=past.isoformat(),   # info
    ))
    severities = [s["severity"] for s in out]
    # warn 在前、info 在后
    assert severities[0] == "warn"
    assert severities[-1] == "info"


def test_sort_same_severity_by_id_asc(_state):
    """同 severity 时按 id 字母序。expired + traffic_warn 都是 warn。"""
    out = suggest.build_suggestions(sub=_ok_sub(
        expire_days_left=-3, traffic_used_pct=95.0))
    warn_ids = [s["id"] for s in out if s["severity"] == "warn"]
    assert warn_ids == sorted(warn_ids)


# ────────────────────────────────────────────────────────────────────────────
# schema 完整性
# ────────────────────────────────────────────────────────────────────────────

REQUIRED_FIELDS = {"id", "severity", "actor", "title", "evidence",
                   "inspect_command", "fix_command", "auto_fixable",
                   "doc", "fingerprint", "first_seen", "since"}


def test_every_suggestion_has_full_schema(_state):
    out = suggest.build_suggestions(sub=_ok_sub(
        expire_days_left=-1, traffic_used_pct=95.0))
    for s in out:
        missing = REQUIRED_FIELDS - set(s.keys())
        assert not missing, f"{s['id']} 缺字段: {missing}"
        assert s["severity"] in suggest.SEVERITY_ENUM
        assert s["actor"] in suggest.ACTOR_ENUM
        assert s["doc"].startswith("suggestion:")
        assert s["doc"].endswith(s["id"])


def test_extra_raw_injected_and_decorated(_state):
    """_extra_raw 测试接口：未来 commit 2/3 加规则前的接入点。"""
    raw = [{
        "id": "test.example",
        "severity": "warn",
        "actor": "agent",
        "title": "test",
        "evidence": {},
        "doc": "suggestion:test.example",
        "since": "0.5.0",
    }]
    out = suggest.build_suggestions(sub=None, _extra_raw=raw)
    assert len(out) == 1
    s = out[0]
    assert s["fingerprint"] == suggest._compute_fingerprint("test.example")
    assert s["auto_fixable"] is False  # 默认补齐


# ────────────────────────────────────────────────────────────────────────────
# 边界：id 缺失 / 多次调用幂等
# ────────────────────────────────────────────────────────────────────────────

def test_raw_without_id_silently_skipped(_state):
    out = suggest.build_suggestions(sub=None, _extra_raw=[{"severity": "warn"}])
    assert out == []


def test_idempotent_calls_same_output(_state):
    sub = _ok_sub(expire_days_left=5)
    out1 = suggest.build_suggestions(sub=sub)
    out2 = suggest.build_suggestions(sub=sub)
    # first_seen 相同 → 整体输出相同
    assert out1 == out2
