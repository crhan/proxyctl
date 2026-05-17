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


# ── 0.3.3：新 flag 必须在补全脚本里 ────────────────────────────────────────
def test_bash_completion_includes_0_3_x_global_flags(capsys):
    completion.cmd_completion(["bash"])
    out = capsys.readouterr().out
    for flag in ("--dry-run", "--plain"):
        assert flag in out, f"bash 补全缺 {flag}"


def test_bash_completion_includes_help_subcommand(capsys):
    """0.3.0 新增的 proxyctl help <cmd> 顶层命令应被补全。"""
    completion.cmd_completion(["bash"])
    out = capsys.readouterr().out
    # 顶层命令列表里应含 help
    assert " help " in out or '"help"' in out or "help -" in out or "help " in out


def test_bash_completion_includes_commands_schema(capsys):
    completion.cmd_completion(["bash"])
    out = capsys.readouterr().out
    assert "--schema" in out


def test_bash_completion_includes_agent_guide_section(capsys):
    completion.cmd_completion(["bash"])
    out = capsys.readouterr().out
    assert "--section" in out
    assert "--list-sections" in out


def test_zsh_completion_includes_help_subcommand_dispatch(capsys):
    completion.cmd_completion(["zsh"])
    out = capsys.readouterr().out
    assert "help)" in out  # case $words[2] in help) ...
    assert "_proxyctl_cmd_names" in out


def test_zsh_completion_documents_new_global_flags(capsys):
    completion.cmd_completion(["zsh"])
    out = capsys.readouterr().out
    for flag in ("--plain", "--dry-run", "--no-color"):
        assert flag in out, f"zsh 补全缺 {flag}"


def test_zsh_completion_handles_commands_schema(capsys):
    completion.cmd_completion(["zsh"])
    out = capsys.readouterr().out
    # case $words[2] in commands) _values 'flag' --schema --json ;;
    assert "commands)" in out
    assert "--schema" in out


def test_zsh_completion_handles_agent_guide_section(capsys):
    completion.cmd_completion(["zsh"])
    out = capsys.readouterr().out
    assert "agent-guide)" in out
    assert "--section" in out
    assert "--list-sections" in out


def test_fish_completion_includes_dry_run_for_each_write_command(capsys):
    completion.cmd_completion(["fish"])
    out = capsys.readouterr().out
    # 至少 mode / engine / fix 上有 --dry-run
    for cmd in ("mode", "engine", "fix", "audit", "config", "daemon",
                "dns-lock", "dns-unlock"):
        line = (f"complete -c proxyctl -n '__fish_seen_subcommand_from "
                f"{cmd}' -l dry-run")
        assert line in out, f"fish 补全缺 {cmd} 的 --dry-run"


def test_fish_completion_includes_plain_for_audit_check(capsys):
    completion.cmd_completion(["fish"])
    out = capsys.readouterr().out
    for cmd in ("audit", "check"):
        line = (f"complete -c proxyctl -n '__fish_seen_subcommand_from "
                f"{cmd}' -l plain")
        assert line in out, f"fish 补全缺 {cmd} 的 --plain"


def test_fish_completion_includes_help_subcommand(capsys):
    completion.cmd_completion(["fish"])
    out = capsys.readouterr().out
    # help 作为顶层子命令
    assert "-a 'help'" in out


def test_fish_completion_includes_commands_schema(capsys):
    completion.cmd_completion(["fish"])
    out = capsys.readouterr().out
    assert ("complete -c proxyctl -n '__fish_seen_subcommand_from commands' "
            "-l schema") in out


def test_fish_completion_includes_agent_guide_flags(capsys):
    completion.cmd_completion(["fish"])
    out = capsys.readouterr().out
    assert ("complete -c proxyctl -n '__fish_seen_subcommand_from agent-guide' "
            "-l section") in out
    assert ("complete -c proxyctl -n '__fish_seen_subcommand_from agent-guide' "
            "-l list-sections") in out


def test_fish_completion_includes_log_tail_no_follow(capsys):
    completion.cmd_completion(["fish"])
    out = capsys.readouterr().out
    assert ("complete -c proxyctl -n '__fish_seen_subcommand_from log' "
            "-l tail") in out
    assert ("complete -c proxyctl -n '__fish_seen_subcommand_from log' "
            "-l no-follow") in out
