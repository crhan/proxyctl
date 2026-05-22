"""测试 COMMANDS_META.side_effects 必须是枚举列表 + 可选 conditional_side_effects。"""

from __future__ import annotations

import pytest

from proxyctl import explain


SE_ENUM = set(explain.SIDE_EFFECT_ENUM)


def test_side_effect_enum_values():
    """枚举值集合稳定（schema 契约）。"""
    assert SE_ENUM == {"process", "system", "config-write",
                       "cache", "network-io"}


def test_all_commands_side_effects_is_list():
    """COMMANDS_META 的每条 side_effects 必须是 list[str]（0.3.0 枚举形态）。"""
    for c in explain.COMMANDS_META:
        se = c.get("side_effects")
        assert isinstance(se, list), \
            f"{c['name']}.side_effects = {se!r}，应为 list"


def test_all_side_effects_within_enum():
    """每条 side_effects 元素必须 ⊆ SIDE_EFFECT_ENUM。"""
    for c in explain.COMMANDS_META:
        se = c.get("side_effects", [])
        for v in se:
            assert v in SE_ENUM, \
                f"{c['name']}.side_effects 含非法值: {v!r}"


def test_no_legacy_string_side_effects():
    """不允许保留 v0.2 的字符串形态（如 'process+system'）。"""
    for c in explain.COMMANDS_META:
        se = c.get("side_effects")
        assert not isinstance(se, str), \
            f"{c['name']}.side_effects 仍是 str: {se!r}（0.3.0 必须 list）"


def test_conditional_side_effects_structure():
    """conditional_side_effects 必须是 dict[str, list[enum]]。"""
    for c in explain.COMMANDS_META:
        cse = c.get("conditional_side_effects")
        if cse is None:
            continue
        assert isinstance(cse, dict), \
            f"{c['name']}.conditional_side_effects 应为 dict"
        for trigger, effects in cse.items():
            assert isinstance(trigger, str)
            assert isinstance(effects, list)
            for v in effects:
                assert v in SE_ENUM, \
                    f"{c['name']}.conditional[{trigger}] 含非法值: {v!r}"


def test_known_command_side_effects():
    """关键命令的 side_effects 字段断言（防退化）。"""
    by_name = {c["name"]: c for c in explain.COMMANDS_META}
    # 写命令
    assert set(by_name["start"]["side_effects"]) == {"process", "system"}
    assert set(by_name["restart-clean"]["side_effects"]) == {
        "process", "system", "cache"}
    assert set(by_name["mode"]["side_effects"]) == {"config-write"}
    assert set(by_name["engine"]["side_effects"]) == {
        "config-write", "process"}
    assert set(by_name["fix"]["side_effects"]) == {"system", "cache"}
    # 只读命令
    for name in ("status", "doctor", "connections", "traffic", "explain",
                 "agent-guide", "commands", "help", "env", "log", "plugins"):
        assert by_name[name]["side_effects"] == [], \
            f"{name} 应无 side_effects"
    # 网络只读
    assert by_name["check"]["side_effects"] == ["network-io"]
    assert by_name["trace"]["side_effects"] == ["network-io"]
    assert by_name["bench"]["side_effects"] == ["network-io"]
    # 条件性副作用
    assert by_name["audit"]["conditional_side_effects"] == {
        "apply": ["config-write"]}
    assert by_name["config"]["conditional_side_effects"] == {
        "set": ["config-write"]}
    assert by_name["traffic"]["conditional_side_effects"] == {
        "sample": ["cache"], "watch": ["cache"]}
    assert by_name["audit"]["side_effects"] == []
    assert by_name["config"]["side_effects"] == []
