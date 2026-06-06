"""proxyctl.explain — Agent 友好自描述：explain / agent-guide / commands / config / doctor

设计：
- `proxyctl explain` (无参) 输出"我要改 ... 去哪里？"三大问题速查表
- `proxyctl explain <topic>` 输出 topic 卡片：SUMMARY / FILE / EDIT / VERIFY / NEXT
- `proxyctl agent-guide` 输出给 LLM 喂上下文的 markdown 文档
- `proxyctl commands` 输出所有命令的元数据（人类表 / --json）
- `proxyctl config path|get <key>` 让 Agent 定位/查询 proxyctl 自身配置
- `proxyctl doctor` 极简健康打分（5 项布尔 + score），人类 5 行 / --json envelope

Topic 内容由当前 backend / config 动态计算（路径、端口），不硬编码用户路径。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from typing import Any, Callable

from proxyctl import _io
from proxyctl._io import (
    CONFIG_ERR, ENGINE_DOWN, NOT_FOUND, OK, USAGE,
    emit_json, envelope, fail, should_color,
)


# ── 颜色（lazy，跟随 _io.set_no_color）────────────────────────────────────
RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[0;33m"
CYAN   = "\033[0;36m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
NC     = "\033[0m"

from proxyctl._io import maybe_disable_module_colors as _pc_maybe_disable
_pc_maybe_disable(__name__)


# ── Topic Registry ────────────────────────────────────────────────────────

TopicCard = dict   # {topic, summary, file, edit, verify, next}

TOPICS: dict[str, Callable[[Any, dict], TopicCard]] = {}


def topic(name: str):
    def wrap(fn):
        TOPICS[name] = fn
        return fn
    return wrap


@topic("rules")
def _t_rules(backend, config) -> TopicCard:
    return {
        "topic": "rules",
        "summary": "路由规则（分流规则）— 决定哪些域名/IP 走代理、走直连、被拒绝。",
        "file": f"{backend.config_file}  [rules: 段]",
        "edit": (
            f"$EDITOR {backend.config_file}\n"
            "  # mihomo: 在 rules: 段顶部插入 DOMAIN-SUFFIX,example.com,DIRECT\n"
            "  # sing-box: 在 route.rules[] 中插入对应规则对象"
        ),
        "verify": "proxyctl fix && proxyctl trace <domain>",
        "next_commands": ["explain config", "trace <domain>", "audit"],
    }


@topic("nodes")
def _t_nodes(backend, config) -> TopicCard:
    return {
        "topic": "nodes",
        "summary": "代理节点（线路）— 出口节点和分组定义；订阅由 mihomo/sing-box 或用户脚本管理（详见 explain subscription）。",
        "file": f"{backend.config_file}  [proxies: / proxy-providers: / proxy-groups: 段]",
        "edit": (
            "  # 添加单个节点：在 proxies: 段加 entry，再加到 proxy-groups: 的某个组\n"
            "  # 订阅源：用 mihomo 自身的 proxy-providers: + url + path + interval；\n"
            "  #        或用户脚本拉订阅写 config + 写 ~/.config/proxyctl/subscription.json（v0.4.4+）\n"
            "  # proxyctl 不发起订阅拉取；但显示订阅状态（见 explain subscription）"
        ),
        "verify": "proxyctl bench <group>   # 测节点延迟",
        "next_commands": ["bench", "explain subscription", "explain engine"],
    }


@topic("config")
def _t_config(backend, config) -> TopicCard:
    return {
        "topic": "config",
        "summary": (
            "proxyctl 自身配置（不是 mihomo 配置）。控制后端选择、Clash API、端口、"
            "企业 DNS 等。**建议把整个 ~/.config/proxyctl/ 目录纳入 git 追踪**（详见 "
            "explain config-tracking），订阅源 URL / Clash secret 等 secret 文件用 "
            ".gitignore 排除，配置改动留得下回放历史。"
        ),
        "file": _io_proxyctl_config_path(),
        "edit": f"$EDITOR {_io_proxyctl_config_path()}",
        "verify": "proxyctl config get <key>",
        "next_commands": ["explain config-tracking", "explain ports",
                          "explain corp-dns", "explain extra-daemons"],
    }


@topic("config-tracking")
def _t_config_tracking(backend, config) -> TopicCard:
    cfg_dir = os.path.join(os.path.expanduser("~"), ".config", "proxyctl")
    return {
        "topic": "config-tracking",
        "summary": (
            "用 git 追踪 ~/.config/proxyctl/ 的变化。proxyctl 自己不做版本控制，"
            "但用户的 config.yaml / 插件 / 订阅契约文件经常被多个工具（用户、cron、"
            "proxyctl autostart sync、update-subscription.sh、agent）改动，没有"
            "版本历史时很难 diff 出'谁改了什么'。git 是最便宜的解法。"
        ),
        "file": f"{cfg_dir}/.git",
        "edit": (
            f"  cd {cfg_dir} && git init\n"
            "  # .gitignore 必须排除 secret + 高频抖动文件（agent 别忘）：\n"
            "  cat > .gitignore <<'EOF'\n"
            "  subscription-source.env       # 订阅 URL 含 UUID secret\n"
            "  subscription.json             # 流量/到期高频变化\n"
            "  .lock.*                       # proxyctl 运行时锁文件\n"
            "  .ipgeo-*                      # ipgeo 探针缓存\n"
            "  *.bak                         # config set 自动备份\n"
            "  EOF\n"
            "  git add . && git commit -m 'init: 本机 proxyctl 配置基线'\n"
            "  # 后续改 config 之前：git status 看上次留下了啥；改完 commit"
        ),
        "verify": f"cd {cfg_dir} && git log --oneline",
        "next_commands": ["explain config", "explain plugins",
                          "config get backend"],
    }


@topic("dns")
def _t_dns(backend, config) -> TopicCard:
    return {
        "topic": "dns",
        "summary": "DNS 行为：系统 DNS 必须指向 127.0.0.1 才能让 fakeip 生效。"
                   "proxyctl 用三层防线（networksetup / AnyConnect / scutil）抵抗覆盖，"
                   "并提供 dns-lock 看门狗。",
        "file": f"{backend.config_file}  [dns: 段]",
        "edit": "  # 修改 fake-ip-range / nameserver 等：编辑 mihomo dns: 段",
        "verify": "proxyctl status   # 看 'DNS 解析' 行；proxyctl trace example.com",
        "next_commands": ["explain troubleshooting", "explain corp-dns", "fix"],
    }


@topic("engine")
def _t_engine(backend, config) -> TopicCard:
    return {
        "topic": "engine",
        "summary": (
            f"代理引擎（后端）。当前：{backend.name}。"
            "支持 mihomo（首发，端到端验证）/ sing-box（预留，未端到端验证 — "
            "类 / 路径 / audit / trace 解析已实现，但完整启停闭环未跑过生产）；"
            "通过 proxyctl engine <name> 切换后端，"
            "通过 proxyctl mode <tun|proxy> 切换流量入站方式。"
        ),
        "file": f"{backend.config_file}",
        "edit": (
            "  proxyctl engine mihomo|singbox   # 切换后端实现\n"
            "  proxyctl mode tun                # TUN 模式（透明代理，需 sudo）\n"
            "  proxyctl mode proxy              # HTTP/SOCKS proxy 模式"
        ),
        "verify": "proxyctl status",
        "next_commands": ["status", "explain mode", "explain ports"],
    }


@topic("ports")
def _t_ports(backend, config) -> TopicCard:
    port = config.get("proxy_port", 7890)
    return {
        "topic": "ports",
        "summary": (
            f"代理端口。proxyctl 默认 HTTP/SOCKS mixed-port = {port}，"
            "Clash API external-controller 默认 9090。同机多实例需要改 proxy_port。"
        ),
        "file": _io_proxyctl_config_path() + "  [proxy_port: 字段]",
        "edit": "  # 同步改 mihomo config.yaml 的 mixed-port / port",
        "verify": "proxyctl config get proxy_port",
        "next_commands": ["explain config", "config get proxy_port"],
    }


@topic("extra-daemons")
def _t_extra_daemons(backend, config) -> TopicCard:
    declared = list((config.get("extra_daemons") or {}).keys())
    return {
        "topic": "extra-daemons",
        "summary": (
            "辅助 daemon（如 claude-proxy，为 AI 流量跑一个 secondary sing-box）。"
            f"已声明：{declared or '(无)'}。用 proxyctl daemon <name> <subcmd> 管理。"
        ),
        "file": _io_proxyctl_config_path() + "  [extra_daemons: 段]",
        "edit": (
            "  # 在 extra_daemons: 下加 name + label + plist_src + log_path + port\n"
            "  # 然后 proxyctl daemon <name> start"
        ),
        "verify": "proxyctl daemon <name> status",
        "next_commands": ["explain config"],
    }


@topic("env")
def _t_env(backend, config) -> TopicCard:
    port = config.get("proxy_port", 7890)
    return {
        "topic": "env",
        "summary": (
            "代理环境变量（HTTP_PROXY / HTTPS_PROXY / NO_PROXY 等）。"
            f"`proxyctl env` 输出 export 行，可 eval 进当前 shell（端口 {port}）。"
        ),
        "file": _io_proxyctl_config_path() + "  [no_proxy_extra: 字段]",
        "edit": (
            "  # no_proxy_extra: 追加内网域名 / IPv4 CIDR / 企业 host\n"
            "  # 裸 IPv6 CIDR 会被跳过，避免 Python/httpx 误解析 NO_PROXY"
        ),
        "verify": "eval \"$(proxyctl env)\" && env | grep -i proxy",
        "next_commands": ["env", "env --unset"],
    }


@topic("corp-dns")
def _t_corp_dns(backend, config) -> TopicCard:
    corp = config.get("corp_dns") or {}
    has = bool(corp.get("server") or corp.get("domain"))
    return {
        "topic": "corp-dns",
        "summary": (
            "企业内网 DNS / AnyConnect 集成。"
            + ("当前已启用。" if has else "当前未配置（适合非企业环境）。")
        ),
        "file": _io_proxyctl_config_path() + "  [corp_dns: 段]",
        "edit": "  # 填 server / domain / test_domain / ip_prefix / check_targets",
        "verify": "proxyctl status   # 看 '企业内网' 段（若有 plugin 注入）",
        "next_commands": ["explain plugins", "explain dns"],
    }


@topic("plugins")
def _t_plugins(backend, config) -> TopicCard:
    user_plugin_dir = os.path.join(os.path.expanduser("~"),
                                   ".config", "proxyctl", "plugins")
    return {
        "topic": "plugins",
        "summary": (
            "插件系统。8 种 hook：check_groups / check_targets / dns_hooks / "
            "route_hooks / status_sections / watchdog_layers / audit_*。"
            "内置：connectivity_basic / corp_network。"
        ),
        "file": user_plugin_dir + "/*.py",
        "edit": (
            "  # 写一个继承自 proxyctl.core.plugin.Plugin 的类，放到该目录\n"
            "  # 禁用某个插件：config.yaml 中 plugins_disabled: [name1, name2]"
        ),
        "verify": "proxyctl plugins",
        "next_commands": ["plugins"],
    }


@topic("troubleshooting")
def _t_trbl(backend, config) -> TopicCard:
    return {
        "topic": "troubleshooting",
        "summary": (
            "故障排查决策树。优先级：doctor → status → check → trace → fix → recover。"
        ),
        "file": "(no file)",
        "edit": (
            "  proxyctl doctor              # 5 项布尔健康分（最快）\n"
            "  proxyctl status              # 详细状态面板\n"
            "  proxyctl check               # 4 阶段健康检查\n"
            "  proxyctl trace <domain>      # 域名链路诊断\n"
            "  proxyctl fix                 # 修复 DNS / 代理\n"
            "  proxyctl recover             # 切网后软恢复（不重启进程）"
        ),
        "verify": "proxyctl doctor --json",
        "next_commands": ["doctor", "status", "check", "trace", "fix", "recover"],
    }


@topic("exit-codes")
def _t_exit_codes(backend, config) -> TopicCard:
    lines = []
    for code, msg in sorted(_io.EXIT_CODE_HELP.items()):
        lines.append(f"  {code}  {msg}")
    return {
        "topic": "exit-codes",
        "summary": "退出码语义表。0 成功，非 0 失败；新错误路径分语义码。",
        "file": "(no file)",
        "edit": "\n".join(lines),
        "verify": "proxyctl statuss   # 拼写错应返回 2 (USAGE) 并给 did-you-mean",
        "next_commands": ["agent-guide", "commands"],
    }


@topic("agent")
def _t_agent(backend, config) -> TopicCard:
    return {
        "topic": "agent",
        "summary": (
            "Agent 接入入口。从这里开始：proxyctl agent-guide。"
            "也可设置 PROXYCTL_AGENT=1 一键开启 --json + 关色 + 非交互。"
        ),
        "file": "(no file)",
        "edit": (
            "  proxyctl agent-guide                  # 一段 markdown，喂给 LLM\n"
            "  proxyctl commands --json              # 所有命令元数据（含 side_effects）\n"
            "  PROXYCTL_AGENT=1 proxyctl status      # 自动 JSON + 关色 + 非交互"
        ),
        "verify": "PROXYCTL_AGENT=1 proxyctl status",
        "next_commands": ["agent-guide", "commands", "explain exit-codes"],
    }


@topic("subscription")
def _t_subscription(backend, config) -> TopicCard:
    return {
        "topic": "subscription",
        "summary": (
            "订阅边界（双重立场）："
            "(1) proxyctl 不更新订阅 — 拉新节点由用户脚本或引擎自身的 proxy-providers 负责。"
            "(2) v0.4.4 起 proxyctl 显示订阅状态（到期日 / 已用流量 / 拉取健康度）— "
            "通过读取 ~/.config/proxyctl/subscription.json 契约文件，"
            "由用户脚本每次拉订阅后写入。proxyctl 自身不发起任何网络请求。"
        ),
        "file": (
            f"{backend.config_file}  [节点 / 订阅源仍由用户管]\n"
            f"~/.config/proxyctl/subscription.json  [订阅状态契约文件，proxyctl 读、用户脚本写]"
        ),
        "edit": (
            "  # === 拉订阅 / 加节点（proxyctl 不做）===\n"
            "  # 选项 A: 写用户脚本 cron 拉订阅，参考仓库 update-subscription.sh\n"
            "  # 选项 B: 用 mihomo / sing-box 内置 proxy-providers:\n"
            "  #   proxy-providers:\n"
            "  #     myprovider:\n"
            "  #       type: http\n"
            "  #       url: https://...\n"
            "  #       interval: 86400\n"
            "  # 选项 C: 手动 Clash API PUT 触发刷新:\n"
            "  #   curl -X PUT -H 'Authorization: Bearer <api_secret>' \\\n"
            "  #        http://127.0.0.1:9090/providers/proxies/myprovider\n"
            "  #\n"
            "  # === 让 proxyctl 显示订阅状态（v0.4.4+）===\n"
            "  # 用户脚本拉完订阅后写入契约文件：\n"
            "  #   ~/.config/proxyctl/subscription.json (schema v1)\n"
            "  # 关键字段 (全部可选，缺失 → None):\n"
            "  #   fetch_ok / fetch_http_status / fetch_error\n"
            "  #   expire_at / expire_days_left\n"
            "  #   traffic_used_bytes / traffic_total_bytes / traffic_used_pct\n"
            "  #   info_nodes / node_count\n"
            "  # 详细 schema 见 proxyctl.subscription 模块 docstring。\n"
            "  # 失败时也要写（fetch_ok=false + fetch_error），让 proxyctl 能区分\n"
            "  # 「过期」vs「网络挂」vs「订阅服务方挂」。"
        ),
        "verify": (
            "proxyctl status                        # 末尾 SUBSCRIPTION 段显示\n"
            "proxyctl status --json | jq .data.subscription   # agent 消费"
        ),
        "next_commands": [
            "status",
            "status --json | jq .data.subscription",
            "bench",
            "explain nodes",
        ],
    }


@topic("agent-protocol")
def _t_agent_protocol(backend, config) -> TopicCard:
    return {
        "topic": "agent-protocol",
        "summary": (
            "envelope v2 + 退出码 + 决策树。Agent 应该读 AGENTS.md "
            "（仓库视角）与 proxyctl agent-guide（运行时视角）。"
            "本卡片是 cheat sheet。"
        ),
        "file": "(no file — AGENTS.md / agent-guide / commands --schema)",
        "edit": (
            "  envelope v2 字段：schema_version / cmd / ok / data / error /\n"
            "                   code / hints[] / warnings[] / doc / meta{}\n"
            "  meta：           ts / elapsed_ms / proxyctl_version / request_id\n"
            "  退出码：         0 OK / 2 USAGE / 3 NOT_FOUND / 4 PERMISSION /\n"
            "                   5 ENGINE_DOWN / 6 CONFIG_ERR / 7 NETWORK_ERR /\n"
            "                   8 LOCKED / 9 TIMEOUT / 10 DEPENDENCY_MISSING\n"
            "  能力探测：       proxyctl --version --json | jq .data.supported_features\n"
            "  全量元数据：     proxyctl commands --json\n"
            "  元数据 schema：  proxyctl commands --schema"
        ),
        "verify": "proxyctl agent-guide  # 完整接入文档",
        "next_commands": ["agent-guide", "commands --json",
                          "commands --schema", "explain locks"],
    }


@topic("locks")
def _t_locks(backend, config) -> TopicCard:
    lock_dir = os.path.join(os.path.expanduser("~"), ".config", "proxyctl")
    return {
        "topic": "locks",
        "summary": (
            "写操作互斥锁。LOCKED(8) 退出码触发时，错误 hints 已列出锁路径。"
            "极端情况（挂死进程）可手动 rm。"
        ),
        "file": f"{lock_dir}/.lock.{{system,config,daemon,traffic}}",
        "edit": (
            "  # 三类锁（按写操作类型分）：\n"
            "  #   .lock.system   start/stop/restart/fix\n"
            "  #   .lock.config   mode/engine/audit apply/config set\n"
            "  #   .lock.daemon   daemon/dns-lock/dns-unlock\n"
            "  #   .lock.traffic  traffic sample/watch\n"
            "  #\n"
            "  # 排查：\n"
            "  #   lsof <lock_path>            # 看谁持有\n"
            "  #   ps -p <pid>                 # 验证是 proxyctl\n"
            "  #\n"
            "  # 手动释放（仅当 lsof 显示无持有者）：\n"
            "  #   rm <lock_path>"
        ),
        "verify": (
            "proxyctl doctor --json | jq .data.lock_held"
            "  # 当前持锁的锁名"
        ),
        "next_commands": ["doctor --json", "explain exit-codes"],
    }


@topic("flags")
def _t_flags(backend, config) -> TopicCard:
    return {
        "topic": "flags",
        "summary": (
            "全局 flag 速查：--json / --plain / --dry-run/-n / --no-color / --quiet。"
            "全部位置无关；--json 与 --plain 互斥；--dry-run 仅对写命令有效。"
        ),
        "file": "(no file)",
        "edit": (
            "  --json       envelope schema v2（含 meta.ts/elapsed_ms/request_id）\n"
            "  --plain      纯 TSV 输出（audit / check 等表格命令）\n"
            "  --dry-run/-n 预演写命令的 plan（list[PlanStep]），不真正执行\n"
            "  --no-color   关闭 ANSI（也读 NO_COLOR / PROXYCTL_NO_COLOR）\n"
            "  --quiet/-q   压制非关键 stderr\n"
            "  --help/-h    单命令或全局帮助\n"
            "  --version/-v 版本号（加 --json 输出 supported_features）"
        ),
        "verify": "proxyctl mode tun --dry-run --json | jq .data.plan",
        "next_commands": ["agent-protocol", "agent-guide", "commands"],
    }


# ── 工具：proxyctl 自身配置文件路径 ───────────────────────────────────────
def _io_proxyctl_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "proxyctl", "config.yaml")


# ── 主入口 1: explain ─────────────────────────────────────────────────────

def cmd_explain(args: list, backend, config) -> None:
    """proxyctl explain [<topic>] [--json]

    无参 → 三大问题速查表。带 topic → 卡片。--json 在外层解析后由 cli 注入。
    """
    as_json = GLOBAL_FLAGS_REF().get("json", False)

    if not args:
        if as_json:
            emit_json(envelope("explain", data={
                "topics": sorted(TOPICS.keys()),
                "quickref": _quickref_data(backend, config),
            }))
            return
        _print_quickref(backend, config)
        return

    name = args[0]
    if name not in TOPICS:
        suggestions = _suggest(name, list(TOPICS.keys()))
        hint = f"已知 topic: {', '.join(sorted(TOPICS.keys()))}"
        if suggestions:
            hint = f"是否想要: {', '.join(suggestions)} ？  {hint}"
        fail(f"未知 topic: {name}", hint=hint, code=USAGE,
             cmd="explain", as_json=as_json)

    card = TOPICS[name](backend, config)
    if as_json:
        emit_json(envelope("explain", data=card))
        return
    _print_card(card)


def _print_quickref(backend, config) -> None:
    port = config.get("proxy_port", 7890)
    pcfg = _io_proxyctl_config_path()
    mcfg = backend.config_file

    print(f"{BOLD}proxyctl explain{NC} — 我要改 ... 去哪里？")
    print(f"{DIM}（更多：proxyctl explain <topic>；Agent 入口：proxyctl agent-guide）{NC}")
    print("─" * 72)
    rows = [
        ("分流规则 (rules)",      f"{mcfg}  [rules: 段]",
                                  "proxyctl explain rules"),
        ("节点 / 订阅 (nodes)",   f"{mcfg}  [proxies/proxy-providers: 段]",
                                  "proxyctl explain nodes"),
        ("proxyctl 自身配置",     pcfg, "proxyctl config path"),
        ("DNS 行为",              "—", "proxyctl explain dns"),
        (f"端口 (HTTP/SOCKS={port})", "—", "proxyctl config get proxy_port"),
        ("故障排查",              "—", "proxyctl explain troubleshooting"),
        ("Agent 接入",            "—", "proxyctl agent-guide"),
    ]
    for label, file, cmd in rows:
        print(f"  {CYAN}{label}{NC}")
        if file != "—":
            print(f"      file: {file}")
        print(f"      → {cmd}")
    print()
    print(f"所有 topic: {', '.join(sorted(TOPICS.keys()))}")


def _print_card(card: TopicCard) -> None:
    print(f"{BOLD}TOPIC{NC}    {card['topic']}")
    print(f"{BOLD}SUMMARY{NC}  {card['summary']}")
    print(f"{BOLD}FILE{NC}     {card['file']}")
    print(f"{BOLD}EDIT{NC}")
    for line in card["edit"].splitlines():
        print(f"  {line}" if not line.startswith("  ") else line)
    print(f"{BOLD}VERIFY{NC}   {card['verify']}")
    nxt_list = card.get("next_commands") or card.get("next")
    if nxt_list:
        nxt = ", ".join(f"proxyctl {n}" for n in nxt_list)
        print(f"{BOLD}NEXT{NC}     {nxt}")


def _quickref_data(backend, config) -> dict:
    port = config.get("proxy_port", 7890)
    return {
        "rules":     {"file": backend.config_file, "section": "rules",
                      "explain": "proxyctl explain rules"},
        "nodes":     {"file": backend.config_file,
                      "section": "proxies / proxy-providers / proxy-groups",
                      "explain": "proxyctl explain nodes"},
        "proxyctl_config": {"file": _io_proxyctl_config_path(),
                            "explain": "proxyctl explain config"},
        "dns":       {"explain": "proxyctl explain dns"},
        "ports":     {"proxy_port": port,
                      "explain": "proxyctl explain ports"},
        "troubleshooting": {"explain": "proxyctl explain troubleshooting"},
        "agent":     {"entry": "proxyctl agent-guide"},
    }


def _suggest(name: str, candidates: list) -> list:
    import difflib
    return difflib.get_close_matches(name, candidates, n=3, cutoff=0.5)


# ── 主入口 2: agent-guide ─────────────────────────────────────────────────

def cmd_agent_guide(args: list, backend, config) -> None:
    """proxyctl agent-guide [--section <name>] [--list-sections] [--json]

    无参 → 输出完整 markdown（≤300 行）。
    `--list-sections` → 列出所有可用 section 名，agent 可挑一个取。
    `--section <name>` → 只输出该 section 的 markdown（H2 标题下 + 直到下一个 H2）。
    name 是大小写不敏感 + 空格容忍的模糊匹配（"envelope fields" / "envelope-fields"
    / "Envelope 字段含义表" 都接受）。
    """
    as_json = GLOBAL_FLAGS_REF().get("json", False)

    # 解析 args
    positional, flags = _io.extract_flags(
        args, known={"--section": "value", "--list-sections": "bool"})

    text = _build_agent_guide(backend, config)
    sections = _split_agent_guide_sections(text)
    section_names = list(sections.keys())  # 保持原顺序

    if flags.get("list_sections"):
        if as_json:
            emit_json(envelope("agent-guide", data={
                "available_sections": section_names,
                "section_count": len(section_names),
            }))
            return
        print(f"{BOLD}可用 section（{len(section_names)} 个）：{NC}")
        for s in section_names:
            print(f"  {CYAN}{s}{NC}")
        print(f"\n{DIM}用法：proxyctl agent-guide --section <name>{NC}")
        return

    requested = flags.get("section")
    if requested:
        match = _match_section(requested, section_names)
        if match is None:
            import difflib
            suggest = difflib.get_close_matches(
                requested, section_names, n=3, cutoff=0.3)
            hints = [f"可用 section: {', '.join(section_names)}"]
            if suggest:
                hints.insert(0, f"是否想要：{suggest[0]}？")
            _io.fail(f"未识别 section：{requested}",
                     hints=hints, doc="agent",
                     code=_io.USAGE, cmd="agent-guide")
        chunk = sections[match]
        if as_json:
            emit_json(envelope("agent-guide", data={
                "section": match,
                "markdown": chunk,
                "available_sections": section_names,
            }))
            return
        print(chunk)
        return

    if as_json:
        emit_json(envelope("agent-guide", data={
            "markdown": text,
            "available_sections": section_names,
        }))
        return
    print(text)


def _split_agent_guide_sections(md: str) -> dict[str, str]:
    """把 _build_agent_guide 输出的 markdown 按 H2 标题切分。

    返回 OrderedDict 形式（python 3.7+ 普通 dict 即有序）：
      {section_name: section_markdown_including_heading}

    section_name 是 H2 标题去 emoji / 中文标点后的归一化版（lowercase + 空格连字符化），
    便于 agent 用 ASCII 名稳定引用。
    """
    import re
    lines = md.split("\n")
    sections: dict[str, str] = {}
    cur_name: str | None = None
    cur_lines: list[str] = []
    # 文档开头到第一个 H2 之前的内容 → "introduction"
    intro_started = False
    for ln in lines:
        m = re.match(r"^##\s+(.+?)\s*$", ln)
        if m:
            # 收尾上一个 section
            if cur_name is not None:
                sections[cur_name] = "\n".join(cur_lines).rstrip() + "\n"
            elif cur_lines:
                # 文档开头部分作为 "introduction"
                sections["introduction"] = "\n".join(cur_lines).rstrip() + "\n"
            title = m.group(1).strip()
            cur_name = _normalize_section_name(title)
            cur_lines = [ln]
        else:
            cur_lines.append(ln)
    if cur_name is not None:
        sections[cur_name] = "\n".join(cur_lines).rstrip() + "\n"
    elif cur_lines and not sections:
        sections["introduction"] = "\n".join(cur_lines).rstrip() + "\n"
    return sections


def _normalize_section_name(title: str) -> str:
    """H2 标题 → ASCII 友好的 section name（agent 引用稳定）。

    约定：H2 推荐写成 ``## English Name — 中文标题``，取破折号（—/–/-）前
    的英文段做 ID。这样 agent 引用的是稳定 ASCII slug，人类标题仍然中文友好。

    映射规则：取破折号前内容（若有）→ 保留 ASCII 字母数字 → 空格合并 →
    转连字符 → lowercase。全空回退 'section'。
    """
    import re
    # 1. 取破折号前的部分（U+2014 — / U+2013 – / ASCII - 都接受）。
    #    必须两边都有空格才算分隔符，避免误切 "Non-Interactive" / "Self-Discovery"
    #    这类内部含 ASCII 连字符但无空格的英文短语。
    parts = re.split(r"\s+[—–\-]\s+", title, maxsplit=1)
    head = parts[0] if parts else title
    # 2. 留下 ASCII 字母数字、空格、连字符
    s = re.sub(r"[^A-Za-z0-9\s\-]", " ", head)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace(" ", "-").lower()
    # 3. 连续连字符合并 + 去边界
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "section"


def _match_section(requested: str, names: list[str]) -> str | None:
    """模糊匹配：requested 可以是归一化后的 name、原始 H2 标题、或子串。"""
    norm_req = _normalize_section_name(requested)
    if norm_req in names:
        return norm_req
    # 子串匹配（防止 agent 拼 'envelope' 想找 'envelope-fields'）
    candidates = [n for n in names if norm_req and norm_req in n]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _build_agent_guide(backend, config) -> str:
    port = config.get("proxy_port", 7890)
    mcfg = backend.config_file
    pcfg = _io_proxyctl_config_path()
    log = backend.log_file
    cache = backend.cache_file
    exit_lines = "\n".join(
        f"  {code}  {_io.EXIT_CODE_HELP[code]}"
        for code in sorted(_io.EXIT_CODE_HELP)
    )
    topics_list = ", ".join(sorted(TOPICS.keys()))
    lock_dir = os.path.join(os.path.expanduser("~"), ".config", "proxyctl")
    return f"""# proxyctl — Agent 接入指南（runtime / v0.3）

