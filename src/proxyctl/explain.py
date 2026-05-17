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
        "next": ["explain config", "trace <domain>", "audit"],
    }


@topic("nodes")
def _t_nodes(backend, config) -> TopicCard:
    return {
        "topic": "nodes",
        "summary": "代理节点（线路）— 出口节点和分组定义；订阅由 mihomo/sing-box 自身管理。",
        "file": f"{backend.config_file}  [proxies: / proxy-providers: / proxy-groups: 段]",
        "edit": (
            "  # 添加单个节点：在 proxies: 段加 entry，再加到 proxy-groups: 的某个组\n"
            "  # 订阅源：用 mihomo 自身的 proxy-providers: + url + path + interval\n"
            "  # proxyctl 不管订阅更新；用 mihomo 的 'proxy-providers' 热更新机制"
        ),
        "verify": "proxyctl bench <group>   # 测节点延迟",
        "next": ["bench", "explain engine"],
    }


@topic("config")
def _t_config(backend, config) -> TopicCard:
    return {
        "topic": "config",
        "summary": "proxyctl 自身配置（不是 mihomo 配置）。控制后端选择、Clash API、端口、企业 DNS 等。",
        "file": _io_proxyctl_config_path(),
        "edit": f"$EDITOR {_io_proxyctl_config_path()}",
        "verify": "proxyctl config get <key>",
        "next": ["explain ports", "explain corp-dns", "explain extra-daemons"],
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
        "next": ["explain troubleshooting", "explain corp-dns", "fix"],
    }


