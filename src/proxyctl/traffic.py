"""proxyctl traffic — active Mihomo traffic snapshot.

This command is intentionally read-only. It reports only currently active
Mihomo connections because the Clash-compatible ``/connections`` API does not
retain closed-connection history.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from proxyctl import _io
from proxyctl.connections import (
    ConnectionArgs,
    LocalConnection,
    ProxyOwner,
    _connection_source_port,
    _is_system_extension_owner,
    _mihomo_detail,
    collect_lsof_connections,
    collect_netstat_proxy_owners,
    fetch_mihomo_connections,
)
from proxyctl.connections_filters import (
    APP_CONTEXT_LABELS,
    PRESET_AGENTS,
    ROUTE_FILTER_ALIASES,
    _chain_text,
    _detect_app_contexts,
    _detail_text,
    _matches_filter_dimensions,
    _normalize_filter_value,
    _normalize_route_filter,
    _remote_candidate_contexts,
    _route_kind_text,
)

VALID_GROUPS = ("line", "chain", "app", "route")
FILTER_VALUE_FLAGS = {
    "--app",
    "--host",
    "--chain",
    "--line",
    "--route",
    "--preset",
    "--agent",
    "--query",
    "--filter",
}


@dataclass
class TrafficArgs:
    """Parsed arguments for ``proxyctl traffic``.

    Attributes:
        subcmd: Currently only ``snapshot`` is supported.
        group_by: Aggregation dimensions. Defaults to ``["line"]``.
        filters: Reused connection filters for host/line/route/agent narrowing.
    """

    subcmd: str = "snapshot"
    group_by: list[str] = field(default_factory=lambda: ["line"])
    filters: ConnectionArgs = field(default_factory=lambda: ConnectionArgs([]))


def parse_args(args: list[str]) -> TrafficArgs:
    """Parse ``proxyctl traffic`` arguments."""
    parsed = TrafficArgs()
    idx = 0
    if args and not args[0].startswith("-"):
        parsed.subcmd = args[0]
        idx = 1
    if parsed.subcmd != "snapshot":
        _io.fail(f"traffic 暂不支持子命令：{parsed.subcmd}",
                 hints=["proxyctl traffic",
                        "proxyctl traffic snapshot --by line,app"],
                 doc="agent-protocol", code=_io.USAGE, cmd="traffic")

    explicit_group = False
    while idx < len(args):
        arg = args[idx]
        if arg == "--by":
            if idx + 1 >= len(args):
                _io.fail("traffic --by 需要一个分组维度",
                         hint="proxyctl traffic --by line,app",
                         doc="agent-protocol", code=_io.USAGE, cmd="traffic")
            parsed.group_by = _parse_group_by(args[idx + 1], explicit_group,
                                              parsed.group_by)
            explicit_group = True
            idx += 2
            continue
        if arg == "--all":
            parsed.filters.all_apps = True
            idx += 1
            continue
        if arg in FILTER_VALUE_FLAGS:
            _append_filter_arg(parsed.filters, arg, args, idx)
            idx += 2
            continue
        _io.fail(f"未识别 traffic 参数：{arg}",
                 hints=["proxyctl traffic --by line,app",
                        "proxyctl traffic --chain residential-sg",
                        "proxyctl traffic --route proxy --preset ai"],
                 doc="agent-protocol", code=_io.USAGE, cmd="traffic")
    return parsed


def _parse_group_by(value: str, explicit: bool,
                    current: list[str]) -> list[str]:
    """Parse one ``--by`` value and preserve dimension order."""
    base = [] if not explicit else list(current)
    for raw in value.split(","):
        name = _normalize_filter_value(raw)
        if not name:
            continue
        if name not in VALID_GROUPS:
            known = ", ".join(VALID_GROUPS)
            _io.fail(f"未知 traffic 分组维度：{raw}",
                     hint=f"可用 --by: {known}",
                     doc="agent-protocol", code=_io.USAGE, cmd="traffic")
        if name not in base:
            base.append(name)
    return base or ["line"]


def _append_filter_arg(parsed: ConnectionArgs, arg: str,
                       args: list[str], idx: int) -> None:
    """Append one traffic filter argument."""
    if idx + 1 >= len(args):
        _io.fail(f"traffic {arg} 需要一个值",
                 hint="proxyctl traffic --chain residential-sg",
                 doc="agent-protocol", code=_io.USAGE, cmd="traffic")
    value = args[idx + 1]
    if arg == "--app":
        parsed.app_filters.append(value)
    elif arg == "--host":
        parsed.host_filters.append(value)
    elif arg in ("--chain", "--line"):
        parsed.chain_filters.append(value)
    elif arg == "--route":
        _validate_route(value)
        parsed.route_filters.append(value)
    elif arg == "--preset":
        _validate_preset(value)
        parsed.preset_filters.append(value)
    elif arg == "--agent":
        parsed.agent_filters.append(value)
    else:
        parsed.query_filters.append(value)


def _validate_preset(value: str) -> None:
    """Fail early when a preset name is unknown."""
    normalized = _normalize_filter_value(value)
    if normalized in PRESET_AGENTS:
        return
    known = ", ".join(sorted(PRESET_AGENTS))
    _io.fail(f"未知 traffic preset：{value}",
             hint=f"可用 preset: {known}",
             doc="agent-protocol", code=_io.USAGE, cmd="traffic")


def _validate_route(value: str) -> None:
    """Fail early when a route filter cannot match route_kind."""
    if _normalize_route_filter(value):
        return
    known = ", ".join(sorted(ROUTE_FILTER_ALIASES))
    _io.fail(f"未知 traffic route：{value}",
             hint=f"可用 route: {known}",
             doc="agent-protocol", code=_io.USAGE, cmd="traffic")


def build_snapshot(backend_name: str, config: dict[str, Any],
                   args: TrafficArgs) -> dict[str, Any]:
    """Build an active-connection traffic snapshot."""
    proxy_port = int(config.get("proxy_port", 7890))
    api_base = config.get("api_base", "http://127.0.0.1:9090")
    api_secret = config.get("api_secret", "")
    if backend_name != "mihomo":
        api_status = {
            "ok": False,
            "status": "skipped",
            "url": None,
            "error": f"traffic snapshot needs mihomo backend, current backend is {backend_name}",
            "count": 0,
        }
        rows: list[dict[str, Any]] = []
    else:
        remote_rows, api_status = fetch_mihomo_connections(api_base, api_secret)
        rows = _traffic_rows(remote_rows, proxy_port, args.filters)

    groups = _aggregate_rows(rows, args.group_by)
    return {
        "scope": "active_connections_snapshot",
        "scope_note": (
            "Only currently active Mihomo connections are counted; closed "
            "connections are not available unless a sampler records deltas."
        ),
        "backend": backend_name,
        "proxy_port": proxy_port,
        "group_by": args.group_by,
        "filters": {
            "app": args.filters.app_filters,
            "host": args.filters.host_filters,
            "chain": args.filters.chain_filters,
            "route": args.filters.route_filters,
            "preset": args.filters.preset_filters,
            "agent": args.filters.agent_filters,
            "query": args.filters.query_filters,
        },
        "api": api_status,
        "totals": _totals(rows),
        "groups": groups,
        "connections": rows,
    }


def _traffic_rows(remote_rows: list[dict[str, Any]], proxy_port: int,
                  filters: ConnectionArgs) -> list[dict[str, Any]]:
    """Convert Mihomo rows to traffic rows with best-effort attribution."""
    ports = {
        port for conn in remote_rows
        if (port := _connection_source_port(conn)) is not None
    }
    local_by_port = _local_rows_by_port(proxy_port)
    owner_by_port = collect_netstat_proxy_owners(proxy_port, ports)
    rows: list[dict[str, Any]] = []
    for conn in remote_rows:
        detail = _mihomo_detail(conn)
        if not detail:
            continue
        port = _connection_source_port(conn)
        local = local_by_port.get(port) if port is not None else None
        owner = owner_by_port.get(port) if port is not None else None
        attribution = _attribution(conn, local, owner)
        row = _row_from_detail(port, detail, attribution)
        if _row_matches_filters(row, filters):
            rows.append(row)
    return rows


def _local_rows_by_port(proxy_port: int) -> dict[int, LocalConnection]:
    """Return lsof rows keyed by local source port."""
    rows = [
        row for row in collect_lsof_connections([])
        if row.source_port != proxy_port
    ]
    return {row.source_port: row for row in rows}


def _row_from_detail(port: int | None, detail: dict[str, Any],
                     attribution: dict[str, Any]) -> dict[str, Any]:
    """Build one normalized traffic row."""
    upload = _number(detail.get("upload"))
    download = _number(detail.get("download"))
    chains = [str(item) for item in detail.get("chains") or []]
    return {
        "id": detail.get("id") or "",
        "local_source_port": port,
        "host": detail.get("host") or "",
        "destination_ip": detail.get("destination_ip") or "",
        "rule": detail.get("rule") or "",
        "rule_payload": detail.get("rule_payload") or "",
        "route_kind": detail.get("route_kind") or "unknown",
        "line": _line_name(chains),
        "chains": chains,
        "chain": _chain_name(chains),
        "upload": upload,
        "download": download,
        "total": upload + download,
        "start": detail.get("start") or "",
        "attribution": attribution,
    }


def _attribution(conn: dict[str, Any],
                 local: LocalConnection | None,
                 owner: ProxyOwner | None) -> dict[str, Any]:
    """Return best-effort app attribution for one Mihomo connection."""
    if owner is not None and not _is_system_extension_owner(owner):
        return {
            "app": _owner_label(owner),
            "confidence": "socket-owner",
            "source": "netstat",
            "owner_app": owner.app,
            "owner_pid": owner.pid,
            "candidate_contexts": [],
        }
    if owner is not None and _is_system_extension_owner(owner):
        contexts = _remote_candidate_contexts(conn, [])
        if contexts:
            return {
                "app": _context_label(contexts),
                "confidence": "inferred",
                "source": "destination",
                "owner_app": owner.app,
                "owner_pid": owner.pid,
                "candidate_contexts": contexts,
            }
        return {
            "app": _owner_label(owner),
            "confidence": "system-extension",
            "source": "netstat",
            "owner_app": owner.app,
            "owner_pid": owner.pid,
            "candidate_contexts": [],
        }
    if local is not None:
        contexts = _detect_app_contexts(local._match_text())
        return {
            "app": _context_label(contexts) if contexts else _local_label(local),
            "confidence": "process",
            "source": "lsof",
            "owner_app": local.app,
            "owner_pid": local.pid,
            "candidate_contexts": contexts,
        }
    contexts = _remote_candidate_contexts(conn, [])
    if contexts:
        return {
            "app": _context_label(contexts),
            "confidence": "inferred",
            "source": "destination",
            "owner_app": "",
            "owner_pid": None,
            "candidate_contexts": contexts,
        }
    return {
        "app": "未知",
        "confidence": "unknown",
        "source": "none",
        "owner_app": "",
        "owner_pid": None,
        "candidate_contexts": [],
    }


def _row_matches_filters(row: dict[str, Any], filters: ConnectionArgs) -> bool:
    """Return whether a traffic row satisfies shared connection filters."""
    attribution = row["attribution"]
    process_text = " ".join([
        str(attribution.get("app") or ""),
        str(attribution.get("owner_app") or ""),
    ])
    target_text = " ".join([
        row["host"],
        row["destination_ip"],
        row["rule"],
        row["rule_payload"],
        row["route_kind"],
    ])
    contexts = list(attribution.get("candidate_contexts") or [])
    detail = {
        "host": row["host"],
        "destination_ip": row["destination_ip"],
        "rule": row["rule"],
        "rule_payload": row["rule_payload"],
        "route_kind": row["route_kind"],
        "chains": row["chains"],
    }
    return _matches_filter_dimensions(
        process_text,
        " ".join([target_text, _detail_text(detail)]),
        contexts,
        filters,
        chain_text=_chain_text(detail),
        route_kind=_route_kind_text(detail),
    )


def _aggregate_rows(rows: list[dict[str, Any]],
                    group_by: list[str]) -> list[dict[str, Any]]:
    """Aggregate traffic rows by requested dimensions."""
    buckets: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        dimensions = _dimensions(row, group_by)
        key = tuple(dimensions[name] for name in group_by)
        bucket = buckets.setdefault(key, _new_bucket(dimensions))
        _add_row(bucket, row)
    return sorted(
        buckets.values(),
        key=lambda item: (-item["total"], item["dimensions"]),
    )


def _new_bucket(dimensions: dict[str, str]) -> dict[str, Any]:
    """Return an empty aggregate bucket."""
    return {
        "dimensions": dimensions,
        "connection_count": 0,
        "upload": 0,
        "download": 0,
        "total": 0,
        "route_kinds": [],
        "chain_variants": [],
        "hosts": [],
        "app_breakdown": [],
    }


def _add_row(bucket: dict[str, Any], row: dict[str, Any]) -> None:
    """Add one row to an aggregate bucket."""
    bucket["connection_count"] += 1
    bucket["upload"] += row["upload"]
    bucket["download"] += row["download"]
    bucket["total"] += row["total"]
    _add_unique(bucket, "route_kinds", row["route_kind"])
    _add_unique(bucket, "hosts", row["host"] or row["destination_ip"])
    _add_chain_variant(bucket, row)
    _add_app_breakdown(bucket, row)


def _add_unique(bucket: dict[str, Any], field: str, value: str) -> None:
    """Append a unique non-empty string to a bucket list."""
    if value and value not in bucket[field]:
        bucket[field].append(value)


def _add_chain_variant(bucket: dict[str, Any], row: dict[str, Any]) -> None:
    """Track traffic by full chain inside a bucket."""
    chain = row["chain"]
    for variant in bucket["chain_variants"]:
        if variant["chain"] == chain:
            _add_bytes(variant, row)
            return
    variant = {"chain": chain, "chains": row["chains"],
               "connection_count": 0, "upload": 0, "download": 0, "total": 0}
    _add_bytes(variant, row)
    bucket["chain_variants"].append(variant)


def _add_app_breakdown(bucket: dict[str, Any], row: dict[str, Any]) -> None:
    """Track traffic by attributed app inside a bucket."""
    attr = row["attribution"]
    for item in bucket["app_breakdown"]:
        if (item["app"], item["confidence"]) == (
            attr["app"], attr["confidence"]
        ):
            _add_owner_evidence(item, attr)
            _add_bytes(item, row)
            return
    item = {
        "app": attr["app"],
        "confidence": attr["confidence"],
        "source": attr["source"],
        "owner_apps": [],
        "owner_pids": [],
        "connection_count": 0,
        "upload": 0,
        "download": 0,
        "total": 0,
    }
    _add_owner_evidence(item, attr)
    _add_bytes(item, row)
    bucket["app_breakdown"].append(item)


def _add_owner_evidence(item: dict[str, Any], attr: dict[str, Any]) -> None:
    """Attach socket-owner evidence to an app breakdown item."""
    owner_app = attr.get("owner_app")
    if owner_app and owner_app not in item["owner_apps"]:
        item["owner_apps"].append(owner_app)
    owner_pid = attr.get("owner_pid")
    if owner_pid is not None and owner_pid not in item["owner_pids"]:
        item["owner_pids"].append(owner_pid)


def _add_bytes(target: dict[str, Any], row: dict[str, Any]) -> None:
    """Add row byte counters to an aggregate target."""
    target["connection_count"] += 1
    target["upload"] += row["upload"]
    target["download"] += row["download"]
    target["total"] += row["total"]


def _totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Return total counters for all rows."""
    return {
        "connection_count": len(rows),
        "upload": sum(row["upload"] for row in rows),
        "download": sum(row["download"] for row in rows),
        "total": sum(row["total"] for row in rows),
    }


