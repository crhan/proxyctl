"""测试 completion 子命令生成的 shell 脚本基本健全性。"""

from __future__ import annotations

import pytest

from proxyctl import _io, completion


def test_bash_completion_contains_command_names(capsys):
    completion.cmd_completion(["bash"])
    out = capsys.readouterr().out
    assert "_proxyctl_complete()" in out
    assert "complete -F _proxyctl_complete proxyctl" in out
    # 关键命令名要出现在脚本里
    for name in ("status", "agent-guide", "explain", "doctor", "commands", "config"):
        assert name in out


def test_zsh_completion_has_compdef(capsys):
    completion.cmd_completion(["zsh"])
    out = capsys.readouterr().out
    assert out.startswith("#compdef proxyctl")
    assert "_proxyctl_topics()" in out
    assert "_describe 'topic' topics" in out


def test_fish_completion_has_complete_lines(capsys):
    completion.cmd_completion(["fish"])
    out = capsys.readouterr().out
    assert "complete -c proxyctl -l help" in out
    assert "__fish_use_subcommand" in out


def test_completion_topics_match_explain_topics(capsys):
    """补全脚本里的 topic 列表必须和 explain.TOPICS 同步。"""
    from proxyctl import explain
    completion.cmd_completion(["bash"])
    out = capsys.readouterr().out
    for topic in explain.TOPICS:
        assert topic in out


def test_completion_unknown_shell_fails_with_usage():
    with pytest.raises(SystemExit) as exc:
        completion.cmd_completion(["ksh"])
    assert exc.value.code == _io.USAGE


def test_completion_missing_arg_fails_with_usage():
    with pytest.raises(SystemExit) as exc:
        completion.cmd_completion([])
    assert exc.value.code == _io.USAGE


def test_completion_json_envelope(capsys):
    from proxyctl.explain import set_global_flags
    set_global_flags({"json": True})
    completion.cmd_completion(["zsh"])
    import json as _json
    env = _json.loads(capsys.readouterr().out)
    assert env["cmd"] == "completion"
    assert env["data"]["shell"] == "zsh"
    assert "compdef proxyctl" in env["data"]["script"]
    set_global_flags({"json": False})
