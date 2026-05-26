"""Human renderer for ``proxyctl connections``."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

from proxyctl import _io
from proxyctl._io import maybe_disable_module_colors
from proxyctl.connections_filters import MATCH_DIMENSIONS

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
CYAN = "\033[0;36m"
NC = "\033[0m"

# Highlight wrapper for matched keyword spans (bold + yellow bg + black fg).
# Defined as module-level constants so set_no_color() can blank them via
# _patch_module_colors after we register them in _io._COLOR_NAMES.
HL_START = "\033[1;43;30m"
HL_END = "\033[0m"

maybe_disable_module_colors(__name__)


def _highlight_keywords(text: str, keywords: list[str]) -> str:
    """Wrap each (case-insensitive) keyword occurrence in ANSI highlight.

    The implementation finds all match spans, merges overlapping ones, then
    rebuilds the string in one pass. This avoids the bug where a previous
    keyword's injected ANSI sequence gets matched by a later digit keyword
    (e.g. ``43`` matching the ``\\033[...43;30m`` we just wrote).

    Returns ``text`` unchanged when colors are off, ``keywords`` is empty,
    or no keyword hits.
    """
    if not keywords or not text or not _io.should_color():
        return text
    spans: list[tuple[int, int]] = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
            spans.append((m.start(), m.end()))
    if not spans:
        return text
    spans.sort()
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        parts.append(text[cursor:start])
        parts.append(f"{HL_START}{text[start:end]}{HL_END}")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _hl(value: Any, keywords: list[str]) -> str:
    """Stringify ``value`` and apply keyword highlight (no-op when empty)."""
    return _highlight_keywords(str(value) if value is not None else "", keywords)


def _report_keywords(report: dict[str, Any]) -> list[str]:
    """Return the positional/--query keywords carried by this report."""
    filters = report.get("filters") or {}
    raw = filters.get("query") or []
    return [str(kw) for kw in raw if str(kw).strip()]


def _format_bytes(value: Any) -> str:
    """Render a byte count with binary (1024) units. Falls back to ``-``.

    Examples:
        >>> _format_bytes(0)        # '0 B'
        >>> _format_bytes(8228)     # '8.0 KiB'
        >>> _format_bytes(71126)    # '69.5 KiB'
        >>> _format_bytes(1572864)  # '1.5 MiB'
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "-"
    if n < 0:
        return "-"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    size = float(n)
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{n} {units[0]}"
    return f"{size:.1f} {units[idx]}"


def _format_start(start_iso: Any) -> str:
    """Render an ISO timestamp followed by a "X 前" relative tag.

    Returns ``-`` when ``start_iso`` is empty/missing; returns the raw
    timestamp alone when parsing fails (so we never hide the data).
    """
    if not start_iso:
        return "-"
    rel = _format_duration_since(start_iso)
    return f"{start_iso} ({rel})" if rel else str(start_iso)


def _format_duration_since(start_iso: Any,
                           now: datetime | None = None) -> str:
    """Render how long ago an ISO-8601 timestamp was. Falls back to ``""``.

    The mihomo API returns timestamps such as
    ``2026-05-23T17:20:27.854916+08:00``. ``datetime.fromisoformat`` since
    Python 3.11 handles this format directly; on parse failure we return an
    empty string so the caller can omit the parenthetical.
    """
    if not start_iso or not isinstance(start_iso, str):
        return ""
    try:
        started = datetime.fromisoformat(start_iso)
    except ValueError:
        return ""
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    delta = current - started
    secs = int(delta.total_seconds())
    if secs < 0:
        return ""
    if secs < 60:
        return f"{secs}s 前"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s 前"
    if secs < 86400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h{m:02d}m 前"
    days = secs // 86400
    h = (secs % 86400) // 3600
    return f"{days}d{h:02d}h 前"

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
    keywords = _report_keywords(report)
    verbose = bool(report.get("verbose"))
    _emit_header(report)
    if verbose:
        _emit_history_status_hint(report.get("history_status") or {})
    if _should_stop_after_header(report, keywords):
        return
    if _has_narrowing_filters(report):
        _emit_local_connections(report, keywords, verbose)
    _emit_proxy_owner_explain_human(report)
    _emit_proxy_owner_groups_human(report, keywords, verbose)


def _emit_history_status_hint(status: dict[str, Any]) -> None:
    """Tell the user once whether --verbose has any historical data to show."""
    if not status.get("loaded"):
        return
    count = status.get("event_count") or 0
    if count == 0:
        print(f"  {DIM}（--verbose 历史: traffic-events.ndjson "
              f"{'为空' if status.get('exists') else '不存在'}；"
              f"先跑 `proxyctl traffic watch --interval 5` 采样后再回来看历史）{NC}")
        return
    print(f"  {DIM}（--verbose 历史: 读到 {count} 条采样事件 "
          f"from {status.get('events_path')}）{NC}")


def _emit_header(report: dict[str, Any]) -> None:
    """Render report header and API degradation status."""
    api = report["api"]
    summary = report["summary"]
    all_proxy = "是" if summary["all_via_proxy_port"] else "否"
    print(f"{BOLD}proxyctl connections{NC}  backend={report['backend']} "
          f"代理端口={report['proxy_port']} 全部经代理端口={all_proxy}")
    if not api.get("ok"):
        print(f"  {YELLOW}降级：{NC}{api.get('error')}")


def _should_stop_after_header(report: dict[str, Any],
                              keywords: list[str]) -> bool:
    """Return whether there is no connection data to render."""
    if report["connections"] or report["proxy_owner_connections"]:
        return False
    print(f"  {DIM}没有匹配过滤条件的本机连接{NC}")
    _emit_zero_match_hint(keywords)
    return True


def _emit_zero_match_hint(keywords: list[str]) -> None:
    """When keyword filters were active but matched nothing, list what was tried."""
    if not keywords:
        return
    kw_str = " ".join(keywords)
    dims_str = ", ".join(MATCH_DIMENSIONS)
    print(f"  {DIM}匹配关键字: {kw_str}{NC}")
    print(f"  {DIM}尝试过的维度: {dims_str}{NC}")
    print(f"  {DIM}提示: 数字关键字精确比较端口/PID，文本关键字子串匹配 host/进程/命令行{NC}")


def _has_narrowing_filters(report: dict[str, Any]) -> bool:
    """Return whether the command used explicit filters."""
    filters = report.get("filters") or {}
    return any(bool(filters.get(name)) for name in (
        "app", "host", "chain", "route", "preset", "agent", "query"
    ))


def _emit_local_connections(report: dict[str, Any],
                            keywords: list[str],
                            verbose: bool = False) -> None:
    """Render app-owned local socket rows."""
    if not report["connections"]:
        if not report["proxy_owner_connections"]:
            print(f"  {DIM}没有匹配过滤条件的 App 直连 socket{NC}")
            _emit_zero_match_hint(keywords)
        return
    for item in report["connections"]:
        status = f"{GREEN}已关联{NC}" if item["matched"] else f"{YELLOW}未关联{NC}"
        target_raw = "代理" if item["connects_proxy_port"] else (item["target_host"] or "")
        target = _hl(target_raw, keywords) if target_raw != "代理" else target_raw
        contexts = _format_contexts(item.get("app_contexts") or [])
        app = _hl(item["app"], keywords)
        pid = _hl(item["pid"], keywords)
        src_port = _hl(item["local_source_port"], keywords)
        dst_port = _hl(item["target_port"], keywords)
        print(f"  {CYAN}{app}{NC} pid={pid} fd={item['fd']} "
              f"源端口={src_port} -> {target}:{dst_port} "
              f"上下文={contexts} {status}")
        _emit_local_connection_detail(item, keywords, verbose)


def _emit_local_connection_detail(item: dict[str, Any],
                                  keywords: list[str],
                                  verbose: bool = False) -> None:
    """Render one local app-owned connection detail line."""
    if not item["matched"]:
        print(f"    原因={_unmatched_reason_label(item['unmatched_reason'])}")
        return
    m = item["mihomo"] or {}
    dest_raw = m.get("host") or m.get("destination_ip") or "?"
    dest = _hl(dest_raw, keywords) if dest_raw != "?" else "?"
    print(f"    目的={dest} 规则={m.get('rule') or '?'} "
          f"规则载荷={m.get('rule_payload') or '-'} "
          f"链路={','.join(m.get('chains') or []) or '-'} "
          f"上传={_format_bytes(m.get('upload'))} "
          f"下载={_format_bytes(m.get('download'))} "
          f"开始={_format_start(m.get('start'))}")
    if verbose:
        _emit_verbose_process_and_reasons(item, keywords, indent="    ")


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


def _emit_proxy_owner_groups_human(report: dict[str, Any],
                                   keywords: list[str],
                                   verbose: bool = False) -> None:
    """Render grouped proxy-owned route summary."""
    groups = report["proxy_owner_groups"]
    if not groups:
        return
    print(f"  {BOLD}目的站点汇总{NC}")
    for group in groups:
        _emit_destination_group(
            group, report["proxy_owner_connections"], keywords, verbose)


def _emit_destination_group(group: dict[str, Any],
                            owner_rows: list[dict[str, Any]],
                            keywords: list[str],
                            verbose: bool = False) -> None:
    """Render one destination summary group."""
    marker = f"{YELLOW}警告{NC}" if group["warning"] else f"{GREEN}正常{NC}"
    route = ",".join(_route_label(item) for item in group["route_kinds"]) or "未知"
    first_variant = (group["chain_variants"] or [{"chains": []}])[0]
    chains = _format_chain(first_variant["chains"])
    contexts = _format_contexts(group.get("contexts") or [])
    context_label = _context_label(bool(group.get("candidate_contexts")))
    key_disp = _hl(group["key"], keywords)
    print(f"  {marker} {CYAN}{key_disp}{NC}  "
          f"{group['connection_count']} 条  路由={route}")
    print(f"    {context_label}: {contexts}")
    print(f"    链路: {chains}")
    samples = ",".join(_hl(port, keywords) for port in group.get("sample_source_ports", []))
    if group["hosts"]:
        hosts_disp = ", ".join(_hl(h, keywords) for h in group["hosts"][:6])
        print(f"    主机: {hosts_disp}")
    if samples:
        print(f"    源端口样例: {samples}")
    owners = _owner_count_lines(group, owner_rows, keywords)
    if owners:
        print("    持有进程:")
        for owner_line in owners:
            print(f"      {owner_line}")
    if group["warning"]:
        print(f"    告警={_warning_label(group['warning'])} "
              f"链路变体数={len(group['chain_variants'])}")
    if verbose:
        _emit_group_socket_details(group, owner_rows, keywords)


def _emit_group_socket_details(group: dict[str, Any],
                               owner_rows: list[dict[str, Any]],
                               keywords: list[str]) -> None:
    """Render per-socket detail for the rows belonging to one group."""
    group_key = (group["key_type"], group["key"])
    members = [row for row in owner_rows if _row_group_key(row) == group_key]
    if not members:
        return
    print(f"    {DIM}socket 明细：{NC}")
    for idx, item in enumerate(members, start=1):
        _emit_proxy_owner_socket_detail(item, keywords, idx)


def _emit_proxy_owner_socket_detail(item: dict[str, Any],
                                    keywords: list[str], idx: int) -> None:
    """Render one proxy-owner socket's full detail (verbose mode)."""
    owner = item.get("owner") or {}
    m = item.get("mihomo") or {}
    app = _hl(owner.get("app") or "?", keywords)
    pid = _hl(owner.get("pid"), keywords)
    src_port = _hl(item.get("local_source_port"), keywords)
    dst_port = _hl(item.get("target_port"), keywords)
    print(f"      [{idx}] {CYAN}{app}{NC}(pid={pid}) "
          f"源端口={src_port} -> 代理端口:{dst_port}")
    process = str(owner.get("process") or "")
    command = str(owner.get("command") or "")
    if process:
        print(f"          进程={_hl(process, keywords)}")
    if command and command.strip() != process.strip():
        print(f"          命令行={_hl(command, keywords)}")
    if m:
        host_disp = _hl(m.get("host") or "-", keywords)
        ip_disp = _hl(m.get("destination_ip") or "-", keywords)
        chains = _format_chain(m.get("chains") or [])
        print(f"          目的={host_disp} ip={ip_disp} "
              f"规则={m.get('rule') or '-'} "
              f"载荷={m.get('rule_payload') or '-'}")
        print(f"          链路={chains} "
              f"路由={_route_label(m.get('route_kind'))}")
        print(f"          上传={_format_bytes(m.get('upload'))} "
              f"下载={_format_bytes(m.get('download'))} "
              f"开始={_format_start(m.get('start'))}")
    reasons = item.get("match_reasons")
    if reasons:
        reason_str = ", ".join(
            f"{kw}={'+'.join(dims)}" for kw, dims in reasons.items())
        print(f"          {DIM}命中: {reason_str}{NC}")
    _emit_history_block(item, indent="          ")


def _emit_verbose_process_and_reasons(item: dict[str, Any],
                                      keywords: list[str],
                                      indent: str) -> None:
    """Append process/command/match_reasons for an item under verbose mode."""
    process = str(item.get("process") or "")
    command = str(item.get("command") or "")
    if process:
        print(f"{indent}进程={_hl(process, keywords)}")
    if command and command.strip() != process.strip():
        print(f"{indent}命令行={_hl(command, keywords)}")
    reasons = item.get("match_reasons")
    if reasons:
        reason_str = ", ".join(
            f"{kw}={'+'.join(dims)}" for kw, dims in reasons.items())
        print(f"{indent}{DIM}命中: {reason_str}{NC}")
    _emit_history_block(item, indent=indent)


def _emit_history_block(item: dict[str, Any], indent: str) -> None:
    """Render the historical traffic block attached by _attach_history_to_rows.

    No-ops when the item has no ``history`` key (either because verbose was
    off, or because the traffic store had no events for this host).
    """
    history = item.get("history")
    if not history:
        return
    upload = _format_bytes(history.get("upload_total"))
    download = _format_bytes(history.get("download_total"))
    events = history.get("event_count") or 0
    connections = history.get("connection_count") or 0
    print(f"{indent}{DIM}历史: 累计 上传={upload} 下载={download} "
          f"采样事件={events} 历史连接={connections}{NC}")
    first_seen = history.get("first_seen") or ""
    last_seen = history.get("last_seen") or ""
    if first_seen or last_seen:
        first_disp = _format_start(first_seen) if first_seen else "-"
        last_disp = _format_start(last_seen) if last_seen else "-"
        print(f"{indent}{DIM}      首见={first_disp}{NC}")
        print(f"{indent}{DIM}      末见={last_disp}{NC}")
    apps = history.get("owner_apps") or []
    if apps:
        print(f"{indent}{DIM}      涉及进程: {', '.join(apps)}{NC}")


def _owner_count_lines(group: dict[str, Any],
                       rows: list[dict[str, Any]],
                       keywords: list[str]) -> list[str]:
    """Return owner process count lines for one destination group."""
    counts: dict[tuple[str, Any], int] = {}
    for row in rows:
        if _row_group_key(row) != (group["key_type"], group["key"]):
            continue
        owner = row.get("owner") or {}
        key = (_owner_display_name(owner), owner.get("pid"))
        counts[key] = counts.get(key, 0) + 1
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [f"{_format_owner_label(key, keywords)}  {count} 条" for key, count in items]


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


def _format_owner_label(key: tuple[str, Any], keywords: list[str]) -> str:
    """Format an owner process key for human output."""
    app, pid = key
    app_disp = _hl(app, keywords)
    if pid is None:
        return app_disp
    pid_disp = _hl(pid, keywords)
    return f"{app_disp}(pid={pid_disp})"


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