> 一句话：proxyctl 是 macOS + Linux 的代理 *生命周期管理* CLI。
> 它管「启停 / 状态 / 健康检查 / DNS 防护 / 配置切换」，**不装 mihomo、
> 不改规则、不更新订阅** —— 这些去引擎自己的配置文件里改。
> v0.4.4 起，proxyctl **显示**订阅状态（到期日 / 已用流量 / 拉取健康度），
> 通过用户脚本写入的契约文件读取 —— 详见下方 `Subscription Status` 段。
>
> 本文档由 `proxyctl agent-guide` 在运行时输出，含当前 backend/路径/端口。
> 仓库视角（开发/贡献协议）见仓库根 `AGENTS.md`。

## Onboarding — Agent 第一次接入：6 步引导路径

```
Step 1  proxyctl agent-guide              # 你正在看
Step 2  proxyctl --version --json         # 检查 schema_version=2 + supported_features
Step 3  proxyctl commands --json          # 全部命令元数据（机读）
Step 3' proxyctl commands --schema        # 上面 JSON 的 JSON Schema（验证用）
Step 4  PROXYCTL_AGENT=1 proxyctl ...     # 一键 JSON + 关色 + 非交互
Step 5  proxyctl doctor --json            # 当前健康基线
Step 6  proxyctl explain <topic>          # 深入概念（topic 见下）
```

