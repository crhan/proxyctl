"""Human renderer for ``proxyctl connections``."""

from __future__ import annotations

import os
from typing import Any

from proxyctl._io import maybe_disable_module_colors

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
CYAN = "\033[0;36m"
NC = "\033[0m"

maybe_disable_module_colors(__name__)

APP_CONTEXT_LABELS = {
    "codex_app": "Codex App",
    "codex_cli": "Codex CLI",
    "claude_app": "Claude App",
    "claude_cli": "Claude CLI",
    "chatgpt_app": "ChatGPT App",
}
ROUTE_LABELS = {
    "proxy": "代理",
    "direct": "直连",
    "reject": "拒绝",
    "unknown": "未知",
}
UNMATCHED_REASON_LABELS = {
    "not_proxyctl_proxy_port": "没有连到 proxyctl 代理端口",
    "backend_not_mihomo": "当前后端不是 mihomo",
    "mihomo_api_unavailable": "mihomo API 不可用",
    "no_mihomo_source_port_match": "未在 mihomo /connections 找到相同源端口",
}
WARNING_LABELS = {
    "mixed_chains": "同一目的地使用了不同代理链路",
    "mixed_route_kind": "同一目的地混用了代理/直连/拒绝路由",
}


def _route_label(route_kind: str | None) -> str:
    """Return a Chinese human label for a route kind."""
    return ROUTE_LABELS.get(route_kind or "unknown", route_kind or "未知")


def _context_label(has_candidates: bool) -> str:
    """Return the Chinese label for confirmed or inferred app contexts."""
    return "候选" if has_candidates else "上下文"


def _unmatched_reason_label(reason: str | None) -> str:
    """Return a Chinese human label for an unmatched reason."""
    return UNMATCHED_REASON_LABELS.get(reason or "", reason or "未知")


def _warning_label(value: str | None) -> str:
    """Return a Chinese human label for group route warnings."""
    return WARNING_LABELS.get(value or "", value or "未知")


def _format_contexts(contexts: list[str]) -> str:
    """Format app contexts for human output."""
    return ", ".join(APP_CONTEXT_LABELS.get(item, item) for item in contexts) or "-"


def _format_chain(chains: list[Any]) -> str:
    """Format a proxy chain for human output."""
    return " -> ".join(str(item) for item in chains) or "-"


def emit_human(report: dict[str, Any]) -> None:
    """Render a compact human-readable connections report."""
    _emit_header(report)
    if _should_stop_after_header(report):
        return
    if _has_narrowing_filters(report):
        _emit_local_connections(report)
    _emit_proxy_owner_explain_human(report)
    _emit_proxy_owner_groups_human(report)


def _emit_header(report: dict[str, Any]) -> None:
    """Render report header and API degradation status."""
    api = report["api"]
    summary = report["summary"]
    all_proxy = "是" if summary["all_via_proxy_port"] else "否"
    print(f"{BOLD}proxyctl connections{NC}  backend={report['backend']} "
          f"代理端口={report['proxy_port']} 全部经代理端口={all_proxy}")
    if not api.get("ok"):
        print(f"  {YELLOW}降级：{NC}{api.get('error')}")


def _should_stop_after_header(report: dict[str, Any]) -> bool:
    """Return whether there is no connection data to render."""
    if report["connections"] or report["proxy_owner_connections"]:
        return False
    print(f"  {DIM}没有匹配过滤条件的本机连接{NC}")
    return True


def _has_narrowing_filters(report: dict[str, Any]) -> bool:
    """Return whether the command used explicit filters."""
    filters = report.get("filters") or {}
    return any(bool(filters.get(name)) for name in (
        "app", "host", "preset", "agent", "query"
    ))


def _emit_local_connections(report: dict[str, Any]) -> None:
    """Render app-owned local socket rows."""
    if not report["connections"]:
        if not report["proxy_owner_connections"]:
            print(f"  {DIM}没有匹配过滤条件的 App 直连 socket{NC}")
        return
    for item in report["connections"]:
        status = f"{GREEN}已关联{NC}" if item["matched"] else f"{YELLOW}未关联{NC}"
        target = "代理" if item["connects_proxy_port"] else item["target_host"]
        contexts = _format_contexts(item.get("app_contexts") or [])
        print(f"  {CYAN}{item['app']}{NC} pid={item['pid']} fd={item['fd']} "
              f"源端口={item['local_source_port']} -> {target}:{item['target_port']} "
              f"上下文={contexts} {status}")
        _emit_local_connection_detail(item)