def _dimensions(row: dict[str, Any], group_by: list[str]) -> dict[str, str]:
    """Return group dimensions for one row."""
    values = {
        "line": row["line"],
        "chain": row["chain"],
        "app": row["attribution"]["app"],
        "route": row["route_kind"],
    }
    return {name: values[name] for name in group_by}


def _line_name(chains: list[str]) -> str:
    """Return the selected outbound line for a chain."""
    return chains[0] if chains else "unknown"


def _chain_name(chains: list[str]) -> str:
    """Return a stable full-chain label."""
    return " -> ".join(chains) if chains else "unknown"


def _context_label(contexts: list[str]) -> str:
    """Return a readable app-context label."""
    labels = [APP_CONTEXT_LABELS[item] for item in contexts
              if item in APP_CONTEXT_LABELS]
    return " / ".join(labels) if labels else "未知"


def _owner_label(owner: ProxyOwner) -> str:
    """Return a readable socket-owner label."""
    if owner.app and owner.app != "?":
        return owner.app
    if owner.process:
        return os.path.basename(owner.process)
    if owner.command:
        return os.path.basename(owner.command.split()[0])
    return "未知进程"


def _local_label(row: LocalConnection) -> str:
    """Return a readable local-process label."""
    if row.app and row.app != "?":
        return row.app
    if row.process:
        return os.path.basename(row.process)
    if row.command:
        return os.path.basename(row.command.split()[0])
    return "未知进程"


