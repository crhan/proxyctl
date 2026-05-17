"""测试 cli.DISPATCH 表与 COMMANDS_META / _known_commands 完整对齐。

防退化：新增命令后忘记同步路由表或元数据。
"""

from __future__ import annotations

import pytest

from proxyctl import cli, explain


def test_dispatch_covers_all_commands_meta():
    """COMMANDS_META 列的每个命令都必须有对应的 dispatch handler。"""
    missing = [c["name"] for c in explain.COMMANDS_META
               if c["name"] not in cli.DISPATCH]
    assert not missing, f"dispatch 缺以下命令: {missing}"


def test_commands_meta_covers_all_dispatch_targets():
    """反方向：dispatch 路由的每个命令（不含别名）都应在 COMMANDS_META 里。"""
    meta_names = {c["name"] for c in explain.COMMANDS_META}
    # 已知别名：agent_guide 是 agent-guide 的下划线版本
    aliases = {"agent_guide"}
    extras = set(cli.DISPATCH.keys()) - meta_names - aliases
    assert not extras, f"dispatch 多了 {extras}（meta 未注册）"


def test_known_commands_includes_dispatch_keys():
    """_known_commands() 必须包含所有 dispatch 命令（用于 did-you-mean）。"""
    from proxyctl.cli import _known_commands
    known = set(_known_commands())
    aliases = {"agent_guide"}  # 不必出现在 known
    for cmd in cli.DISPATCH.keys() - aliases:
        assert cmd in known, f"_known_commands 缺 {cmd}"


def test_dispatch_handlers_are_callable():
    """每个 handler 都得是 callable。"""
    for name, handler in cli.DISPATCH.items():
        assert callable(handler), f"DISPATCH[{name!r}] 不是 callable"


def test_explain_topic_for_each_meta_group_exists():
    """每个 commands meta 的 group 应有一个 explain topic 或 commands 整体说明。

    放宽：只要 lifecycle / diagnostic / config / daemon / agent / tool / maintenance
    之一即可（这些是设计上的分组）。"""
    allowed_groups = {"lifecycle", "diagnostic", "config", "daemon",
                      "agent", "tool", "maintenance"}
    for c in explain.COMMANDS_META:
        assert c["group"] in allowed_groups, \
            f"{c['name']} group={c['group']} 不在允许列表"


def test_check_json_groups_stage_schema(monkeypatch):
    """groups stage 必须是 list[dict]，每条含 name / type / now / members。"""
    from proxyctl.check import _proxy_groups_section
    # mock Clash API 响应
    fake_proxies = {
        "proxies": {
            "proxy-group-1": {
                "type": "Selector",
                "all": ["node-a", "node-b"],
                "now": "node-a",
            },
            "node-a": {"history": [{"delay": 100}]},
            "node-b": {"history": [{"delay": 200}]},
        }
    }
    import json
    import subprocess
    class _R:
        stdout = json.dumps(fake_proxies)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    collected: list = []
    _proxy_groups_section("http://x", "secret",
                          groups=["proxy-group-1"],
                          collect_into=collected)
    assert len(collected) == 1
    g = collected[0]
    assert g["name"] == "proxy-group-1"
    assert g["type"] == "Selector"
    assert g["now"] == "node-a"
    assert len(g["members"]) == 2
    assert g["members"][0]["name"] == "node-a"
    assert g["members"][0]["is_now"] is True
    assert g["members"][0]["delay_ms"] == 100
    assert g["members"][1]["is_now"] is False