调用任何写命令前先加 `--dry-run --json`（或 `-n --json`）看 `data.plan`，确认无误再去掉。

## Capabilities — 能做什么（按副作用三分类）

| 类别 | sudo | 命令 |
|---|---|---|
| **只读** (side_effects=[]) | 否 | `status doctor connections traffic snapshot/report audit env log plugins explain agent-guide commands config path|get help version` |
| **只读 + 网络 IO** (network-io) | 否 | `check trace bench recover`（curl/HTTP，不改本地状态） |
| **写 proxyctl 本地缓存** (cache) | 否 | `traffic sample/watch`（记录连接计数器增量，供 report 汇总） |
| **写 proxyctl 自身配置** | 否 | `config set <key> <value>`（原子写 + .bak + YAML 校验） |
| **写引擎配置** (config-write) | 是 | `mode tun|proxy` `audit apply` |
| **写系统 + 进程** | 是 | `start stop restart restart-clean fix engine daemon dns-lock dns-unlock` |

完整精确表见 `proxyctl commands --json` 的 `side_effects` 与 `conditional_side_effects` 字段。

## Exclusions — 不能做什么（去别处改）

- 添加 / 修改 / 删除分流规则 → 编辑 `{mcfg}` 的 `rules:` 段
- 添加节点 / 改订阅源 → 编辑 `{mcfg}` 的 `proxies:` / `proxy-providers:` 段
- **更新订阅 / 拉新节点** → mihomo `proxy-providers.interval` 自动 / Clash API 手动 /
  用户自己写脚本（参考仓库 `update-subscription.sh`）；proxyctl 自己不发起网络拉取。
  **但 proxyctl 会显示订阅状态**（v0.4.4+），见 `Subscription Status` 段。
- 安装 mihomo / sing-box → `brew install mihomo` 等
- 重启第三方应用 → 浏览器 / Slack / VSCode 需用户自己重启读 system proxy

## Subscription Status — 订阅状态展示（v0.4.4+）

proxyctl **不更新订阅**，但 **会显示订阅状态**——通过读取契约文件
`~/.config/proxyctl/subscription.json`（schema v1）。

### Agent 怎么用

```bash
proxyctl status --json | jq .data.subscription
# 关键字段（全部可选）：
#   fetch_ok / fetch_http_status / fetch_error      （拉取健康度）
#   expire_at / expire_days_left                     （套餐到期）
#   traffic_used_bytes / traffic_total_bytes / traffic_used_pct   （流量）
#   info_nodes / node_count / url_host

proxyctl status --json | jq .hints
# 风险摘要也会进 envelope.hints[]：
#   过期 ≤ 7 天     → "subscription expires in Nd ..."
#   过期已发生      → "subscription EXPIRED Nd ago ..."  (critical)
#   流量 ≥ 80%      → "subscription traffic at X% ..."
#   流量 ≥ 100%     → "subscription traffic exhausted ..." (critical)
#   fetch_ok=false  → "subscription fetch failed: <error>" (critical)
```

### 谁来写契约文件

**用户脚本**（不是 proxyctl）。proxyctl 自己不发起任何网络请求 / 不解析订阅 URL。
本仓库 `update-subscription.sh` 是参考实现：拉两个订阅 → 解析 Subscription-Userinfo
HTTP header → 写出 subscription.json。**任何符合 schema v1 的脚本都行**。

成功或失败都要写：fetch_ok=false 时也填出 fetch_error / fetch_http_status，
proxyctl 才能区分「过期 / 网络挂 / 订阅服务方挂」。

### 探测 capability

`proxyctl --version --json` → `data.supported_features.status_subscription = true`
（0.4.4+）。`false` 或缺失字段表示老版本，agent 应忽略 `data.subscription`。

详见 `proxyctl explain subscription`。

## Concept Map — "想改 X 去哪"