@topic("engine")
def _t_engine(backend, config) -> TopicCard:
    return {
        "topic": "engine",
        "summary": (
            f"代理引擎（后端）。当前：{backend.name}。"
            "支持 mihomo / sing-box；通过 proxyctl engine <name> 切换后端，"
            "通过 proxyctl mode <tun|proxy> 切换流量入站方式。"
        ),
        "file": f"{backend.config_file}",
        "edit": (
            "  proxyctl engine mihomo|singbox   # 切换后端实现\n"
            "  proxyctl mode tun                # TUN 模式（透明代理，需 sudo）\n"
            "  proxyctl mode proxy              # HTTP/SOCKS proxy 模式"
        ),
        "verify": "proxyctl status",
        "next": ["status", "explain mode", "explain ports"],
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
        "next": ["explain config", "config get proxy_port"],
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
        "next": ["explain config"],
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
        "edit": "  # no_proxy_extra: 追加内网域名 / Tailscale 段 / 企业 host",
        "verify": "eval \"$(proxyctl env)\" && env | grep -i proxy",
        "next": ["env", "env --unset"],
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
        "next": ["explain plugins", "explain dns"],
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
        "next": ["plugins"],
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
        "next": ["doctor", "status", "check", "trace", "fix", "recover"],
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
        "next": ["agent-guide", "commands"],
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
        "next": ["agent-guide", "commands", "explain exit-codes"],
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
    if card.get("next"):
        nxt = ", ".join(f"proxyctl {n}" for n in card["next"])
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
    """proxyctl agent-guide [--json] — 给 LLM Agent 一份 ≤200 行的自描述文档。"""
    as_json = GLOBAL_FLAGS_REF().get("json", False)
    text = _build_agent_guide(backend, config)
    if as_json:
        emit_json(envelope("agent-guide", data={"markdown": text}))
        return
    print(text)


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
    return f"""# proxyctl — Agent 接入指南

> 一句话：proxyctl 是 macOS（含 Linux 部分支持）的代理 *生命周期管理* CLI。
> 它管「启停 / 状态 / 健康检查 / DNS 防护 / 配置切换」，**它不装 mihomo、
> 不改具体规则、不改订阅** —— 这些去 mihomo 配置文件里改。

## 能做什么

- `start / stop / restart / restart-clean`：启停代理引擎（mihomo / sing-box）
- `status / check / trace / bench / audit`：诊断（status/check/trace 已读，audit 可写）
- `fix / recover`：修复 DNS / 代理；切网后软恢复
- `mode tun|proxy`：切换流量入站方式（写 mihomo 配置）
- `engine mihomo|singbox`：切换后端实现
- `daemon <name> <subcmd>`：管理额外 daemon（如 claude-proxy）
- `dns-lock / dns-unlock`：DNS 看门狗（对抗 DHCP / VPN 覆盖）
- `env`：输出代理环境变量
- `plugins`：列已加载插件

## 不能做什么（去别处改）

- 添加 / 修改 / 删除分流规则 → 编辑 `{mcfg}` 的 `rules:` 段
- 添加节点 / 改订阅 → 编辑 `{mcfg}` 的 `proxies:` / `proxy-providers:` 段
- 安装 mihomo / sing-box → 用 `brew install mihomo` 等
- 改全局系统 DNS 之外的网络栈 → 不在范围

## 概念地图（"想改 X 去哪"）

| 想改 | 文件 | 段 / 字段 |
|---|---|---|
| 分流规则 | `{mcfg}` | `rules:` |
| 节点 / 出口线路 | `{mcfg}` | `proxies:` / `proxy-providers:` / `proxy-groups:` |
| 代理引擎自身的 DNS / fakeip | `{mcfg}` | `dns:` |
| proxyctl 自己（API token / 端口 / 企业 DNS） | `{pcfg}` | 顶层 |
| 系统 DNS 行为 | (运行时由 proxyctl 管) | 用 `proxyctl fix` / `dns-lock` |

更多：`proxyctl explain <topic>`，topic 有
{', '.join(sorted(TOPICS.keys()))}。

## 关键路径

- proxyctl 配置: `{pcfg}`
- mihomo / sing-box 配置: `{mcfg}`
- 引擎缓存: `{cache}`
- 引擎日志: `{log}`
- 代理端口: HTTP/SOCKS = `{port}`，Clash API = `9090`
- 用户插件目录: `~/.config/proxyctl/plugins/*.py`

## 退出码

```
{exit_lines}
```

旧路径仍返回 1。新增子命令与新错误路径使用分语义码。

## 故障决策树（给 Agent 自动化）

1. `proxyctl doctor --json`  ← 最快，5 项布尔 + score
2. 如果 `engine_up=false` → `proxyctl start`
3. 如果 `port_listen=false`（引擎已启动但没监听）→ `proxyctl restart`
4. 如果 `dns_ok=false` → `proxyctl fix`
5. 如果 `system_proxy_ok=false` 且当前 mode=proxy → `proxyctl fix`
6. 如果 `connectivity_ok=false` 而前 4 项都 OK → `proxyctl trace google.com`
7. 切网后 → `proxyctl recover`（不重启进程）

## non-interactive 承诺

proxyctl 在 stdin 非 TTY 时**不会**调用 `input()` 等阻塞读取。
你可以放心从 Agent 沙箱里调用，不会被任何 prompt 卡住。

设置 `PROXYCTL_AGENT=1` 等价同时打开：

- `--json`：所有命令默认输出 envelope JSON（schema_version=1）
- `--no-color`：关闭 ANSI
- 写操作摘要打到 stderr 便于追踪

## JSON envelope（schema v1）

```json
{{
  "schema_version": 1,
  "cmd": "status",
  "ok": true,
  "data": {{ ... }},
  "error": null,
  "code": 0,
  "hint": null,
  "doc": null
}}
```

失败时 `ok:false / data:null / error/code/hint 填充`，envelope 写 stdout。
人类可读错误同步写 stderr。

## 支持 --json 的命令（schema v1）

`status`, `doctor`, `explain`, `agent-guide`, `commands`, `config`, `plugins`,
`log`（JSON Lines）。

`check / trace / audit / bench` 在 v0.2 仍是人类输出，--json 计划在后续版本接入。

## footgun（提醒）

- `mode tun`：开启 TUN 模式需要 sudo（macOS launchd 已自动处理）
- `audit apply`：会写入 mihomo 配置的 rules 段，建议先 `proxyctl audit` 看建议
- `engine <other>`：切换后端会切换 launchd plist，需 sudo
- 写操作会获取 `~/.config/proxyctl/.lock.*` 文件锁；并发调用另一个会立刻失败（exit 8）

## 自发现

- `proxyctl commands --json`：所有命令的元数据（`side_effects` / `needs_sudo` / `interactive` / `supports_json` / `exit_codes` / `examples`）
- `proxyctl explain <topic>`：每个概念的详细卡片
- `proxyctl --help`：人类速查
"""


# ── 主入口 3: commands ────────────────────────────────────────────────────

# 命令元数据表（与 cli.main() 的 if-elif 分发一一对应）
COMMANDS_META: list[dict] = [
    # lifecycle
    {"name": "start", "group": "lifecycle", "summary": "启动引擎 + 注入 DNS/代理",
     "args": [], "supports_json": False, "side_effects": "process+system",
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1], "examples": ["proxyctl start"]},
    {"name": "stop", "group": "lifecycle", "summary": "停止引擎 + 还原系统配置",
     "args": [], "supports_json": False, "side_effects": "process+system",
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1], "examples": ["proxyctl stop"]},
    {"name": "restart", "group": "lifecycle", "summary": "重启引擎",
     "args": [], "supports_json": False, "side_effects": "process+system",
     "needs_sudo": True, "interactive": False, "exit_codes": [0, 1],
     "examples": ["proxyctl restart"]},
    {"name": "restart-clean", "group": "lifecycle", "summary": "重启并清除缓存",
     "args": [], "supports_json": False, "side_effects": "process+system+cache",
     "needs_sudo": True, "interactive": False, "exit_codes": [0, 1],
     "examples": ["proxyctl restart-clean"]},
    # diagnostic
    {"name": "status", "group": "diagnostic", "summary": "系统状态面板",
     "args": [], "supports_json": True, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 5],
     "examples": ["proxyctl status", "proxyctl status --json"]},
    {"name": "doctor", "group": "diagnostic", "summary": "极简 5 项健康打分（最快）",
     "args": [], "supports_json": True, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 5],
     "examples": ["proxyctl doctor", "proxyctl doctor --json"]},
    {"name": "check", "group": "diagnostic", "summary": "全面健康检查（4 阶段）",
     "args": [], "supports_json": False, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 1, 5],
     "examples": ["proxyctl check"]},
    {"name": "trace", "group": "diagnostic", "summary": "域名链路诊断",
     "args": [{"name": "domain", "required": True}],
     "supports_json": False, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 1, 2],
     "examples": ["proxyctl trace github.com"]},
    {"name": "audit", "group": "diagnostic",
     "summary": "扫描日志找疑似应直连域名；apply 子命令会写 rules 段",
     "args": [{"name": "days_or_apply", "required": False}],
     "supports_json": False, "side_effects": "config-write (only with apply)",
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 1],
     "examples": ["proxyctl audit 7", "proxyctl audit apply 7"]},
    {"name": "bench", "group": "diagnostic", "summary": "代理组测速",
     "args": [{"name": "groups", "required": False, "variadic": True}],
     "supports_json": False, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 1],
     "examples": ["proxyctl bench", "proxyctl bench proxy"]},
    # config & mode
    {"name": "mode", "group": "config", "summary": "切换 tun / proxy 模式",
     "args": [{"name": "target", "choices": ["tun", "proxy"], "required": False}],
     "supports_json": False, "side_effects": "config-write",
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1, 2, 4, 6, 8], "examples": ["proxyctl mode tun"]},
    {"name": "engine", "group": "config", "summary": "切换代理引擎后端",
     "args": [{"name": "target", "choices": ["mihomo", "singbox"], "required": False}],
     "supports_json": False, "side_effects": "config-write+process",
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1, 2, 4, 8], "examples": ["proxyctl engine mihomo"]},
    {"name": "fix", "group": "maintenance", "summary": "修复 DNS / 代理 / 热重载",
     "args": [], "supports_json": False, "side_effects": "system",
     "needs_sudo": True, "interactive": False, "exit_codes": [0, 1, 5, 8],
     "examples": ["proxyctl fix"]},
    {"name": "recover", "group": "maintenance",
     "summary": "切网后软恢复（清 DNS 缓存 + 重测代理组，不重启）",
     "args": [], "supports_json": False, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 1, 2, 5],
     "examples": ["proxyctl recover"]},
    # daemon / dns-lock
    {"name": "daemon", "group": "daemon",
     "summary": "管理 extra_daemons（如 claude-proxy）",
     "args": [{"name": "name", "required": False},
              {"name": "subcmd", "required": False,
               "choices": ["start", "stop", "restart", "status", "log"]}],
     "supports_json": False, "side_effects": "process",
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1, 3, 4, 8],
     "examples": ["proxyctl daemon", "proxyctl daemon claude-proxy status"]},
    {"name": "claude-proxy", "group": "daemon",
     "summary": "daemon claude-proxy 的别名（向后兼容）",
     "args": [{"name": "subcmd", "required": False}],
     "supports_json": False, "side_effects": "process",
     "needs_sudo": True, "interactive": False,
     "exit_codes": [0, 1, 3, 4, 8],
     "examples": ["proxyctl claude-proxy status"]},
    {"name": "dns-lock", "group": "daemon", "summary": "启动 DNS 看门狗",
     "args": [], "supports_json": False, "side_effects": "process",
     "needs_sudo": True, "interactive": False, "exit_codes": [0, 1, 4],
     "examples": ["proxyctl dns-lock", "proxyctl dns-lock --reload"]},
    {"name": "dns-unlock", "group": "daemon", "summary": "停止 DNS 看门狗",
     "args": [], "supports_json": False, "side_effects": "process",
     "needs_sudo": True, "interactive": False, "exit_codes": [0, 1],
     "examples": ["proxyctl dns-unlock"]},
    # tools / agent
    {"name": "env", "group": "tool", "summary": "输出代理环境变量（可 eval）",
     "args": [], "supports_json": False, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0],
     "examples": ["proxyctl env", "proxyctl env --unset"]},
    {"name": "log", "group": "tool",
     "summary": "查看后端日志：默认 tail -f；支持 --tail N / --no-follow / --json",
     "args": [], "supports_json": True, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 3],
     "examples": ["proxyctl log", "proxyctl log --tail 50 --no-follow"]},
    {"name": "plugins", "group": "tool", "summary": "显示已加载插件",
     "args": [], "supports_json": True, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0],
     "examples": ["proxyctl plugins", "proxyctl plugins --json"]},
    # agent-only
    {"name": "explain", "group": "agent",
     "summary": "解释 topic：rules / nodes / config / dns / engine / 等",
     "args": [{"name": "topic", "required": False,
               "choices": sorted(TOPICS.keys())}],
     "supports_json": True, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 2],
     "examples": ["proxyctl explain", "proxyctl explain rules --json"]},
    {"name": "agent-guide", "group": "agent",
     "summary": "给 LLM Agent 的入门 markdown（含能力边界、退出码、决策树）",
     "args": [], "supports_json": True, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0],
     "examples": ["proxyctl agent-guide", "proxyctl agent-guide --json"]},
    {"name": "commands", "group": "agent",
     "summary": "所有命令的元数据（含 side_effects / needs_sudo / exit_codes）",
     "args": [], "supports_json": True, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0],
     "examples": ["proxyctl commands --json"]},
    {"name": "config", "group": "agent",
     "summary": "proxyctl 自身配置：path | get <key>",
     "args": [{"name": "subcmd", "choices": ["path", "get"], "required": True},
              {"name": "key", "required": False}],
     "supports_json": True, "side_effects": "none",
     "needs_sudo": False, "interactive": False, "exit_codes": [0, 2, 3, 6],
     "examples": ["proxyctl config path", "proxyctl config get proxy_port"]},
]


