"""proxyctl completion — shell 补全脚本生成。

支持 bash / zsh / fish。脚本从 COMMANDS_META + TOPICS 动态派生，
0.3.x 起补全：

  - 全局 flag: --help / --version / --json / --no-color / --quiet
              / --dry-run / -n / --plain
  - explain <topic>          → topic 列表
  - mode <tun|proxy>          → 模式
  - engine <mihomo|singbox>   → 引擎
  - config <path|get|set>     → 子命令
  - audit <apply|N>           → 子命令 / 常用天数
  - commands [--schema]        → 0.3.x schema 子模式
  - agent-guide [--section <name> | --list-sections]   → 0.3.3 切片
  - help <cmd>                → 顶层命令（与 <cmd> --help 同源）
  - completion <bash|zsh|fish> → shell
  - 写命令位置补 --dry-run；audit/check 位置补 --plain

用法：
    eval "$(proxyctl completion zsh)"
    proxyctl completion bash > ~/.proxyctl.bash && echo 'source ~/.proxyctl.bash' >> ~/.bashrc
"""

from __future__ import annotations

import sys


# 写命令支持 --dry-run（与 COMMANDS_META.supports_dry_run 同步）
# 0.4.2 起，lifecycle 4 个 + recover 也补齐 --dry-run。
_DRY_RUN_CMDS = ("start", "stop", "restart", "restart-clean", "recover",
                 "mode", "engine", "fix", "audit", "config",
                 "daemon", "claude-proxy", "dns-lock", "dns-unlock")
# 0.3.x：audit / check 支持 --plain
_PLAIN_CMDS = ("audit", "check")
# 0.3.x：commands 支持 --schema 子模式
_SCHEMA_CMDS = ("commands",)
# 0.3.3：agent-guide 支持 --section / --list-sections
_AGENT_GUIDE_FLAGS = ("--section", "--list-sections")


def _command_names() -> list:
    from proxyctl.explain import COMMANDS_META
    return [c["name"] for c in COMMANDS_META]


def _topic_names() -> list:
    from proxyctl.explain import TOPICS
    return sorted(TOPICS.keys())


def _gen_bash() -> str:
    cmds = " ".join(_command_names())
    topics = " ".join(_topic_names())
    dry_run_cmds = " ".join(f'"{c}"' for c in _DRY_RUN_CMDS)
    plain_cmds = " ".join(f'"{c}"' for c in _PLAIN_CMDS)
    return f"""# proxyctl bash completion (0.3.x)
_proxyctl_complete() {{
    local cur prev cmds topics first
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    cmds="{cmds}"
    topics="{topics}"
    first="${{COMP_WORDS[1]}}"

    # 第二个 token = 子命令
    if [ ${{COMP_CWORD}} -eq 1 ]; then
        COMPREPLY=( $(compgen -W "${{cmds}} help --help --version --json --plain --dry-run -n --no-color --quiet" -- "${{cur}}") )
        return 0
    fi

    # help <cmd>（与 <cmd> --help 同源）
    if [ "$first" = "help" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "${{cmds}}" -- "${{cur}}") )
        return 0
    fi

    # explain <topic>
    if [ "$first" = "explain" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "${{topics}}" -- "${{cur}}") )
        return 0
    fi

    # mode <tun|proxy>
    if [ "$first" = "mode" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "tun proxy" -- "${{cur}}") )
        return 0
    fi

    # engine <mihomo|singbox>
    if [ "$first" = "engine" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "mihomo singbox" -- "${{cur}}") )
        return 0
    fi

    # config path|get|set
    if [ "$first" = "config" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "path get set" -- "${{cur}}") )
        return 0
    fi

    # audit <apply|days>
    if [ "$first" = "audit" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "apply 1 3 7 30" -- "${{cur}}") )
        return 0
    fi

    # commands --schema 子模式
    if [ "$first" = "commands" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "--schema --json" -- "${{cur}}") )
        return 0
    fi

    # agent-guide --section / --list-sections
    if [ "$first" = "agent-guide" ]; then
        if [ "$prev" = "--section" ]; then
            # 这里不知道实际 section list（运行时才有）；给个占位
            COMPREPLY=( $(compgen -W "introduction onboarding capabilities envelope locks footgun" -- "${{cur}}") )
            return 0
        fi
        COMPREPLY=( $(compgen -W "--section --list-sections --json" -- "${{cur}}") )
        return 0
    fi

    # completion shell
    if [ "$first" = "completion" ] && [ ${{COMP_CWORD}} -eq 2 ]; then
        COMPREPLY=( $(compgen -W "bash zsh fish" -- "${{cur}}") )
        return 0
    fi

    # 在写命令的任意位置补 --dry-run
    local dry_run_cmds=({dry_run_cmds})
    for c in "${{dry_run_cmds[@]}}"; do
        if [ "$first" = "$c" ]; then
            COMPREPLY=( $(compgen -W "--dry-run -n --json --no-color --quiet --help" -- "${{cur}}") )
            return 0
        fi
    done

    # audit / check 加 --plain
    local plain_cmds=({plain_cmds})
    for c in "${{plain_cmds[@]}}"; do
        if [ "$first" = "$c" ]; then
            COMPREPLY=( $(compgen -W "--plain --json --dry-run -n --help" -- "${{cur}}") )
            return 0
        fi
    done

    # log 特有 flag
    if [ "$first" = "log" ]; then
        COMPREPLY=( $(compgen -W "--tail --no-follow --json --help" -- "${{cur}}") )
        return 0
    fi

    # env 特有 flag
    if [ "$first" = "env" ]; then
        COMPREPLY=( $(compgen -W "--unset" -- "${{cur}}") )
        return 0
    fi

    # 默认 flag 补全
    COMPREPLY=( $(compgen -W "--help --json --plain --dry-run -n --no-color --quiet" -- "${{cur}}") )
}}
complete -F _proxyctl_complete proxyctl
"""


