"""CI 强校验：每个 suggestion.id 都必须有对应 `explain doc suggestion:<id>` topic。

UX review 关键诉求：agent 看到 suggestion 时能一跳到位读详情，不必二次定位。
"""

from __future__ import annotations

import pytest

from proxyctl import explain


# 当前所有规则的 id（从对应模块收集）——若新增规则忘了写 explain topic 就会失败
EXPECTED_IDS = [
    # subscription（src/proxyctl/subscription.py::to_suggestions）
    "subscription.expired",
    "subscription.expiring_soon",
    "subscription.traffic_exhausted",
    "subscription.traffic_warn",
    "subscription.traffic_high",
    "subscription.last_fetch_error",
    "subscription.stale",
    "subscription.missing",
    # autostart（src/proxyctl/autostart.py::to_suggestions）
    "autostart.unit_missing",
    "autostart.binary_missing",
    "autostart.binary_mismatch",
    "autostart.version_mismatch",
    "autostart.config_dir_mismatch",
    "autostart.placeholder_unrendered",
    "autostart.disabled",
    "autostart.flapping",
    # controller / engine / data / proxy_group（src/proxyctl/suggest_rules.py）
    "controller.empty_secret",
    "controller.weak_secret",
    "controller.public_bind",
    "engine.outdated",
    "data.geo_stale",
    "proxy_group.mostly_dead",
]


REQUIRED_CARD_FIELDS = {"topic", "summary", "file", "edit", "verify",
                        "next_commands"}


@pytest.mark.parametrize("sid", EXPECTED_IDS)
def test_every_suggestion_has_explain_topic(sid):
    topic_name = f"suggestion:{sid}"
    assert topic_name in explain.TOPICS, (
        f"suggestion id '{sid}' 没有对应的 explain topic '{topic_name}'。"
        f"在 src/proxyctl/explain.py 的 _SUGGESTION_DOCS 字典里加一条。"
    )
    # 实际调用 topic 函数验证不 crash
    card = explain.TOPICS[topic_name](backend=None, config={})
    missing = REQUIRED_CARD_FIELDS - set(card.keys())
    assert not missing, f"{topic_name} 缺字段: {missing}"
    assert card["topic"] == topic_name
    assert card["summary"]
    assert card["edit"]


def test_no_orphan_suggestion_topics():
    """反向校验：所有 suggestion:* topic 都必须在 EXPECTED_IDS 中。"""
    orphans = [
        t for t in explain.TOPICS
        if t.startswith("suggestion:") and t.removeprefix("suggestion:") not in EXPECTED_IDS
    ]
    assert not orphans, (
        f"发现孤儿 explain topic（没有对应规则）: {orphans}。"
        "要么补回规则，要么从 _SUGGESTION_DOCS 删除。"
    )