def cmd_commands(args: list, backend, config) -> None:
    """proxyctl commands [--json]"""
    from proxyctl.cli import VERSION
    as_json = GLOBAL_FLAGS_REF().get("json", False)
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
            if c["side_effects"] != "none": flags.append(c["side_effects"])
            tag = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  {CYAN}{c['name']:<14}{NC} {c['summary']}{DIM}{tag}{NC}")
        print()
    print(f"{DIM}详细：proxyctl commands --json{NC}")


# ── 主入口 4: config ──────────────────────────────────────────────────────

def cmd_config(args: list, backend, config) -> None:
    """proxyctl config path | get <dot.key>"""
    as_json = GLOBAL_FLAGS_REF().get("json", False)
    if not args:
        fail("缺少子命令", hint="用法：proxyctl config path | proxyctl config get <key>",
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

    fail(f"未知子命令：{sub}",
         hint="用法：proxyctl config path | proxyctl config get <key>",
         code=USAGE, cmd="config", as_json=as_json)


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
    """proxyctl doctor [--json] — 极简健康打分。"""
    as_json = GLOBAL_FLAGS_REF().get("json", False)

    # 从 cli.py 复用 service_running，但为避免循环 import 在这里独立判断
    from proxyctl.cli import service_running
    port = config.get("proxy_port", 7890)
    api_base = config.get("api_base", "http://127.0.0.1:9090")

    engine_up = bool(service_running(backend))
    port_listen = _tcp_open("127.0.0.1", port)
    dns_ok = _dns_points_to_loopback()
    system_proxy_ok = _system_proxy_points_to_loopback(port)
    connectivity_ok = _quick_connectivity(api_base, port) if engine_up else False

    flags = {
        "engine_up": engine_up,
        "port_listen": port_listen,
        "dns_ok": dns_ok,
        "system_proxy_ok": system_proxy_ok,
        "connectivity_ok": connectivity_ok,
    }
    score = sum(1 for v in flags.values() if v)
    hint = _doctor_hint(flags)
    data = {**flags, "score": score, "max": len(flags), "hint": hint}
    healthy = (score == len(flags))
    code = OK if healthy else ENGINE_DOWN

    if as_json:
        emit_json(envelope("doctor", data=data, ok=healthy,
                           code=code, hint=hint))
        sys.exit(code)

    icon = lambda b: f"{GREEN}✓{NC}" if b else f"{RED}✗{NC}"
    print(f"{BOLD}proxyctl doctor{NC}  ({score}/{len(flags)})")
    print(f"  {icon(engine_up)}  engine_up        ({backend.name} 服务运行中)")
    print(f"  {icon(port_listen)}  port_listen      (127.0.0.1:{port})")
    print(f"  {icon(dns_ok)}  dns_ok           (系统 DNS 含 127.0.0.1)")
    print(f"  {icon(system_proxy_ok)}  system_proxy_ok  (macOS HTTP/HTTPS proxy)")
    print(f"  {icon(connectivity_ok)}  connectivity_ok  (https://www.google.com via proxy)")
    if hint:
        print(f"{CYAN}next:{NC} {hint}")
    sys.exit(code)


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
    _GLOBAL_FLAGS.clear()
    _GLOBAL_FLAGS.update(flags)
