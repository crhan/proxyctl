"""测试 agent-guide --section <name> / --list-sections（0.3.3 新增）。

设计原则：
- 不假设具体 section 名（除了 introduction），因为 markdown 标题会演进。
- 验证：切分函数稳定 / --list-sections 输出 / --section 模糊匹配 / 错误路径 did-you-mean。
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

from proxyctl import cli, _io, explain
from proxyctl.cli import MihomoBackend


@pytest.fixture
def backend():
    return MihomoBackend({"config_dir": "/tmp/test", "proxy_port": 7890})


@pytest.fixture
def config():
    return {"backend": "mihomo", "proxy_port": 7890,
            "api_base": "http://127.0.0.1:9090", "api_secret": ""}


# ── 切分函数 ──────────────────────────────────────────────────────────────
def test_split_sections_returns_non_empty(backend, config):
    md = explain._build_agent_guide(backend, config)
    sections = explain._split_agent_guide_sections(md)
    assert len(sections) >= 5, "agent-guide 至少应切出 5 个 section"
    # 第一段应该是 introduction（H2 前的文本）
    assert "introduction" in sections


def test_split_sections_each_section_contains_its_heading(backend, config):
    """每个 section 的内容应以对应 H2 标题开头（introduction 除外）。"""
    md = explain._build_agent_guide(backend, config)
    sections = explain._split_agent_guide_sections(md)
    for name, content in sections.items():
        if name == "introduction":
            continue
        # 至少包含一个 ## 标题
        first_line = content.split("\n", 1)[0]
        assert first_line.startswith("## "), \
            f"section {name!r} 首行不是 H2: {first_line!r}"


def test_normalize_section_name_handles_chinese_and_emoji():
    norm = explain._normalize_section_name
    # 含中文 + 数字 + 特殊字符 + 空格
    assert norm("Agent 第一次接入：6 步引导路径") == "agent-6"
    assert norm("envelope 字段含义表") == "envelope"
    assert norm("Envelope Fields") == "envelope-fields"
    # 全中文 / 全空 → fallback 'section'
    assert norm("能做什么（按副作用三分类）") == "section"
    assert norm("   ") == "section"


def test_match_section_substring():
    """子串匹配：agent 拼 'envelope' 想找 'envelope-fields' 也能命中。

    设计上 normalize 后再做子串匹配，因此 'EnvelopeFields'（无分隔）
    会被归一化为 'envelopefields'，与 'envelope-fields' 不构成子串关系
    （连字符不被认为是子串边界），这是有意的——agent 应使用 --list-sections
    拿到准确名再请求，模糊匹配只兜底子串。
    """
    names = ["introduction", "envelope-fields", "lock-files",
             "exit-codes", "footgun"]
    assert explain._match_section("envelope-fields", names) == "envelope-fields"
    assert explain._match_section("envelope", names) == "envelope-fields"
    # 没有分隔符的连写形式不匹配（由 --list-sections + did-you-mean 兜底）
    assert explain._match_section("envelopefields", names) is None


def test_match_section_returns_none_when_ambiguous():
    names = ["foo-bar", "foo-baz"]
    assert explain._match_section("foo", names) is None


def test_match_section_returns_none_when_missing():
    names = ["alpha", "beta"]
    assert explain._match_section("gamma", names) is None


# ── CLI: --list-sections ──────────────────────────────────────────────────
def test_list_sections_human(backend, config, capsys):
    explain.set_global_flags({"json": False})
    explain.cmd_agent_guide(["--list-sections"], backend, config)
    out = capsys.readouterr().out
    assert "可用 section" in out
    assert "introduction" in out


def test_list_sections_json(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_agent_guide(["--list-sections"], backend, config)
    obj = json.loads(capsys.readouterr().out)
    data = obj["data"]
    assert "available_sections" in data
    assert isinstance(data["available_sections"], list)
    assert data["section_count"] == len(data["available_sections"])
    assert "introduction" in data["available_sections"]


# ── CLI: --section <name> ────────────────────────────────────────────────
def test_section_introduction_outputs_only_intro(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_agent_guide(["--section", "introduction"], backend, config)
    obj = json.loads(capsys.readouterr().out)
    assert obj["data"]["section"] == "introduction"
    md = obj["data"]["markdown"]
    # introduction 在第一个 H2 之前，所以不应该含 ## 行
    h2_lines = [l for l in md.split("\n") if l.startswith("## ")]
    assert len(h2_lines) == 0
    # available_sections 仍然列出全部
    assert len(obj["data"]["available_sections"]) >= 5


def test_section_unknown_fails_with_did_you_mean(backend, config, capsys):
    """请求不存在的 section 应返回 USAGE(2) + did-you-mean。"""
    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit) as ei:
        explain.cmd_agent_guide(
            ["--section", "introducton"], backend, config)  # 故意拼错
    assert ei.value.code == _io.USAGE
    obj = json.loads(capsys.readouterr().out)
    assert obj["ok"] is False
    # hints 应含建议或可用列表
    hints_str = " ".join(obj["hints"])
    assert "introduction" in hints_str


def test_section_via_substring_match(backend, config, capsys):
    """子串匹配：用户拼 'envelope' 应能匹配到含 'envelope' 的唯一 section（如果存在）。"""
    explain.set_global_flags({"json": True})
    md = explain._build_agent_guide(backend, config)
    sections = list(explain._split_agent_guide_sections(md).keys())
    envelope_sections = [s for s in sections if "envelope" in s]
    if len(envelope_sections) != 1:
        pytest.skip("agent-guide 的 envelope 相关 section 数量已变，跳过子串测试")
    explain.cmd_agent_guide(["--section", "envelope"], backend, config)
    obj = json.loads(capsys.readouterr().out)
    assert obj["data"]["section"] == envelope_sections[0]


# ── 全文输出（兼容 0.3.2 行为）──────────────────────────────────────────
def test_no_args_still_outputs_full_markdown(backend, config, capsys):
    explain.set_global_flags({"json": True})
    explain.cmd_agent_guide([], backend, config)
    obj = json.loads(capsys.readouterr().out)
    # 0.3.3：full 模式 envelope.data 现在也含 available_sections（增量字段）
    assert "markdown" in obj["data"]
    assert "available_sections" in obj["data"]
    # markdown 长度应远大于单 section
    assert len(obj["data"]["markdown"]) > 1000


# ── 0.4.2: 逐 section 可用性 parametrize ──────────────────────────────────
def _list_sections_for_parametrize() -> list[str]:
    """模块加载期就把 list-sections 输出枚举出来，作为 parametrize ids。"""
    backend = MihomoBackend({"config_dir": "/tmp/test", "proxy_port": 7890})
    config = {"backend": "mihomo", "proxy_port": 7890,
              "api_base": "http://127.0.0.1:9090", "api_secret": ""}
    md = explain._build_agent_guide(backend, config)
    return list(explain._split_agent_guide_sections(md).keys())


_ALL_SECTIONS = _list_sections_for_parametrize()


@pytest.mark.parametrize("section_name", _ALL_SECTIONS)
def test_every_listed_section_returns_non_empty(section_name, backend,
                                                   config, capsys):
    """0.4.2 guard：--list-sections 输出的**每一个** slug，
    都必须能用 --section <slug> 取回非空 markdown 且 envelope.ok=True。"""
    explain.set_global_flags({"json": True})
    explain.cmd_agent_guide(["--section", section_name], backend, config)
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is True, \
        f"section {section_name!r} 应能取回；envelope={env!r}"
    assert env["data"]["section"] == section_name
    md = env["data"]["markdown"]
    assert isinstance(md, str) and md.strip(), \
        f"section {section_name!r} 返回 markdown 为空"
    # introduction 之外的 section 应至少含 1 个 H2 标题
    if section_name != "introduction":
        h2_lines = [l for l in md.split("\n") if l.startswith("## ")]
        assert h2_lines, f"section {section_name!r} 应含至少一个 H2 标题"


# ── supported_features 翻 true ────────────────────────────────────────────
def test_version_features_agent_guide_sections_true():
    _io.set_no_color(True)
    _io._JSON_MODE = False  # type: ignore[attr-defined]
    _io._REQUEST_ID = None  # type: ignore[attr-defined]
    sys.argv = ["proxyctl", "--version", "--json"]
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            cli.main()
        except SystemExit:
            pass
    feat = json.loads(out.getvalue())["data"]["supported_features"]
    assert feat["agent_guide_sections"] is True
