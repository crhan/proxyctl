"""Human renderer for ``proxyctl connections``."""

from __future__ import annotations

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
ATTRIBUTION_LABELS = {
    "owner_matches_app_filter": "持有进程匹配过滤条件",
    "system_extension_owner_original_app_hidden": "系统扩展持有，原始 App 被隐藏",
    "owner_does_not_match_app_filter": "持有进程不匹配过滤条件",
    "visible_owner": "可见持有进程",
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


def _attribution_label(value: str | None) -> str:
    """Return a Chinese human label for owner attribution."""
    return ATTRIBUTION_LABELS.get(value or "", value or "未知")


def _warning_label(value: str | None) -> str:
    """Return a Chinese human label for group route warnings."""
    return WARNING_LABELS.get(value or "", value or "未知")


def _format_contexts(contexts: list[str]) -> str:
    """Format app contexts for human output."""
    return ",".join(APP_CONTEXT_LABELS.get(item, item) for item in contexts) or "-"


def _format_counts(counts: dict[str, int]) -> str:
    """Format a small count map in stable human-readable order."""
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ",".join(f"{key}:{value}" for key, value in items) or "-"


def emit_human(report: dict[str, Any]) -> None:
    """Render a compact human-readable connections report."""
    _emit_header(report)
    if _should_stop_after_header(report):
        return
    _emit_local_connections(report)
    _emit_proxy_owner_explain_human(report)
    _emit_proxy_owner_groups_human(report)
    _emit_proxy_owner_human(report)


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


def _emit_local_connections(report: dict[str, Any]) -> None:
    """Render app-owned local socket rows."""
    if not report["connections"]:
        print(f"  {DIM}没有匹配过滤条件的 App 直连 socket{NC}")
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
    has_system_extension = any(
        (item.get("owner") or {}).get("system_extension_owner") for item in rows
    )
    print(f"  {DIM}说明：下面是同一批连接的两个视图：先按目的站点汇总，"
          f"再把逐条连接折叠成同类连接组。{NC}")
    print(f"  {DIM}说明：每条连接链路是 本机入口 -> 代理端口 -> Mihomo -> 目的站点；"
          f"路由和链路来自 Mihomo。{NC}")
    if has_candidates or has_system_extension:
        print(f"  {DIM}说明：com.antgroup.asp 这类系统扩展会隐藏原始 App；"
              f"候选只按目的域名推断，不等于确认 App。{NC}")


def _emit_proxy_owner_groups_human(report: dict[str, Any]) -> None:
    """Render grouped proxy-owned route summary."""
    groups = report["proxy_owner_groups"]
    if not groups:
        return
    print(f"  {BOLD}目的站点汇总（同一批连接的聚合视图）{NC}")
    for group in groups:
        _emit_destination_group(group)


def _emit_destination_group(group: dict[str, Any]) -> None:
    """Render one destination summary group."""
    marker = f"{YELLOW}警告{NC}" if group["warning"] else f"{GREEN}正常{NC}"
    route = ",".join(_route_label(item) for item in group["route_kinds"]) or "未知"
    first_variant = (group["chain_variants"] or [{"chains": []}])[0]
    chains = ",".join(first_variant["chains"]) or "-"
    contexts = _format_contexts(group.get("contexts") or [])
    context_label = _context_label(bool(group.get("candidate_contexts")))
    print(f"  {marker} {CYAN}{group['key']}{NC} "
          f"数量={group['connection_count']} {context_label}={contexts} "
          f"路由={route} 链路={chains}")
    if group["hosts"]:
        print(f"    主机={','.join(group['hosts'][:6])}")
    if group["warning"]:
        print(f"    告警={_warning_label(group['warning'])} "
              f"链路变体数={len(group['chain_variants'])}")


def _emit_proxy_owner_human(report: dict[str, Any]) -> None:
    """Render folded reverse-owned proxy connection groups."""
    groups = _fold_proxy_owner_rows(report["proxy_owner_connections"])
    if not groups:
        return
    print(f"  {BOLD}同类连接组（由逐条连接明细折叠）{NC}")
    for group in groups[:12]:
        _emit_folded_proxy_owner_group(report, group)
    if len(groups) > 12:
        print(f"  {DIM}... 已省略 {len(groups) - 12} 个同类连接组；"
              f"JSON 中仍保留逐条 proxy_owner_connections{NC}")


def _fold_proxy_owner_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold equivalent proxy-owner rows for human output."""
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = _proxy_owner_fold_key(row)
        if key not in groups:
            groups[key] = _new_folded_group(row)
        _add_folded_row(groups[key], row)
    return sorted(groups.values(), key=lambda item: (-item["count"], item["dest"]))


def _proxy_owner_fold_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Return the human-output fold key for one proxy-owner row."""
    owner = row["owner"]
    m = row.get("mihomo") or {}
    contexts = tuple(row.get("candidate_contexts") or owner.get("app_contexts") or [])
    return (
        owner.get("app"), owner.get("pid"), contexts, row.get("attribution"),
        m.get("host") or m.get("destination_ip") or "?",
        m.get("rule") or "?", m.get("rule_payload") or "-",
        m.get("route_kind") or "unknown", tuple(m.get("chains") or []),
    )


def _new_folded_group(row: dict[str, Any]) -> dict[str, Any]:
    """Create an empty folded group seeded from one row."""
    owner = row["owner"]
    m = row.get("mihomo") or {}
    return {
        "owner": owner,
        "contexts": row.get("candidate_contexts") or owner.get("app_contexts") or [],
        "has_candidates": bool(row.get("candidate_contexts")),
        "dest": m.get("host") or m.get("destination_ip") or "?",
        "rule": m.get("rule") or "?",
        "rule_payload": m.get("rule_payload") or "-",
        "route_kind": m.get("route_kind") or "unknown",
        "chains": m.get("chains") or [],
        "attribution": row.get("attribution"),
        "count": 0,
        "source_ports": [],
        "state_counts": {},
    }


def _add_folded_row(group: dict[str, Any], row: dict[str, Any]) -> None:
    """Add one proxy-owner row to an existing folded group."""
    group["count"] += 1
    group["source_ports"].append(row["local_source_port"])
    state = (row.get("owner") or {}).get("state") or "UNKNOWN"
    group["state_counts"][state] = group["state_counts"].get(state, 0) + 1


def _emit_folded_proxy_owner_group(report: dict[str, Any],
                                   group: dict[str, Any]) -> None:
    """Render one folded proxy-owner group."""
    owner = group["owner"]
    ports = ",".join(str(port) for port in group["source_ports"][:6])
    if len(group["source_ports"]) > 6:
        ports += ",..."
    context_label = _context_label(group["has_candidates"])
    context_text = _format_contexts(group["contexts"])
    print(f"  入口进程={CYAN}{owner['app']}{NC} pid={owner['pid']} "
          f"数量={group['count']} 源端口={ports} -> 代理:{report['proxy_port']} "
          f"状态={_format_counts(group['state_counts'])} "
          f"{context_label}={context_text} 路由={_route_label(group['route_kind'])}")
    print(f"    目的={group['dest']} 规则={group['rule']} "
          f"规则载荷={group['rule_payload']} "
          f"链路={','.join(group['chains']) or '-'} "
          f"归因={_attribution_label(group['attribution'])}")
