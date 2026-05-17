"""proxyctl completion — shell 补全脚本生成。

支持 bash / zsh / fish。脚本从 COMMANDS_META + TOPICS 动态派生。

用法：
    eval "$(proxyctl completion zsh)"   # 立即生效
    proxyctl completion bash > ~/.proxyctl.bash && echo 'source ~/.proxyctl.bash' >> ~/.bashrc
"""

from __future__ import annotations

import sys


def _command_names() -> list:
    from proxyctl.explain import COMMANDS_META
    return [c["name"] for c in COMMANDS_META]


def _topic_names() -> list:
    from proxyctl.explain import TOPICS
    return sorted(TOPICS.keys())


def _gen_bash() -> str:
    cmds = " ".join(_command_names())
    topics = " ".join(_topic_names())
    return f"""# proxyctl bash completion
_proxyctl_complete() {{
    local cur prev cmds topics
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    cmds="{cmds}"
    topics="{topics}"

    # 第二个 token = 子命令
    if [ ${{COMP_CWORD}} -eq 1 ]; then
        COMPREPLY=( $(compgen -W "${{cmds}} --help --version --json --no-color --quiet" -- "${{cur}}") )
        return 0
    fi

    # explain <topic>
    if [ "${{COMP_WORDS[1]}}" = "explain" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "${{topics}}" -- "${{cur}}") )
        return 0
    fi

    # mode <tun|proxy>
    if [ "${{COMP_WORDS[1]}}" = "mode" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "tun proxy" -- "${{cur}}") )
        return 0
    fi

    # engine <mihomo|singbox>
    if [ "${{COMP_WORDS[1]}}" = "engine" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "mihomo singbox" -- "${{cur}}") )
        return 0
    fi

    # config path|get|set
    if [ "${{COMP_WORDS[1]}}" = "config" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "path get set" -- "${{cur}}") )
        return 0
    fi

    # audit apply 子参数
    if [ "${{COMP_WORDS[1]}}" = "audit" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "apply 1 3 7 30" -- "${{cur}}") )
        return 0
    fi

    # completion shell
    if [ "${{COMP_WORDS[1]}}" = "completion" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "bash zsh fish" -- "${{cur}}") )
        return 0
    fi

    # 默认 flag 补全
    COMPREPLY=( $(compgen -W "--help --json --no-color --quiet" -- "${{cur}}") )
}}
complete -F _proxyctl_complete proxyctl
"""


def _gen_zsh() -> str:
    cmds = "\n".join(f"  '{name}:{summary}'" for name, summary in _zsh_pairs())
    topics = " ".join(_topic_names())
    return f"""#compdef proxyctl
# proxyctl zsh completion

_proxyctl_topics() {{
  local -a topics
  topics=({topics})
  _describe 'topic' topics
}}

_proxyctl() {{
  local context state state_descr line
  typeset -A opt_args

  local -a cmds
  cmds=(
{cmds}
  )

  _arguments -C \\
    '1: :->cmd' \\
    '*: :->args' \\
    '--help[显示帮助]' \\
    '--version[显示版本]' \\
    '--json[结构化 JSON 输出]' \\
    '--no-color[关闭 ANSI 颜色]' \\
    '--quiet[安静模式]'

  case $state in
    cmd) _describe 'command' cmds ;;
    args)
      case $words[2] in
        explain) _proxyctl_topics ;;
        mode)    _values 'mode' tun proxy ;;
        engine)  _values 'engine' mihomo singbox ;;
        config)  _values 'subcommand' path get set ;;
        audit)   _values 'days_or_apply' apply 1 3 7 30 ;;
        completion) _values 'shell' bash zsh fish ;;
      esac
      ;;
  esac
}}

compdef _proxyctl proxyctl
"""


def _zsh_pairs() -> list:
    from proxyctl.explain import COMMANDS_META
    out = []
    for c in COMMANDS_META:
        summary = c["summary"].replace("'", "'\\''")
        out.append((c["name"], summary))
    return out


def _gen_fish() -> str:
    from proxyctl.explain import COMMANDS_META
    lines = ["# proxyctl fish completion",
             "complete -c proxyctl -l help -d '显示帮助'",
             "complete -c proxyctl -l version -d '显示版本'",
             "complete -c proxyctl -l json -d '结构化 JSON 输出'",
             "complete -c proxyctl -l no-color -d '关闭 ANSI'",
             "complete -c proxyctl -l quiet -d '安静模式'",
             ""]
    # 全局子命令补全（不带子参数时）
    for c in COMMANDS_META:
        summary = c["summary"].replace("'", "")[:60]
        lines.append(
            f"complete -c proxyctl -n '__fish_use_subcommand' "
            f"-a '{c['name']}' -d '{summary}'"
        )
    # explain topic
    topics = " ".join(_topic_names())
    lines.append("")
    lines.append(
        f"complete -c proxyctl -n '__fish_seen_subcommand_from explain' "
        f"-a '{topics}'"
    )
    lines.append(
        "complete -c proxyctl -n '__fish_seen_subcommand_from mode' "
        "-a 'tun proxy'"
    )
    lines.append(
        "complete -c proxyctl -n '__fish_seen_subcommand_from engine' "
        "-a 'mihomo singbox'"
    )
    lines.append(
        "complete -c proxyctl -n '__fish_seen_subcommand_from config' "
        "-a 'path get set'"
    )
    lines.append(
        "complete -c proxyctl -n '__fish_seen_subcommand_from completion' "
        "-a 'bash zsh fish'"
    )
    return "\n".join(lines) + "\n"


SHELLS = {"bash": _gen_bash, "zsh": _gen_zsh, "fish": _gen_fish}


def cmd_completion(args: list) -> None:
    """proxyctl completion [bash|zsh|fish]"""
    from proxyctl import _io
    try:
        from proxyctl.explain import GLOBAL_FLAGS_REF as _gf
        as_json = bool(_gf().get("json"))
    except Exception:
        as_json = False

    if not args:
        _io.fail("缺少 shell 参数",
                 hint="proxyctl completion [bash|zsh|fish]",
                 doc="agent", code=_io.USAGE, cmd="completion",
                 as_json=as_json)
        return

    shell = args[0]
    if shell not in SHELLS:
        import difflib
        suggest = difflib.get_close_matches(shell, list(SHELLS), n=1, cutoff=0.4)
        hints = [f"支持: {', '.join(SHELLS)}"]
        if suggest:
            hints.insert(0, f"是否想要：{suggest[0]}？")
        _io.fail(f"未知 shell：{shell}",
                 hints=hints, doc="agent",
                 code=_io.USAGE, cmd="completion",
                 as_json=as_json)
        return

    script = SHELLS[shell]()
    if as_json:
        _io.emit_json(_io.envelope("completion",
                                    data={"shell": shell, "script": script}))
        return
    print(script)