def _emit_local_connection_detail(item: dict[str, Any]) -> None:
    """Render one local app-owned connection detail line."""
    if not item["matched"]:
        print(f"    原因={_unmatched_reason_label(item['unmatched_reason'])}")
        return
    m = item["mihomo"] or {}
    dest = m.get("host") or m.get("destination_ip") or "?"
    print(f"    目的={dest} 规则={m.get('rule') or '?'} "
          f"规则载荷={m.get('rule_payload') or '-'} "
          f"链路={','.join(m.get('chains') or []) or '-'} "
          f"上传={m.get('upload')} 下载={m.get('download')} "
          f"开始={m.get('start') or '-'}")


def _emit_proxy_owner_explain_human(report: dict[str, Any]) -> None:
    """Render short notes that explain proxy-owner attribution."""
    rows = report["proxy_owner_connections"]
    if not rows:
        return
    has_candidates = any(item.get("candidate_contexts") for item in rows)
    print(f"  {DIM}说明：默认按目的站点汇总，并在每个目的站点下统计持有进程；"
          f"逐条 socket 明细保留在 --json 的 proxy_owner_connections[]。{NC}")
    print(f"  {DIM}说明：路由和链路来自 Mihomo，表示目的站点最终走直连还是代理。{NC}")
    if has_candidates:
        print(f"  {DIM}说明：持有进程只是本机 socket owner；系统扩展也按普通进程统计。"
              f"候选只按目的域名推断，不等于确认 App。{NC}")


def _emit_proxy_owner_groups_human(report: dict[str, Any]) -> None:
    """Render grouped proxy-owned route summary."""
    groups = report["proxy_owner_groups"]
    if not groups:
        return
    print(f"  {BOLD}目的站点汇总{NC}")
    for group in groups:
        _emit_destination_group(group, report["proxy_owner_connections"])


def _emit_destination_group(group: dict[str, Any],
                            owner_rows: list[dict[str, Any]]) -> None:
    """Render one destination summary group."""
    marker = f"{YELLOW}警告{NC}" if group["warning"] else f"{GREEN}正常{NC}"
    route = ",".join(_route_label(item) for item in group["route_kinds"]) or "未知"
    first_variant = (group["chain_variants"] or [{"chains": []}])[0]
    chains = _format_chain(first_variant["chains"])
    contexts = _format_contexts(group.get("contexts") or [])
    context_label = _context_label(bool(group.get("candidate_contexts")))
    print(f"  {marker} {CYAN}{group['key']}{NC}  "
          f"{group['connection_count']} 条  路由={route}")
    print(f"    {context_label}: {contexts}")
    print(f"    链路: {chains}")
    samples = ",".join(str(port) for port in group.get("sample_source_ports", []))
    if group["hosts"]:
        print(f"    主机: {', '.join(group['hosts'][:6])}")
    if samples:
        print(f"    源端口样例: {samples}")
    owners = _owner_count_lines(group, owner_rows)
    if owners:
        print("    持有进程:")
        for owner_line in owners:
            print(f"      {owner_line}")
    if group["warning"]:
        print(f"    告警={_warning_label(group['warning'])} "
              f"链路变体数={len(group['chain_variants'])}")


def _owner_count_lines(group: dict[str, Any],
                       rows: list[dict[str, Any]]) -> list[str]:
    """Return owner process count lines for one destination group."""
    counts: dict[tuple[str, Any], int] = {}
    for row in rows:
        if _row_group_key(row) != (group["key_type"], group["key"]):
            continue
        owner = row.get("owner") or {}
        key = (_owner_display_name(owner), owner.get("pid"))
        counts[key] = counts.get(key, 0) + 1
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [f"{_format_owner_label(key)}  {count} 条" for key, count in items]


def _row_group_key(row: dict[str, Any]) -> tuple[str, str]:
    """Return the destination grouping key used by proxy_owner_groups."""
    m = row.get("mihomo") or {}
    if m.get("rule_payload"):
        return "rule_payload", str(m["rule_payload"])
    if m.get("host"):
        return "host", str(m["host"])
    if m.get("destination_ip"):
        return "destination_ip", str(m["destination_ip"])
    return "unknown", "unknown"


def _format_owner_label(key: tuple[str, Any]) -> str:
    """Format an owner process key for human output."""
    app, pid = key
    if pid is None:
        return app
    return f"{app}(pid={pid})"


def _owner_display_name(owner: dict[str, Any]) -> str:
    """Return the clearest available process name for an owner row."""
    app = str(owner.get("app") or "")
    if app and app != "?":
        return app
    process = str(owner.get("process") or "")
    if process:
        return os.path.basename(process)
    command = str(owner.get("command") or "").split()
    if command:
        return os.path.basename(command[0])
    return "未知进程"