def _gen_zsh() -> str:
    cmds = "\n".join(f"  '{name}:{summary}'" for name, summary in _zsh_pairs())
    topics = " ".join(_topic_names())
    return f"""#compdef proxyctl
# proxyctl zsh completion (0.3.x)

_proxyctl_topics() {{
  local -a topics
  topics=({topics})
  _describe 'topic' topics
}}

_proxyctl_cmd_names() {{
  local -a names
  names=({' '.join(_command_names())})
  _describe 'command' names
}}

_proxyctl() {{
  local context state state_descr line
  typeset -A opt_args

  local -a cmds
  cmds=(
{cmds}
    'help:顶层帮助 / 单命令帮助'
  )

  _arguments -C \\
    '1: :->cmd' \\
    '*: :->args' \\
    '--help[显示帮助]' \\
    '-h[显示帮助]' \\
    '--version[显示版本（加 --json 暴露 supported_features）]' \\
    '-v[显示版本]' \\
    '--json[结构化 envelope v2 输出]' \\
    '--plain[纯 TSV 输出（audit/check）；与 --json 互斥]' \\
    '--dry-run[预演写命令；输出 data.plan]' \\
    '-n[预演写命令；输出 data.plan]' \\
    '--no-color[关闭 ANSI 颜色]' \\
    '--quiet[安静模式]' \\
    '-q[安静模式]'

  case $state in
    cmd) _describe 'command' cmds ;;
    args)
      case $words[2] in
        help)        _proxyctl_cmd_names ;;
        explain)     _proxyctl_topics ;;
        mode)        _values 'mode' tun proxy ;;
        engine)      _values 'engine' mihomo singbox ;;
        config)      _values 'subcommand' path get set ;;
        audit)       _values 'days_or_apply' apply 1 3 7 30 ;;
        commands)    _values 'flag' --schema --json ;;
        completion)  _values 'shell' bash zsh fish ;;
        agent-guide)
          if [[ "$words[$CURRENT-1]" == "--section" ]]; then
            # 运行时取真实 section 名（fallback 常见值）
            _values 'section' \\
              introduction onboarding capabilities exclusions \\
              concept-map paths exit-codes envelope decision-tree \\
              non-interactive locks footgun self-discovery repo
          else
            _values 'flag' --section --list-sections --json
          fi
          ;;
        log)         _values 'flag' --tail --no-follow --json ;;
        env)         _values 'flag' --unset ;;
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
    lines = ["# proxyctl fish completion (0.3.x)",
             "complete -c proxyctl -l help -d '显示帮助'",
             "complete -c proxyctl -l version -d '显示版本'",
             "complete -c proxyctl -l json -d 'envelope v2 输出'",
             "complete -c proxyctl -l plain -d 'TSV 输出（audit/check）'",
             "complete -c proxyctl -s n -l dry-run -d '预演写命令'",
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
    # help 也作为顶层子命令补
    lines.append(
        "complete -c proxyctl -n '__fish_use_subcommand' "
        "-a 'help' -d '顶层/单命令帮助'"
    )
    # 子参数 / 子 flag
    topics = " ".join(_topic_names())
    lines.append("")
    lines.append(
        f"complete -c proxyctl -n '__fish_seen_subcommand_from explain' "
        f"-a '{topics}'"
    )
    lines.append(
        "complete -c proxyctl -n '__fish_seen_subcommand_from help' "
        f"-a '{' '.join(_command_names())}'"
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
    # 0.3.x: 写命令补 --dry-run
    for c in _DRY_RUN_CMDS:
        lines.append(
            f"complete -c proxyctl -n '__fish_seen_subcommand_from {c}' "
            f"-l dry-run -d '预演（不真正执行）'"
        )
    # 0.3.x: audit/check 补 --plain
    for c in _PLAIN_CMDS:
        lines.append(
            f"complete -c proxyctl -n '__fish_seen_subcommand_from {c}' "
            f"-l plain -d 'TSV 输出'"
        )
    # 0.3.x: commands --schema
    lines.append(
        "complete -c proxyctl -n '__fish_seen_subcommand_from commands' "
        "-l schema -d '输出 commands JSON Schema'"
    )
    # 0.3.3: agent-guide --section / --list-sections
    lines.append(
        "complete -c proxyctl -n '__fish_seen_subcommand_from agent-guide' "
        "-l section -d '只输出指定 section'"
    )
    lines.append(
        "complete -c proxyctl -n '__fish_seen_subcommand_from agent-guide' "
        "-l list-sections -d '列出所有 section 名'"
    )
    # log / env 特有 flag
    lines.append(
        "complete -c proxyctl -n '__fish_seen_subcommand_from log' "
        "-l tail -d '只取最后 N 行'"
    )
    lines.append(
        "complete -c proxyctl -n '__fish_seen_subcommand_from log' "
        "-l no-follow -d '一次性读取后返回'"
    )
    lines.append(
        "complete -c proxyctl -n '__fish_seen_subcommand_from env' "
        "-l unset -d '清除代理环境变量'"
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