| 想改 | 文件 | 段 / 字段 |
|---|---|---|
| 分流规则 | `{mcfg}` | `rules:` |
| 节点 / 出口线路 | `{mcfg}` | `proxies:` / `proxy-providers:` / `proxy-groups:` |
| 引擎自身的 DNS / fakeip | `{mcfg}` | `dns:` |
| proxyctl 自己（API token / 端口 / 企业 DNS） | `{pcfg}` | 顶层 |
| 系统 DNS 行为 | (运行时由 proxyctl 管) | `proxyctl fix` / `dns-lock` |

更多：`proxyctl explain <topic>`，topic：
{topics_list}

## Paths — 关键路径（当前 backend = {backend.name}）

- proxyctl 配置: `{pcfg}`
- 引擎配置: `{mcfg}`
- 引擎缓存: `{cache}`
- 引擎日志: `{log}`
- 代理端口: HTTP/SOCKS = `{port}`，Clash API = `9090`
- 用户插件目录: `~/.config/proxyctl/plugins/*.py`
- 锁文件目录: `{lock_dir}/.lock.{{system|config|daemon|traffic}}`

## Config Tracking — 用户配置目录建议纳入 git

`~/.config/proxyctl/` 是用户的私人配置目录（不是 proxyctl 项目源代码）。
被多方写入：用户手改、`config set`、`proxyctl autostart sync`、订阅脚本、
cron、agent。**强烈建议把它纳入 git 追踪**，让你能回放"上次配置是什么样"。

```bash
cd {lock_dir} && git init
# .gitignore 必须排除 secret + 高频抖动文件：
cat > .gitignore <<'EOF'
subscription-source.env       # 订阅 URL（含 UUID secret，禁止入库）
subscription.json             # 流量/到期高频变化
.lock.*                       # proxyctl 运行时锁
.ipgeo-*                      # ipgeo 探针缓存
*.bak                         # config set 自动备份
EOF
git add . && git commit -m 'init: 本机 proxyctl 配置基线'
```

agent 在用户机器上首次跑 proxyctl 时，**应该提示用户是否要做这一步**
（首次见到 `{lock_dir}` 但无 `.git` 即可提示）。proxyctl 自己不动 git——
版本控制是用户/agent 的职责，跟拉订阅是同一类边界。

详见 `proxyctl explain config-tracking`。

## Exit Codes — 退出码

```
{exit_lines}
```

旧路径仍返回 1（GENERIC）。新错误路径使用分语义码。SIGINT → 130。

## Envelope — JSON envelope（schema v2）

```json
{{
  "schema_version": 2,
  "cmd":            "status",
  "ok":             true,
  "data":           {{ ... }},
  "error":          null,
  "code":           0,
  "hints":          [],
  "warnings":       [],
  "doc":            null,
  "meta": {{
    "ts":               "2026-05-17T08:00:00Z",
    "elapsed_ms":       12,
    "proxyctl_version": "0.3.0",
    "request_id":       "abc..."
  }}
}}
```

### envelope 字段含义表

| 字段 | 类型 | 何时填 | Agent 应如何处理 |
|---|---|---|---|
| `schema_version` | int | 总是 | `== 2` 才信任后续字段；不等就让用户升级 |
| `cmd` | str | 总是（discovery 时为空串） | 用于日志关联 |
| `ok` | bool | 总是 | 主判定 |
| `data` | obj/null | 通常 ok=true 时 | 命令载荷；schema 见 `commands --schema` |
| `error` | str/null | ok=false 时 | 人类可读错误（i18n by locale） |
| `code` | int | 总是 | 分语义退出码，见上表 |
| `hints` | list[str] | 失败/discovery 时 | **可执行的下一步命令**（不是描述） |
| `warnings` | list[str] | 任意 | 非致命警告 |
| `doc` | str/null | 失败时常填 | explain topic 名；`proxyctl explain <doc>` |
| `meta.ts` | ISO8601 str | 总是 | 用于日志/审计时间戳 |
| `meta.elapsed_ms` | int/null | 总是 | 端到端耗时（不含 fork） |
| `meta.proxyctl_version` | str | 总是 | 服务端版本（agent 兼容性判断） |
| `meta.request_id` | uuid hex str | 总是 | 一次调用内多条 envelope/NDJSON 共享 |

NDJSON 流式：`bench --json` 每节点一行 JSON + 末尾 envelope summary；
`log --json` 每行一个 `{{file, line}}` 对象（非 envelope）。

## Plan — `data.plan[].action` 类型枚举（dry-run 输出）

写命令的 `--dry-run` 在 `data.plan` 输出 step 列表。从 v0.4.0a1 起，所有
step.target 字符串都不再含 `<...>` 占位符，agent 可原样使用。

| action | target 是什么 | agent 可怎么用 |
|---|---|---|
| `subprocess` | 可执行 shell 命令字符串（`target.split()` 即得 argv） | 复读 / 加 sudo 后直接跑 |
| `system_op`  | 迭代型系统操作的描述（如 networksetup 遍历所有 service） | **不可**直接复读；理解副作用 |
| `fs_write` / `fs_copy` / `fs_write_atomic` | 绝对路径（fs_copy 形如 `src → dst`） | 文件写入意图 |
| `fs_remove` | 绝对路径 | 文件删除意图 |
| `edit_yaml` | `path [section:]` | 配置就地编辑意图 |
| `scan_log` | 日志文件绝对路径 | 日志扫描意图 |
| `http_put` | 完整 HTTP URL | Clash API 热重载等 |

`subprocess` step 的 `target` 是 dry-run 上界 —— cmd 实际执行可能跳过某些
conditional 步骤（如 daemon-start 的 cp 在 plist 已存在时跳过）。所有 step
也有 `requires_sudo` / `reversible` / `side_effects` / `summary` 等元信息。

CI 层 contract test（`tests/integration/test_plan_exec_contract.py`）保证
`_plan_<cmd>` 与 `cmd_<cmd>` 的 subprocess argv 永不漂移。

## Decision Tree — 故障决策树（给 Agent 自动化）

1. `proxyctl doctor --json`  ← 最快，5 项布尔 + score（+ engine/mode/lock_path 信息字段）
2. 如果 `engine_up=false` → `proxyctl start`
3. 如果 `port_listen=false`（引擎已启动但没监听）→ `proxyctl restart`
4. 如果 `dns_ok=false` → `proxyctl fix`
5. 如果 `system_proxy_ok=false` 且当前 mode=proxy → `proxyctl fix`
6. 如果 `connectivity_ok=false` 而前 4 项都 OK → `proxyctl trace google.com`
7. 切网后 → `proxyctl recover`（不重启进程）
8. 拿不到锁（exit=8 LOCKED）→ 见下方"锁文件位置 + 手动释放"

## Non-Interactive — non-interactive 承诺

proxyctl 在 stdin 非 TTY 时**不会**调用 `input()` 等阻塞读取。
设置 `PROXYCTL_AGENT=1` 等价同时打开 `--json + --no-color + 非交互`，
所有命令默认输出 envelope v2，写操作摘要打到 stderr。

## Locks — 锁文件位置 + 手动释放

写操作（mode/engine/fix/audit apply/config set/daemon/dns-lock 等）通过
`fcntl.flock` 保护，并发冲突返回 `LOCKED(8)`。

```
锁文件位置：{lock_dir}/.lock.{{system,config,daemon,traffic}}
诊断：lsof <锁文件>                # 看谁持有
     ps -p <PID>                  # 验证是否为活的 proxyctl
释放：（极端情况）rm <锁文件>      # 仅当 lsof 显示无持有者
```

LOCKED 错误的 `hints` 列表已包含具体锁路径。

## Footgun — footgun 提醒

- `mode tun` 需要 sudo；macOS launchd 已自动 sudo prompt
- `audit apply` 会写引擎 `rules:` 段；建议先 `proxyctl audit apply --dry-run`
- `engine <other>` 切换会替换 launchd plist 并重启 daemon，sudo 必需
- 多实例：默认 `proxy_port=7890` 冲突；第二实例改 `~/.config/proxyctl/config.yaml` 的 `proxy_port`
- 节点订阅由 mihomo `proxy-providers` 自管；proxyctl 不会刷新订阅
- `--quiet` 仅压制非关键 stderr；envelope / error 仍输出

## Self-Discovery — 自发现

- `proxyctl --version --json` — schema_version + supported_features 探测
- `proxyctl commands --json` — 全部命令元数据
- `proxyctl commands --schema` — 上面 JSON 的 JSON Schema
- `proxyctl explain` 速查 / `proxyctl explain <topic>` 卡片
- `proxyctl help <cmd>` — 单命令完整说明
- `proxyctl doctor --json` — 健康基线

## Repo — 仓库视角