def _number(value: Any) -> int:
    """Convert a Mihomo byte counter to int."""
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _format_bytes(value: int) -> str:
    """Format bytes for compact human output."""
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def emit_human(report: dict[str, Any]) -> None:
    """Render a human-readable traffic snapshot."""
    totals = report["totals"]
    print(
        "proxyctl traffic snapshot  "
        f"backend={report['backend']}  分组={','.join(report['group_by'])}"
    )
    print("  说明：这是当前活跃连接快照，不是历史累计；历史累计需要持续采样。")
    print(
        "  总计："
        f"{totals['connection_count']} 条连接  "
        f"下载={_format_bytes(totals['download'])}  "
        f"上传={_format_bytes(totals['upload'])}  "
        f"合计={_format_bytes(totals['total'])}"
    )
    if report["api"].get("status") != "ok":
        print(f"  API：{report['api'].get('status')} {report['api'].get('error')}")
        return
    if not report["groups"]:
        print("  没有匹配的活跃连接流量")
        return
    for group in report["groups"]:
        _emit_group(group)


def _emit_group(group: dict[str, Any]) -> None:
    """Render one traffic aggregate group."""
    dims = "  ".join(f"{key}={value}"
                     for key, value in group["dimensions"].items())
    print(
        f"  {dims}  {group['connection_count']} 条  "
        f"下载={_format_bytes(group['download'])}  "
        f"上传={_format_bytes(group['upload'])}  "
        f"合计={_format_bytes(group['total'])}"
    )
    if group["route_kinds"]:
        print(f"    路由: {', '.join(group['route_kinds'])}")
    if group["chain_variants"]:
        chains = [item["chain"] for item in group["chain_variants"][:3]]
        print(f"    链路: {'; '.join(chains)}")
    if group["hosts"]:
        print(f"    主机: {', '.join(group['hosts'][:5])}")
    print("    软件:")
    for item in sorted(group["app_breakdown"],
                       key=lambda row: (-row["total"], row["app"]))[:5]:
        owner = (
            f" owner={','.join(item['owner_apps'])}"
            if item.get("owner_apps") else ""
        )
        print(
            f"      {item['app']}  {item['connection_count']} 条  "
            f"合计={_format_bytes(item['total'])}  "
            f"可信度={item['confidence']}{owner}"
        )


def cmd_traffic(args: list[str], backend, config: dict[str, Any]) -> None:
    """Entry point for ``proxyctl traffic``."""
    parsed = parse_args(args)
    report = build_snapshot(backend.name, config, parsed)
    if _io.is_json_mode():
        _io.emit_json(_io.envelope("traffic", data=report))
        return
    emit_human(report)