如果你正在编辑 proxyctl 源码（而非调用安装好的 CLI），仓库根的
`AGENTS.md` 含开发约定（DISPATCH 注册 / 错误路径 / 锁 / 提交规范）。
"""


# ── 主入口 3: commands ────────────────────────────────────────────────────

# side_effects 枚举（与 PR-5 引入；agent 用以稳定解析"这条命令会改什么"）
# 字段语义：
#   process       启停 daemon / 引擎
#   system        改系统 DNS / proxy / 路由表（macOS networksetup / scutil 等）
#   config-write  写 proxyctl 或引擎配置文件（含 launchd plist）
#   cache         清 DNS / fakeip 缓存
#   network-io    主动向上游 API / 互联网发起非只读 HTTP 请求
SIDE_EFFECT_ENUM: tuple[str, ...] = (
    "process", "system", "config-write", "cache", "network-io",
)


# 命令元数据表（与 cli.DISPATCH 一一对应）
COMMANDS_META: list[dict] = [
    # lifecycle
    {"name": "start", "group": "lifecycle", "summary": "启动引擎 + 注入 DNS/代理",
     "args": [], "supports_json": False,
     "side_effects": ["process", "system"],
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1, 5],
     "examples": ["proxyctl start", "proxyctl start --dry-run"]},
    {"name": "stop", "group": "lifecycle", "summary": "停止引擎 + 还原系统配置",
     "args": [], "supports_json": False,
     "side_effects": ["process", "system"],
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1],
     "examples": ["proxyctl stop", "proxyctl stop --dry-run"]},
    {"name": "restart", "group": "lifecycle", "summary": "重启引擎",
     "args": [], "supports_json": False,
     "side_effects": ["process", "system"],
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False, "exit_codes": [0, 1],
     "examples": ["proxyctl restart", "proxyctl restart --dry-run"]},
    {"name": "restart-clean", "group": "lifecycle", "summary": "重启并清除缓存",
     "args": [], "supports_json": False,
     "side_effects": ["process", "system", "cache"],
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False, "exit_codes": [0, 1],
     "examples": ["proxyctl restart-clean",
                  "proxyctl restart-clean --dry-run"]},
    # diagnostic
    {"name": "status", "group": "diagnostic",
     "summary": "系统状态面板（含订阅状态：到期/流量/拉取健康度，v0.4.4+）",
     "args": [], "supports_json": True, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 5],
     "examples": ["proxyctl status",
                  "proxyctl status --json",
                  "proxyctl status --json | jq .data.subscription"]},
    {"name": "doctor", "group": "diagnostic", "summary": "极简 5 项健康打分（最快）",
     "args": [], "supports_json": True, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 5],
     "examples": ["proxyctl doctor", "proxyctl doctor --json",
                  "proxyctl doctor --suggest-only",
                  "proxyctl doctor --since 0.4.7"]},
    {"name": "autostart", "group": "maintenance",
     "summary": "自动启动 unit 管理（inspect / sync）",
     "args": [{"name": "subcmd", "choices": ["inspect", "sync"],
               "required": False}],
     "supports_json": True,
     "side_effects": [],
     "conditional_side_effects": {"sync": ["process", "system", "config-write"]},
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 2, 6, 8, 10],
     "examples": ["proxyctl autostart",
                  "proxyctl autostart inspect --json",
                  "proxyctl autostart sync --dry-run",
                  "proxyctl autostart sync"]},
    {"name": "check", "group": "diagnostic", "summary": "全面健康检查（4 阶段）",
     "args": [], "supports_json": True, "side_effects": ["network-io"],
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 1, 5, 7],
     "examples": ["proxyctl check", "proxyctl check --json"]},
    {"name": "connections", "group": "diagnostic",
     "summary": "本机 App/进程连接、macOS socket owner 与 mihomo /connections 关联",
     "args": [{"name": "keyword", "required": False, "repeatable": True,
               "positional": True},
              {"name": "--app", "required": False, "repeatable": True},
              {"name": "--host", "required": False, "repeatable": True},
              {"name": "--chain", "required": False, "repeatable": True},
              {"name": "--line", "required": False, "repeatable": True},
              {"name": "--route", "required": False, "repeatable": True},
              {"name": "--preset", "required": False, "repeatable": True},
              {"name": "--agent", "required": False, "repeatable": True},
              {"name": "--query", "required": False, "repeatable": True},
              {"name": "--filter", "required": False, "repeatable": True},
              {"name": "--verbose", "required": False},
              {"name": "--all", "required": False}],
     "supports_json": True, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 2, 10],
     "examples": ["proxyctl connections",
                  "proxyctl connections codex",
                  "proxyctl connections claude --verbose",
                  "proxyctl connections 443 anthropic",
                  "proxyctl connections --preset ai",
                  "proxyctl connections --host anthropic.com",
                  "proxyctl connections --chain SG-Residential-01",
                  "proxyctl connections --route proxy",
                  "proxyctl connections --agent codex --json"]},
    {"name": "traffic", "group": "diagnostic",
     "summary": "按线路/软件统计当前活跃或已采样的 Mihomo 连接流量",
     "args": [{"name": "subcmd", "required": False,
               "choices": ["snapshot", "sample", "watch", "report"]},
              {"name": "--by", "required": False, "repeatable": True},
              {"name": "--interval", "required": False},
              {"name": "--count", "required": False},
              {"name": "--since", "required": False},
              {"name": "--store", "required": False},
              {"name": "--host", "required": False, "repeatable": True},
              {"name": "--chain", "required": False, "repeatable": True},
              {"name": "--line", "required": False, "repeatable": True},
              {"name": "--route", "required": False, "repeatable": True},
              {"name": "--preset", "required": False, "repeatable": True},
              {"name": "--agent", "required": False, "repeatable": True},
              {"name": "--query", "required": False, "repeatable": True},
              {"name": "--filter", "required": False, "repeatable": True},
              {"name": "--app", "required": False, "repeatable": True},
              {"name": "--all", "required": False}],
     "supports_json": True, "side_effects": [],
     "conditional_side_effects": {"sample": ["cache"], "watch": ["cache"]},
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 2],
     "examples": ["proxyctl traffic",
                  "proxyctl traffic --by line,app",
                  "proxyctl traffic sample",
                  "proxyctl traffic watch --interval 5 --count 12",
                  "proxyctl traffic report --since 1h --by line,app",
                  "proxyctl traffic --chain residential-sg",
                  "proxyctl traffic --route proxy --preset ai",
                  "proxyctl traffic --json"]},
    {"name": "trace", "group": "diagnostic", "summary": "域名链路诊断",
     "args": [{"name": "domain", "required": True}],
     "supports_json": True, "side_effects": ["network-io"],
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 1, 2],
     "examples": ["proxyctl trace github.com",
                  "proxyctl trace github.com --json"]},
    {"name": "audit", "group": "diagnostic",
     "summary": "扫描日志找疑似应直连域名；apply 子命令会写 rules 段",
     "args": [{"name": "days_or_apply", "required": False}],
     "supports_json": True,
     "side_effects": [],
     "conditional_side_effects": {"apply": ["config-write"]},
     "supports_dry_run": True,
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 1, 2, 8],
     "examples": ["proxyctl audit 7", "proxyctl audit apply 7",
                  "proxyctl audit apply --dry-run",
                  "proxyctl audit --json 7"]},
    {"name": "bench", "group": "diagnostic",
     "summary": "代理组测速（--json 为 NDJSON 流式 + summary envelope）",
     "args": [{"name": "groups", "required": False, "variadic": True}],
     "supports_json": True, "side_effects": ["network-io"],
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 1, 3, 7],
     "examples": ["proxyctl bench", "proxyctl bench proxy",
                  "proxyctl bench --json proxy"]},
    # config & mode
    {"name": "mode", "group": "config", "summary": "切换 tun / proxy 模式",
     "args": [{"name": "target", "choices": ["tun", "proxy"], "required": False}],
     "supports_json": False, "side_effects": ["config-write"],
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1, 2],
     "examples": ["proxyctl mode tun", "proxyctl mode tun --dry-run"]},
    {"name": "engine", "group": "config", "summary": "切换代理引擎后端",
     "args": [{"name": "target", "choices": ["mihomo", "singbox"], "required": False}],
     "supports_json": False, "side_effects": ["config-write", "process"],
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1, 2, 3, 4, 5, 8],
     "examples": ["proxyctl engine mihomo", "proxyctl engine singbox --dry-run"]},
    {"name": "fix", "group": "maintenance", "summary": "修复 DNS / 代理 / 热重载",
     "args": [], "supports_json": False, "side_effects": ["system", "cache"],
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False, "exit_codes": [0, 1, 5, 8],
     "examples": ["proxyctl fix", "proxyctl fix --dry-run"]},
    {"name": "recover", "group": "maintenance",
     "summary": "切网后软恢复（清 DNS 缓存 + 重测代理组，不重启）",
     "args": [], "supports_json": False, "side_effects": ["cache", "network-io"],
     "supports_dry_run": True,
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 1, 2, 5, 7],
     "examples": ["proxyctl recover", "proxyctl recover --dry-run"]},
    # daemon / dns-lock
    {"name": "daemon", "group": "daemon",
     "summary": "管理 extra_daemons（如 claude-proxy）",
     "args": [{"name": "name", "required": False},
              {"name": "subcmd", "required": False,
               "choices": ["start", "stop", "restart", "status", "log"]}],
     "supports_json": False,
     "side_effects": [],
     "conditional_side_effects": {
         "start":   ["process"],
         "stop":    ["process"],
         "restart": ["process"],
     },
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1, 2, 3, 4, 6, 8],
     "examples": ["proxyctl daemon", "proxyctl daemon claude-proxy status",
                  "proxyctl daemon claude-proxy start --dry-run"]},
    {"name": "claude-proxy", "group": "daemon",
     "summary": "daemon claude-proxy 的别名（向后兼容）",
     "args": [{"name": "subcmd", "required": False}],
     "supports_json": False,
     "side_effects": [],
     "conditional_side_effects": {
         "start":   ["process"],
         "stop":    ["process"],
         "restart": ["process"],
     },
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1, 3, 4, 8],
     "examples": ["proxyctl claude-proxy status",
                  "proxyctl claude-proxy start --dry-run"]},
    {"name": "dns-lock", "group": "daemon", "summary": "启动 DNS 看门狗",
     "args": [], "supports_json": False,
     "side_effects": ["process", "config-write"],
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False, "exit_codes": [0, 1, 4, 10],
     "examples": ["proxyctl dns-lock", "proxyctl dns-lock --reload",
                  "proxyctl dns-lock --dry-run"]},
    {"name": "dns-unlock", "group": "daemon", "summary": "停止 DNS 看门狗",
     "args": [], "supports_json": False, "side_effects": ["process"],
     "supports_dry_run": True,
     "needs_sudo": True, "interactive": False, "exit_codes": [0, 1],
     "examples": ["proxyctl dns-unlock", "proxyctl dns-unlock --dry-run"]},
    # tools / agent
    {"name": "env", "group": "tool", "summary": "输出代理环境变量（可 eval）",
     "args": [], "supports_json": False, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0],
     "examples": ["proxyctl env", "proxyctl env --unset"]},
    {"name": "log", "group": "tool",
     "summary": "查看后端日志：默认 tail -f；支持 --tail N / --no-follow / --json",
     "args": [], "supports_json": True, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 2, 3],
     "examples": ["proxyctl log", "proxyctl log --tail 50 --no-follow"]},
    {"name": "plugins", "group": "tool", "summary": "显示已加载插件",
     "args": [], "supports_json": True, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0],
     "examples": ["proxyctl plugins", "proxyctl plugins --json"]},
    # agent-only
    {"name": "explain", "group": "agent",
     "summary": "解释 topic：rules / nodes / config / dns / engine / 等",
     "args": [{"name": "topic", "required": False,
               "choices": sorted(TOPICS.keys())}],
     "supports_json": True, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 2],
     "examples": ["proxyctl explain", "proxyctl explain rules --json"]},
    {"name": "agent-guide", "group": "agent",
     "summary": "给 LLM Agent 的入门 markdown（含能力边界、退出码、决策树）；"
                "支持 --section <name> 按需取小块",
     "args": [], "supports_json": True, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 2],
     "examples": ["proxyctl agent-guide",
                  "proxyctl agent-guide --list-sections",
                  "proxyctl agent-guide --section envelope --json",
                  "proxyctl agent-guide --json"]},
    {"name": "commands", "group": "agent",
     "summary": "所有命令的元数据（含 side_effects / needs_sudo / exit_codes）；"
                "--schema 输出 JSON Schema",
     "args": [], "supports_json": True, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0],
     "examples": ["proxyctl commands --json",
                  "proxyctl commands --schema"]},
    {"name": "completion", "group": "agent",
     "summary": "生成 shell 补全脚本（bash / zsh / fish）",
     "args": [{"name": "shell", "choices": ["bash", "zsh", "fish"],
               "required": True}],
     "supports_json": True, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 2],
     "examples": ['eval "$(proxyctl completion zsh)"',
                  "proxyctl completion bash > ~/.proxyctl.bash"]},
    {"name": "config", "group": "agent",
     "summary": "proxyctl 自身配置：path | get <key> | set <key> <value>",
     "args": [{"name": "subcmd", "choices": ["path", "get", "set"], "required": True},
              {"name": "key", "required": False},
              {"name": "value", "required": False}],
     "supports_json": True,
     "side_effects": [],
     "conditional_side_effects": {"set": ["config-write"]},
     "supports_dry_run": True,
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 2, 3, 4, 6],
     "examples": ["proxyctl config path",
                  "proxyctl config get proxy_port",
                  "proxyctl config set proxy_port 7891",
                  "proxyctl config set proxy_port 7891 --dry-run",
                  "proxyctl config set no_proxy_extra '[\"corp.example.com\"]'"]},
    {"name": "help", "group": "agent",
     "summary": "顶层帮助 / 单命令帮助（等价 --help / <cmd> --help）",
     "args": [{"name": "command", "required": False}],
     "supports_json": False, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 2],
     "examples": ["proxyctl help", "proxyctl help mode",
                  "proxyctl mode --help"]},
    {"name": "version", "group": "agent",
     "summary": "版本号 + supported_features（等价 --version；--json 输出 envelope）",
     "args": [], "supports_json": True, "side_effects": [],
     "needs_sudo": False, "interactive": False, "exit_codes": [0],
     "examples": ["proxyctl version", "proxyctl version --json"]},
]


_COMMANDS_DATA_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "proxyctl commands --json data schema (v0.3.0)",
    "type": "object",
    "required": ["schema_version", "version", "commands"],
    "properties": {
        "schema_version": {"const": _io.SCHEMA_VERSION},
        "version": {"type": "string"},
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "group", "summary", "supports_json",
                             "side_effects", "needs_sudo", "interactive",
                             "exit_codes", "examples"],
                "properties": {
                    "name": {"type": "string"},
                    "group": {"enum": ["lifecycle", "diagnostic", "config",
                                       "maintenance", "daemon", "tool",
                                       "agent"]},
                    "summary": {"type": "string"},
                    "supports_json": {"type": "boolean"},
                    "supports_dry_run": {"type": "boolean"},
                    "needs_sudo": {"type": "boolean"},
                    "interactive": {"type": "boolean"},
                    "side_effects": {
                        "type": "array",
                        "items": {"enum": list(SIDE_EFFECT_ENUM)},
                    },
                    "conditional_side_effects": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "array",
                            "items": {"enum": list(SIDE_EFFECT_ENUM)},
                        },
                    },
                    "exit_codes": {"type": "array",
                                    "items": {"type": "integer", "minimum": 0}},
                    "args": {"type": "array"},
                    "examples": {"type": "array",
                                  "items": {"type": "string"}},
                },
            },
        },
    },
}


def cmd_commands(args: list, backend, config) -> None:
    """proxyctl commands [--json] [--schema]

    --schema 输出 commands --json 的 JSON Schema (Draft 2020-12)。
    """
    from proxyctl.cli import VERSION
    as_json = GLOBAL_FLAGS_REF().get("json", False)

    # --schema 子模式：输出 data 的 schema 而不是 data 本身
    if args and args[0] == "--schema":
        if as_json:
            emit_json(envelope("commands", data=_COMMANDS_DATA_SCHEMA))
            return
        print(json.dumps(_COMMANDS_DATA_SCHEMA,
                         ensure_ascii=False, indent=2))
        return

    payload = {
        "schema_version": _io.SCHEMA_VERSION,
        "version": VERSION,
        "commands": COMMANDS_META,
    }
    if as_json:
        emit_json(envelope("commands", data=payload))
        return
    # 人类表（按 group）
    groups: dict[str, list] = {}
    for c in COMMANDS_META:
        groups.setdefault(c["group"], []).append(c)
    for g, items in groups.items():
        print(f"{BOLD}{g}{NC}")
        for c in items:
            flags = []
            if c["supports_json"]: flags.append("--json")
            if c["needs_sudo"]:    flags.append("sudo")
            se = c.get("side_effects")
            if isinstance(se, list) and se:
                flags.append("+".join(se))
            elif isinstance(se, str) and se and se != "none":
                flags.append(se)
            cse = c.get("conditional_side_effects") or {}
            for trigger, effects in cse.items():
                flags.append(f"[{trigger}]={'+'.join(effects)}")
            tag = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  {CYAN}{c['name']:<14}{NC} {c['summary']}{DIM}{tag}{NC}")
        print()
    print(f"{DIM}详细：proxyctl commands --json{NC}")


# ── 主入口 4: config ──────────────────────────────────────────────────────

def cmd_config(args: list, backend, config) -> None:
    """proxyctl config path | get <dot.key> | set <dot.key> <value>"""
    as_json = GLOBAL_FLAGS_REF().get("json", False)
    if not args:
        fail("缺少子命令",
             hint="proxyctl config path | get <key> | set <key> <value>",
             code=USAGE, cmd="config", as_json=as_json)

    sub = args[0]
    path = _io_proxyctl_config_path()

    if sub == "path":
        if as_json:
            emit_json(envelope("config", data={"path": path,
                                               "exists": os.path.isfile(path)}))
            return
        print(path)
        if not os.path.isfile(path):
            print(f"{YELLOW}(文件不存在，参考 config.yaml.example){NC}",
                  file=sys.stderr)
        return

    if sub == "get":
        if len(args) < 2:
            fail("config get 需要一个 key", hint="例：proxyctl config get proxy_port",
                 code=USAGE, cmd="config", as_json=as_json)
        key = args[1]
        try:
            value = _resolve_dot_key(config, key)
        except KeyError:
            fail(f"未找到字段：{key}",
                 hint=f"可用顶层字段示例：backend / api_base / proxy_port / corp_dns.server",
                 code=NOT_FOUND, cmd="config", as_json=as_json)
            return
        if as_json:
            emit_json(envelope("config",
                               data={"path": path, "key": key, "value": value}))
            return
        if isinstance(value, (dict, list)):
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            print(value)
        return

    if sub == "set":
        if len(args) < 3:
            fail("config set 需要 key 和 value 两个参数",
                 hint="例：proxyctl config set proxy_port 7891",
                 code=USAGE, cmd="config", as_json=as_json)
        key = args[1]
        raw_value = args[2]
        try:
            _cmd_config_set(path, key, raw_value, as_json=as_json)
        except _ConfigWriteError as e:
            fail(str(e), hint=e.hint, doc=e.doc, code=e.code,
                 cmd="config", as_json=as_json)
        return

    fail(f"未知子命令：{sub}",
         hint="proxyctl config path | get <key> | set <key> <value>",
         code=USAGE, cmd="config", as_json=as_json)


# ── config set 实现 ──────────────────────────────────────────────────────

class _ConfigWriteError(Exception):
    def __init__(self, msg: str, *, hint: str | None = None,
                 doc: str | None = None, code: int = 1):
        super().__init__(msg)
        self.hint = hint
        self.doc = doc
        self.code = code


def _coerce_value(raw: str):
    """字面值类型推断：int / float / bool / null / JSON list/dict / str。

    优先级：null/true/false → int → float → JSON 解析（[]/{}/""）→ 原 str。
    """
    s = raw.strip()
    if s in ("null", "None"):
        return None
    if s in ("true", "True"): return True
    if s in ("false", "False"): return False
    # int
    try:
        if s.lstrip("-").isdigit():
            return int(s)
    except ValueError:
        pass
    # float
    try:
        if "." in s and s.replace(".", "", 1).lstrip("-").isdigit():
            return float(s)
    except ValueError:
        pass
    # JSON 容器或带引号字符串
    if s and s[0] in "[{\"":
        try:
            return json.loads(s)
        except (ValueError, json.JSONDecodeError):
            pass
    return raw  # 原 str


def _set_dot_key(d: dict, key: str, value) -> None:
    parts = key.split(".")
    cur = d
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _cmd_config_set(path: str, key: str, raw_value: str,
                    *, as_json: bool) -> None:
    """原子写：备份 → 写 .new → rename → yaml 校验，失败回滚。"""
    import shutil
    import tempfile
    try:
        import yaml
    except ImportError:
        raise _ConfigWriteError("缺少 pyyaml 依赖",
                                hint="pip install pyyaml", code=6)

    # 读旧值
    if os.path.isfile(path):
        with open(path) as f:
            try:
                doc = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                raise _ConfigWriteError(f"现有配置文件 YAML 语法错: {e}",
                                        hint="proxyctl config path",
                                        doc="config", code=6)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {}

    new_value = _coerce_value(raw_value)
    try:
        old_value = _resolve_dot_key(doc, key)
    except KeyError:
        old_value = None
    _set_dot_key(doc, key, new_value)

    # 备份
    bak_path: str | None = None
    if os.path.isfile(path):
        bak_path = path + ".bak"
        shutil.copy2(path, bak_path)

    # 原子写：临时文件 → rename
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".config.", suffix=".new",
                                         dir=os.path.dirname(path))
    try:
        with os.fdopen(tmp_fd, "w") as f:
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
        # 校验：parse 一遍 tmp 文件
        with open(tmp_path) as f:
            yaml.safe_load(f)
        os.replace(tmp_path, path)
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        # 回滚（不动 path，因为还没替换；理论上 path 仍是旧内容）
        raise _ConfigWriteError(f"写配置失败: {e}",
                                hint=f"备份: {bak_path or '(none)'}",
                                doc="config", code=4)

    if as_json:
        emit_json(envelope("config",
                           data={"path": path, "key": key,
                                 "old_value": old_value, "new_value": new_value,
                                 "backup": bak_path}))
        return
    print(f"{GREEN}✓{NC} {key}: {old_value!r} → {new_value!r}")
    if bak_path:
        print(f"  {DIM}backup: {bak_path}{NC}")


def _resolve_dot_key(d: dict, key: str):
    cur: Any = d
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(key)
    return cur


# ── 主入口 5: doctor ──────────────────────────────────────────────────────

def cmd_doctor(args: list, backend, config) -> None:
    """proxyctl doctor [--json] [--no-suggest] [--suggest-only] [--since <ver>]
        — 健康打分 + 引导建议。

    Flags:
      --no-suggest         关闭建议引擎，恢复 v0.4.x 极简 5 项布尔
      --suggest-only       跳过 5 项 score 探测，仅跑建议引擎（最快 < 100ms）
                           data 中 5 项布尔置 null，agent 据 doctor_mode 字段识别
      --since <version>    屏蔽 since > 此版本的规则（老 CI 平滑迁移）

    suggestions 默认显示 warn+advisory（上限 3 条，info 仅 --json），
    永不影响 exit code。--quiet 完全跳过 suggestion 块（人类输出）。
    """
    as_json = GLOBAL_FLAGS_REF().get("json", False)
    quiet = GLOBAL_FLAGS_REF().get("quiet", False)
    argv = list(args or [])
    no_suggest = "--no-suggest" in argv
    suggest_only = "--suggest-only" in argv
    since_filter: str | None = None
    if "--since" in argv:
        try:
            si = argv.index("--since")
            if si + 1 < len(argv):
                since_filter = argv[si + 1]
        except ValueError:
            pass

    # 从 cli.py 复用 service_running，但为避免循环 import 在这里独立判断
    from proxyctl.cli import service_running, get_mode, get_engine_version
    port = config.get("proxy_port", 7890)
    api_base = config.get("api_base", "http://127.0.0.1:9090")

    # mode 先取（dns_ok 判定 + 显示均依赖；v0.5.4 起）
    try:
        mode_str = get_mode(backend)
    except Exception:
        mode_str = "unknown"

    if suggest_only:
        # Fast path：跳过所有 5 项 score 探测（最慢可累计 5-10s）
        engine_up = None
        port_listen = None
        dns_ok = None
        system_proxy_ok = None
        connectivity_ok = None
        flags_for_human = {
            "engine_up": None, "port_listen": None, "dns_ok": None,
            "system_proxy_ok": None, "connectivity_ok": None,
        }
        score = None
        hint = None
    else:
        engine_up = bool(service_running(backend))
        port_listen = _tcp_open("127.0.0.1", port)
        dns_ok = _dns_check_ok(mode_str)
        system_proxy_ok = _system_proxy_points_to_loopback(port)
        connectivity_ok = (_quick_connectivity(api_base, port)
                           if engine_up else False)
        flags_for_human = {
            "engine_up": engine_up,
            "port_listen": port_listen,
            "dns_ok": dns_ok,
            "system_proxy_ok": system_proxy_ok,
            "connectivity_ok": connectivity_ok,
        }
        score = sum(1 for v in flags_for_human.values() if v)
        hint = _doctor_hint(flags_for_human)

    # informational extra（不计分）
    try:
        held = _io.held_lock_names()
    except Exception:
        held = []
    lock_path_map = _io.lock_paths()

    engine_ver = get_engine_version(backend.name)

    # ── suggestions 引擎（v0.5.0+，与 score 解耦，永不影响 exit code）──────
    suggestions: list = []
    if not no_suggest:
        try:
            suggestions = _build_doctor_suggestions(
                backend, config, engine_ver, since_filter=since_filter)
        except Exception:
            # 建议引擎绝不能阻塞 doctor 主流程；任何意外都静默降级为空建议
            suggestions = []

    if suggest_only:
        healthy = None
        max_val: int | None = None
        code = OK  # suggest_only 不参与健康分判定
    else:
        healthy = (score == len(flags_for_human))
        max_val = len(flags_for_human)
        code = OK if healthy else ENGINE_DOWN

    data = {
        **flags_for_human,
        "score": score, "max": max_val,
        "healthy": healthy,        # 0.3.3：agent 不必自己算 score == max
        "hint": hint,
        # informational fields (W15 in 0.3.0):
        "engine": backend.name,
        "engine_version": engine_ver,  # v0.4.7+, None 即未知
        "mode": mode_str,
        "port": port,
        "config_path": _io_proxyctl_config_path(),
        "engine_config_path": backend.config_file,
        "lock_held": held,
        "lock_path": lock_path_map,
        # v0.5.0+：与 score 解耦的引导建议列表；--json 总是输出（含 info 级）
        "suggestions": suggestions,
        # v0.5.0+：doctor 运行模式（默认 'full'；--suggest-only 时 'suggest_only'）
        "doctor_mode": "suggest_only" if suggest_only else "full",
    }

    if as_json:
        emit_json(envelope("doctor", data=data, ok=healthy,
                           code=code, hint=hint))
        sys.exit(code)

    icon = lambda b: f"{GREEN}✓{NC}" if b else f"{RED}✗{NC}"
    ev = engine_ver or {}
    ev_tag = f" v{ev['version']}" if ev.get("version") else ""
    if suggest_only:
        print(f"{BOLD}proxyctl doctor{NC}  {DIM}(suggest-only mode){NC}  "
              f"{DIM}engine={backend.name}{ev_tag} mode={mode_str} port={port}{NC}")
        print(f"  {DIM}5 项 score 探测被跳过；仅跑 suggestion 引擎{NC}")
    else:
        print(f"{BOLD}proxyctl doctor{NC}  ({score}/{max_val})  "
              f"{DIM}engine={backend.name}{ev_tag} mode={mode_str} port={port}{NC}")
        print(f"  {icon(engine_up)}  engine_up        ({backend.name} 服务运行中)")
        print(f"  {icon(port_listen)}  port_listen      (127.0.0.1:{port})")
        dns_desc = ("proxy 模式无需 DNS 劫持" if mode_str == "proxy"
                    else "系统 DNS 含 127.0.0.1")
        print(f"  {icon(dns_ok)}  dns_ok           ({dns_desc})")
        print(f"  {icon(system_proxy_ok)}  system_proxy_ok  (macOS HTTP/HTTPS proxy)")
        print(f"  {icon(connectivity_ok)}  connectivity_ok  (https://www.google.com via proxy)")
        if held:
            print(f"  {DIM}lock_held: {', '.join(held)}{NC}")
        if hint:
            print(f"{CYAN}next:{NC} {hint}")

    # ── suggestions 人类输出块 ─────────────────────────────────────────
    # 规则：默认仅显 warn + advisory，上限 3 条；info 仅 --json
    # --quiet 完全跳过整块
    if suggestions and not quiet:
        visible = [s for s in suggestions if s["severity"] in ("warn", "advisory")]
        if visible:
            cap = 3
            shown = visible[:cap]
            extra = len(suggestions) - len(shown)
            print()
            print(f"{BOLD}suggestions{NC} ({len(shown)}/{len(suggestions)}):")
            for s in shown:
                mark = {"warn": "[!]", "advisory": "[*]", "info": "[i]"}.get(
                    s["severity"], "[?]")
                color = {"warn": RED, "advisory": YELLOW,
                         "info": DIM}.get(s["severity"], "")
                print(f"  {color}{mark}{NC} {CYAN}{s['id']:<32}{NC} {s['title']}")
                cmd = s.get("fix_command") or s.get("inspect_command")
                if cmd:
                    print(f"      {DIM}→ {cmd}{NC}")
            if extra > 0:
                print(f"  {DIM}...and {extra} more (use --json){NC}")

    sys.exit(code)


def _build_doctor_suggestions(backend, config, engine_ver, *,
                              since_filter: str | None = None) -> list:
    """采集所有 suggestion 输入并调 build_suggestions。

    所有 inspect_* 调用都独立 try-except，任一失败不影响其他维度。
    """
    from proxyctl import subscription as _sub
    from proxyctl import autostart as _autostart
    from proxyctl import suggest as _suggest
    from proxyctl import suggest_rules as _rules

    try:
        sub = _sub.load()
    except Exception:
        sub = None

    autostart_inspect = None
    path_binary = None
    path_version = None
    expected_cfg_dir = os.path.dirname(backend.config_file)
    try:
        static_inspect = _autostart.inspect_static(backend)
        autostart_inspect = _autostart.inspect_runtime(static_inspect, backend)
    except Exception:
        autostart_inspect = None

    if engine_ver:
        path_binary = engine_ver.get("binary")
        path_version = engine_ver.get("version")

    try:
        eng_cfg_inspect = _rules.inspect_engine_config(backend.config_file)
    except Exception:
        eng_cfg_inspect = None

    try:
        known_versions = _rules.load_known_versions()
    except Exception:
        known_versions = None

    # mihomo /proxies API — 仅在引擎在线时拉，本地 HTTP，timeout 0.5s
    proxies_payload = None
    try:
        api_base = config.get("api_base", "http://127.0.0.1:9090")
        api_secret = config.get("api_secret", "") or ""
        # 复用 doctor 已检测过的 engine_up（外部传入会更优雅，但本函数无该参数；
        # fetch_proxies 自身已 timeout + 静默降级，调一次也无害）
        proxies_payload = _rules.fetch_proxies(api_base, api_secret, timeout=0.5)
    except Exception:
        proxies_payload = None

    return _suggest.build_suggestions(
        sub=sub,
        autostart_inspect=autostart_inspect,
        path_binary=path_binary,
        path_version=path_version,
        expected_config_dir=expected_cfg_dir,
        engine_config_inspect=eng_cfg_inspect,
        known_versions=known_versions,
        engine_config_dir=expected_cfg_dir,
        proxies_payload=proxies_payload,
        since=since_filter,
        apply_user_ignore=True,
    )


def _tcp_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _dns_points_to_loopback() -> bool:
    """系统 DNS 列表中是否含 127.0.0.1 / ::1。"""
    if sys.platform == "darwin":
        r = subprocess.run(["scutil", "--dns"], capture_output=True, text=True)
        return "nameserver[0] : 127.0.0.1" in r.stdout or "127.0.0.1" in r.stdout
    # Linux：读 /etc/resolv.conf
    try:
        with open("/etc/resolv.conf") as f:
            text = f.read()
        return "127.0.0.1" in text or "::1" in text
    except OSError:
        return False


def _dns_check_ok(mode: str) -> bool:
    """doctor dns_ok 判定（v0.5.4：mode-aware）。

    proxy 模式无需系统 DNS 指向 127.0.0.1——流量走 HTTP/SOCKS 代理，
    mihomo 不在 :53 监听，系统 DNS 用 systemd-resolved / DHCP 默认即可。
    旧逻辑在 Ubuntu（DNS=127.0.0.53 stub）+ proxy 模式下永远判 ✗
    并建议 `proxyctl fix`——但 fix 的 DNS 改写只对 macOS TUN 模式有意义。

    tun / mixed / 未知模式：保留旧的"系统 DNS 含 127.0.0.1"判定。
    """
    if mode == "proxy":
        return True
    return _dns_points_to_loopback()


def _system_proxy_points_to_loopback(port: int) -> bool:
    """macOS：networksetup -getwebproxy 是否指向 127.0.0.1:port。Linux：跳过判 True。"""
    if sys.platform != "darwin":
        return True
    try:
        r = subprocess.run(["networksetup", "-getwebproxy", "Wi-Fi"],
                           capture_output=True, text=True, timeout=2)
        return "127.0.0.1" in r.stdout and str(port) in r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def _quick_connectivity(api_base: str, port: int) -> bool:
    """通过本地代理端口请求一个轻量 URL。"""
    proxy = f"http://127.0.0.1:{port}"
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-x", proxy, "--max-time", "2", "https://www.google.com"],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip().startswith(("2", "3"))
    except (OSError, subprocess.TimeoutExpired):
        return False


def _doctor_hint(flags: dict) -> str | None:
    if not flags["engine_up"]:
        return "proxyctl start"
    if not flags["port_listen"]:
        return "proxyctl restart"
    if not flags["dns_ok"]:
        return "proxyctl fix"
    if not flags["system_proxy_ok"]:
        return "proxyctl fix"
    if not flags["connectivity_ok"]:
        return "proxyctl trace google.com"
    return None


# ── GLOBAL_FLAGS 注入（让本模块读到 cli.main() 解析出的 flag）─────────────
# 为避免循环 import，cli.py 把自己的 GLOBAL_FLAGS dict 通过一个 setter 传过来；
# 这里用一个间接函数取值（cli 调用时已经把 dict 引用注入）。

_GLOBAL_FLAGS: dict = {"json": False, "no_color": False, "quiet": False}


def GLOBAL_FLAGS_REF() -> dict:
    return _GLOBAL_FLAGS


def set_global_flags(flags: dict) -> None:
    """让 explain 模块 + _io 模块的 json 模式保持同步。

    cli.main() 调一次即可；测试若想绕过 cli.main 直接调子命令，也应该用
    这个 setter（而不是手工写 _GLOBAL_FLAGS），否则 _io.fail / _io.is_json_mode
    会读到旧值，错误路径不输出 envelope。
    """
    _GLOBAL_FLAGS.clear()
    _GLOBAL_FLAGS.update(flags)
    _io.set_json_mode(bool(flags.get("json", False)))


# ── Suggestion Topics (v0.5.0+) ───────────────────────────────────────────
# 每个 suggestion.id 对应一个 `proxyctl explain suggestion:<id>` topic，
# CI 强校验：tests/unit/test_suggest_explain_completeness.py 遍历所有 id
# 必须能 explain 出非空 card。

_SUGGESTION_DOCS: dict[str, dict] = {
    # ── 订阅 7 条 ─────────────────────────────────────────────────────
    "subscription.expired": {
        "summary": "订阅已过期。proxyctl 不主动续订——续费/换源由用户脚本或人工处理。",
        "edit": (
            "  # 1. 续费机场\n"
            "  # 2. 用户脚本拉新订阅，写 ~/.config/proxyctl/subscription.json\n"
            "  # 3. proxyctl restart 让引擎读新配置"
        ),
        "verify": "proxyctl status --json | jq .data.subscription",
        "next_commands": ["status", "explain subscription"],
    },
    "subscription.expiring_soon": {
        "summary": "订阅 7 天内到期。提前续费避免业务中断。",
        "edit": "  # 同 subscription.expired，提前操作即可",
        "verify": "proxyctl status --json | jq .data.subscription.expire_days_left",
        "next_commands": ["status", "explain subscription"],
    },
    "subscription.traffic_exhausted": {
        "summary": "套餐流量已 100% 用完。继续用可能被限速或停服。",
        "edit": "  # 升级套餐 / 等月初重置 / 换机场",
        "verify": "proxyctl status --json | jq .data.subscription.traffic_used_pct",
        "next_commands": ["status"],
    },
    "subscription.traffic_warn": {
        "summary": "流量已用 ≥ 90%。提前升级套餐或限制后台流量。",
        "edit": "  # 1. 升级套餐\n  # 2. 暂停大流量应用（云盘同步等）",
        "verify": "proxyctl status --json",
        "next_commands": ["status"],
    },
    "subscription.traffic_high": {
        "summary": "流量已用 ≥ 70%。监控级别建议，无须立即处理。",
        "edit": "  # 关注剩余流量趋势，必要时升级",
        "verify": "proxyctl status --json",
        "next_commands": ["status"],
    },
    "subscription.last_fetch_error": {
        "summary": (
            "最近一次订阅拉取失败。原因：网络挂 / 机场跑路 / 密钥变更 / 脚本 bug。"
        ),
        "edit": (
            "  # 1. 手动重跑用户脚本（如 update-subscription.sh）\n"
            "  # 2. 检查机场 URL / token 是否仍有效\n"
            "  # 3. 看脚本日志 ~/.config/proxyctl/dns-watchdog.log 或对应 cron 输出"
        ),
        "verify": "proxyctl status --json | jq '.data.subscription | {fetch_ok,fetch_error}'",
        "next_commands": ["status", "explain subscription"],
    },
    "subscription.stale": {
        "summary": (
            "订阅快照超过 24h 未更新。可能：cron 脚本停了 / 用户脚本崩了 / 机器睡眠。"
        ),
        "edit": (
            "  # 1. 确认 cron / launchd 任务在跑\n"
            "  # 2. 检查脚本最近输出\n"
            "  # 3. 手动重跑一次拉订阅"
        ),
        "verify": "ls -la ~/.config/proxyctl/subscription.json",
        "next_commands": ["status"],
    },
    "subscription.missing": {
        "summary": (
            "未配置订阅状态快照。proxyctl 不主动拉订阅——需用户脚本写 "
            "~/.config/proxyctl/subscription.json 后才能显示订阅状态。"
        ),
        "edit": "  # 参考 proxyctl explain subscription 配置用户脚本",
        "verify": "test -f ~/.config/proxyctl/subscription.json && echo OK",
        "next_commands": ["explain subscription"],
    },

    # ── Autostart 8 条 ─────────────────────────────────────────────────
    "autostart.unit_missing": {
        "summary": (
            "自动启动 unit（macOS plist / Linux systemd user unit）未安装。"
            "重启系统后引擎不会自动启动。"
        ),
        "edit": (
            "  # macOS:\n"
            "  sudo cp ~/.config/proxyctl/launchdaemons/com.mihomo.tun.plist /Library/LaunchDaemons/\n"
            "  sudo launchctl bootstrap system /Library/LaunchDaemons/com.mihomo.tun.plist\n\n"
            "  # Linux:\n"
            "  cp systemd/mihomo.service ~/.config/systemd/user/\n"
            "  systemctl --user daemon-reload && systemctl --user enable --now mihomo.service"
        ),
        "verify": "proxyctl doctor --json | jq '.data.suggestions[] | select(.id==\"autostart.unit_missing\")'",
        "next_commands": ["doctor"],
    },
    "autostart.binary_missing": {
        "summary": (
            "autostart unit 引用的引擎二进制不存在。常见于 brew uninstall 后忘了更新 plist。"
        ),
        "edit": (
            "  # 1. 装回引擎：brew install mihomo  或  cargo install ... \n"
            "  # 2. 或编辑 plist/unit 改 binary 路径到当前 PATH 里的 mihomo\n"
            "  # 3. 重新 bootstrap"
        ),
        "verify": "ls -la $(grep -oE '/[^ ]*mihomo' /Library/LaunchDaemons/com.mihomo.tun.plist | head -1)",
        "next_commands": ["doctor", "explain suggestion:autostart.unit_missing"],
    },
    "autostart.binary_mismatch": {
        "summary": (
            "autostart 跑的 binary 与 PATH 里的不同。`mihomo -v` 看到一个版本，"
            "实际服务跑另一个。哥之前调试 TUIC 时栽过的坑。"
        ),
        "edit": (
            "  # 一键修复（v0.5.0+）：\n"
            "  proxyctl autostart sync --dry-run   # 预览\n"
            "  proxyctl autostart sync             # 写 plist + bootstrap\n"
            "  # 手动方式：\n"
            "  # 1. 选定权威路径（一般 PATH 里那个最新）\n"
            "  # 2. 改 plist/unit 的 binary 路径到权威路径\n"
            "  # 3. launchctl bootout + bootstrap 让 plist 生效"
        ),
        "verify": "diff <($(which mihomo) -v) <(/path/in/plist -v)",
        "next_commands": ["autostart", "doctor"],
    },
    "autostart.version_mismatch": {
        "summary": (
            "autostart binary 与 PATH binary 报告不同版本号。可能是双安装"
            "（brew + 手动装）或 plist 指向被遗忘的老路径。"
        ),
        "edit": (
            "  proxyctl autostart sync   # v0.5.0+ 一键同步到 PATH 版本"),
        "verify": "proxyctl doctor --json | jq '.data.suggestions[] | select(.id==\"autostart.version_mismatch\") .evidence'",
        "next_commands": ["autostart", "doctor",
                          "explain suggestion:autostart.binary_mismatch"],
    },
    "autostart.config_dir_mismatch": {
        "summary": (
            "autostart unit 指向的 config 目录与 proxyctl 看到的不一致。"
            "改了配置但 autostart 跑的是另一份的高风险来源。"
        ),
        "edit": (
            "  proxyctl autostart sync   # v0.5.0+ 一键修复\n"
            "  # 手动方式：改 plist/unit 的 -d 参数到权威目录 + bootstrap"
        ),
        "verify": "grep -E '\\-d ' /Library/LaunchDaemons/com.mihomo.tun.plist",
        "next_commands": ["autostart", "doctor"],
    },
    "autostart.placeholder_unrendered": {
        "summary": (
            "autostart unit 模板未被 install.sh 替换占位符（如 'yourname'）。"
            "服务无法启动。"
        ),
        "edit": "  # 重新跑 install.sh，或手动 sed 替换 yourname → $USER",
        "verify": "grep -n yourname /Library/LaunchDaemons/com.mihomo.tun.plist",
        "next_commands": ["doctor"],
    },
    "autostart.disabled": {
        "summary": "unit 文件存在但未 bootstrap / enable，重启后不会自动启动。",
        "edit": (
            "  # macOS:\n"
            "  sudo launchctl bootstrap system /Library/LaunchDaemons/com.mihomo.tun.plist\n"
            "  # Linux:\n"
            "  systemctl --user enable mihomo.service"
        ),
        "verify": "launchctl print system/com.mihomo.tun  # 或 systemctl --user is-enabled mihomo",
        "next_commands": ["doctor", "start"],
    },
    "autostart.flapping": {
        "summary": (
            "autostart 服务最近异常退出（macOS LastExitStatus != 0 / Linux is-failed）。"
            "KeepAlive 在背后反复重启，但用户感知不到。"
        ),
        "edit": (
            "  # 1. 看引擎日志 ~/.config/mihomo/mihomo.log / .err\n"
            "  # 2. 看系统层日志：\n"
            "  #    macOS: launchctl print system/com.mihomo.tun\n"
            "  #    Linux: journalctl --user -u mihomo.service -n 100\n"
            "  # 3. 修复 config 错误后 proxyctl restart"
        ),
        "verify": "proxyctl log",
        "next_commands": ["log", "doctor"],
    },

    # ── Controller / Engine / Data 5 条 ────────────────────────────────
    "controller.empty_secret": {
        "summary": (
            "Clash API bind 公网（0.0.0.0 或局域网 IP）且 secret 为空——"
            "**任何人都能调你的 API**：切换节点、读取节点列表、改路由。"
            "v0.5.1+ 此规则仅在公网 bind 时触发；127.0.0.1 + 空 secret 不报。"
        ),
        "edit": (
            "  # 优先：把 bind 改回环回（推荐，本机用够了）\n"
            "  #   external-controller: 127.0.0.1:9090\n"
            "  # 或：配强 secret\n"
            "  #   secret: <用 openssl rand -hex 24 生成>"
        ),
        "verify": "grep -E '^secret:|^external-controller:' ~/.config/mihomo/config.yaml",
        "next_commands": ["explain suggestion:controller.public_bind"],
    },
    "controller.weak_secret": {
        "summary": (
            "Clash API bind 公网（0.0.0.0 或局域网 IP）且 secret < 16 字符 → "
            "暴露面 + 弱认证 = 易爆破。**v0.5.1+ 此规则仅在公网 bind 时触发**；"
            "bind 127.0.0.1 时 secret 强度无意义（attack surface 不存在），不报。"
        ),
        "edit": (
            "  # 优先：bind 改回环回\n"
            "  #   external-controller: 127.0.0.1:9090\n"
            "  # 或：升级 secret\n"
            "  #   secret: <用 openssl rand -hex 24 生成的强 secret>"
        ),
        "verify": "grep -E '^secret:|^external-controller:' ~/.config/mihomo/config.yaml",
        "next_commands": ["explain suggestion:controller.public_bind"],
    },
    "controller.public_bind": {
        "summary": (
            "Clash API external-controller bind 到 0.0.0.0 或局域网 IP。"
            "局域网任何设备都能控制你的代理 —— 包括切换节点、读取节点列表。"
        ),
        "edit": (
            "  # 在 mihomo config.yaml 改回环回：\n"
            "  external-controller: 127.0.0.1:9090\n"
            "  # 如果确实需要远程访问，强制配 secret 且至少 24 字符"
        ),
        "verify": "grep -E '^external-controller:' ~/.config/mihomo/config.yaml",
        "next_commands": ["explain suggestion:controller.empty_secret"],
    },
    "engine.outdated": {
        "summary": (
            "引擎版本 < known_versions.json 中的 safe_min_version，"
            "或当前版本在 unsafe_versions 黑名单中（如已知 QUIC race / TUN regression）。"
        ),
        "edit": (
            "  # 1. 升级 binary：brew upgrade mihomo  或重新下载\n"
            "  # 2. 重启服务：proxyctl restart\n"
            "  # 3. known_versions.json 由用户脚本维护，schema：\n"
            "  #    {\"safe_min_version\":\"1.18.0\",\"unsafe_versions\":[],\"updated_at\":\"...\"}"
        ),
        "verify": "mihomo -v",
        "next_commands": ["status", "restart"],
    },
    "data.geo_stale": {
        "summary": (
            "GeoIP / GeoSite 数据库 > 30 天未更新。新加入的网站可能走错路由分流。"
            "对中国大陆用户特别相关——分流规则依赖最新的 CN 段。"
        ),
        "edit": (
            "  # 用户脚本拉最新数据：\n"
            "  cd ~/.config/mihomo\n"
            "  curl -L -o geoip.dat https://github.com/MetaCubeX/meta-rules-dat/raw/release/geoip.dat\n"
            "  curl -L -o geosite.dat https://github.com/MetaCubeX/meta-rules-dat/raw/release/geosite.dat\n"
            "  proxyctl restart"
        ),
        "verify": "stat -f '%m %N' ~/.config/mihomo/geo*.dat",
        "next_commands": ["restart"],
    },
    "proxy_group.mostly_dead": {
        "summary": (
            "单个代理组中 ≥ 70% 节点延迟为 0（已挂或测速失败）。组类型限于"
            "URLTest / Selector / Fallback / LoadBalance / Smart——只有这些会被"
            "用户分流到。多组同时挂会输出多条独立 suggestion（fingerprint 含"
            "group_name，agent 可分别跟踪）。"
        ),
        "edit": (
            "  # 1. 跑 proxyctl bench 重测延迟（部分节点可能临时失活）\n"
            "  # 2. 检查机场订阅是否过期（proxyctl status --json | jq .data.subscription）\n"
            "  # 3. 切换备用订阅源 / 联系机场\n"
            "  # 4. 如果只是少量节点，url-test 组会自动避开，无须干预"
        ),
        "verify": "proxyctl check --json | jq '.data.stages.groups'",
        "next_commands": ["bench", "check", "status"],
    },
}


def _make_suggestion_topic(sid: str, doc: dict):
    """工厂函数：用闭包绑定 sid/doc，避免 for 循环共享变量陷阱。"""
    @topic(f"suggestion:{sid}")
    def _t(backend, config) -> TopicCard:
        return {
            "topic": f"suggestion:{sid}",
            "summary": doc["summary"],
            "file": doc.get("file", "(no file)"),
            "edit": doc["edit"],
            "verify": doc["verify"],
            "next_commands": doc.get("next_commands", ["doctor"]),
        }
    return _t


for _sid, _doc in _SUGGESTION_DOCS.items():
    _make_suggestion_topic(_sid, _doc)
