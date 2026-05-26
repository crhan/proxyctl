"""proxyctl traffic — active and sampled Mihomo traffic accounting.

``snapshot`` and ``report`` are read-only. ``sample`` and ``watch`` write a
small local cache so later reports can aggregate deltas. Mihomo only exposes
currently active connections, so the sampler deliberately treats first-seen
connections as baselines instead of inventing historical bytes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
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
from proxyctl.traffic_store import (
    TRAFFIC_EVENTS_FILE,
    TRAFFIC_STATE_FILE,
    append_events as _append_events,
    delta_events as _delta_events,
    load_state as _load_state,
    merge_totals as _merge_totals,
    now as _now,
    parse_timestamp as _parse_ts,
    read_events as _read_events,
    save_state as _save_state,
    store_paths as _raw_store_paths,
)

VALID_GROUPS = ("line", "chain", "app", "route")
VALID_SUBCMDS = ("snapshot", "sample", "watch", "report")
WatchProgress = Callable[[int, dict[str, Any]], None]
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
        subcmd: ``snapshot`` / ``sample`` / ``watch`` / ``report``.
        group_by: Aggregation dimensions. Defaults to ``["line"]``.
        filters: Reused connection filters for host/line/route/agent narrowing.
        interval: Sampling interval for ``watch``.
        count: Optional watch sample count.
        since: Report window such as ``1h`` / ``30m``.
        store_dir: Optional override for traffic state/event files.
    """

    subcmd: str = "snapshot"
    group_by: list[str] = field(default_factory=lambda: ["line"])
    filters: ConnectionArgs = field(default_factory=lambda: ConnectionArgs([]))
    interval: float = 5.0
    count: int | None = None
    since: str = "1h"
    store_dir: str | None = None


def parse_args(args: list[str]) -> TrafficArgs:
    """Parse ``proxyctl traffic`` arguments."""
    parsed = TrafficArgs()
    idx = 0
    if args and not args[0].startswith("-"):
        parsed.subcmd = args[0]
        idx = 1
    if parsed.subcmd not in VALID_SUBCMDS:
        _io.fail(f"traffic 暂不支持子命令：{parsed.subcmd}",
                 hints=["proxyctl traffic snapshot",
                        "proxyctl traffic sample",
                        "proxyctl traffic report --since 1h"],
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
        if arg == "--interval":
            parsed.interval = _parse_positive_float(args, idx, "--interval")
            idx += 2
            continue
        if arg == "--count":
            parsed.count = _parse_positive_int(args, idx, "--count")
            idx += 2
            continue
        if arg == "--since":
            if idx + 1 >= len(args):
                _io.fail("traffic --since 需要一个时间窗口",
                         hint="proxyctl traffic report --since 1h",
                         doc="agent-protocol", code=_io.USAGE, cmd="traffic")
            _parse_since(args[idx + 1])
            parsed.since = args[idx + 1]
            idx += 2
            continue
        if arg == "--store":
            if idx + 1 >= len(args):
                _io.fail("traffic --store 需要一个目录路径",
                         hint="proxyctl traffic sample --store /tmp/proxyctl-traffic",
                         doc="agent-protocol", code=_io.USAGE, cmd="traffic")
            parsed.store_dir = args[idx + 1]
            idx += 2
            continue
        if arg in FILTER_VALUE_FLAGS:
            _append_filter_arg(parsed.filters, arg, args, idx)
            idx += 2
            continue
        _io.fail(f"未识别 traffic 参数：{arg}",
                 hints=["proxyctl traffic --by line,app",
                        "proxyctl traffic sample",
                        "proxyctl traffic report --since 1h",
                        "proxyctl traffic --chain residential-sg",
                        "proxyctl traffic --route proxy --preset ai"],
                 doc="agent-protocol", code=_io.USAGE, cmd="traffic")
    return parsed


def _parse_positive_float(args: list[str], idx: int, flag: str) -> float:
    """Parse a positive float flag value."""
    if idx + 1 >= len(args):
        _io.fail(f"traffic {flag} 需要一个数字",
                 hint=f"proxyctl traffic watch {flag} 5",
                 doc="agent-protocol", code=_io.USAGE, cmd="traffic")
    try:
        value = float(args[idx + 1])
    except ValueError:
        value = 0.0
    if value <= 0:
        _io.fail(f"traffic {flag} 必须大于 0",
                 hint=f"proxyctl traffic watch {flag} 5",
                 doc="agent-protocol", code=_io.USAGE, cmd="traffic")
    return value


def _parse_positive_int(args: list[str], idx: int, flag: str) -> int:
    """Parse a positive integer flag value."""
    if idx + 1 >= len(args):
        _io.fail(f"traffic {flag} 需要一个整数",
                 hint=f"proxyctl traffic watch {flag} 12",
                 doc="agent-protocol", code=_io.USAGE, cmd="traffic")
    try:
        value = int(args[idx + 1])
    except ValueError:
        value = 0
    if value <= 0:
        _io.fail(f"traffic {flag} 必须大于 0",
                 hint=f"proxyctl traffic watch {flag} 12",
                 doc="agent-protocol", code=_io.USAGE, cmd="traffic")
    return value


def _parse_since(value: str) -> timedelta:
    """Parse report window text such as ``1h`` / ``30m`` / ``2d``."""
    text = value.strip().lower()
    if len(text) < 2:
        _io.fail(f"traffic --since 格式错误：{value}",
                 hint="示例: --since 30m / --since 1h / --since 2d",
                 doc="agent-protocol", code=_io.USAGE, cmd="traffic")
    unit = text[-1]
    try:
        amount = float(text[:-1])
    except ValueError:
        amount = 0.0
    if amount <= 0 or unit not in ("s", "m", "h", "d"):
        _io.fail(f"traffic --since 格式错误：{value}",
                 hint="示例: --since 30m / --since 1h / --since 2d",
                 doc="agent-protocol", code=_io.USAGE, cmd="traffic")
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return timedelta(seconds=amount * scale)


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
        "filters": _filter_dict(args.filters),
        "api": api_status,
        "totals": _totals(rows),
        "groups": groups,
        "connections": rows,
    }


def record_sample(backend_name: str, config: dict[str, Any],
                  args: TrafficArgs) -> dict[str, Any]:
    """Record one traffic delta sample into the local cache."""
    snapshot_args = TrafficArgs(subcmd="snapshot", filters=args.filters)
    snapshot = build_snapshot(backend_name, config, snapshot_args)
    store = _store_paths(config, args)
    now = _now()
    state = _load_state(store["state_path"])
    events, baseline_count = _delta_events(snapshot["connections"], state, now)
    if events:
        _append_events(store["events_path"], events)
    _save_state(store["state_path"], state, now)
    return {
        "scope": "traffic_delta_sample",
        "backend": backend_name,
        "store": store,
        "api": snapshot["api"],
        "active_connection_count": len(snapshot["connections"]),
        "baseline_connection_count": baseline_count,
        "recorded_event_count": len(events),
        "totals": _totals(events),
        "events": events,
    }


def run_watch(backend_name: str, config: dict[str, Any],
              args: TrafficArgs,
              on_sample: WatchProgress | None = None) -> dict[str, Any]:
    """Record traffic samples repeatedly."""
    sample_count = 0
    totals = {"connection_count": 0, "upload": 0, "download": 0, "total": 0}
    last: dict[str, Any] | None = None
    try:
        while args.count is None or sample_count < args.count:
            last = record_sample(backend_name, config, args)
            sample_count += 1
            _merge_totals(totals, last["totals"])
            if on_sample is not None:
                on_sample(sample_count, last)
            if args.count is not None and sample_count >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return {
        "scope": "traffic_watch",
        "backend": backend_name,
        "sample_count": sample_count,
        "interval": args.interval,
        "totals": totals,
        "last_sample": last,
    }


def build_report(backend_name: str, config: dict[str, Any],
                 args: TrafficArgs) -> dict[str, Any]:
    """Build a traffic report from locally recorded sample events."""
    store = _store_paths(config, args)
    since_delta = _parse_since(args.since)
    cutoff = _now() - since_delta
    events = [
        event for event in _read_events(store["events_path"])
        if _parse_ts(event.get("sample_ts")) >= cutoff
    ]
    filtered = [event for event in events if _row_matches_filters(event, args.filters)]
    groups = _aggregate_rows(filtered, args.group_by)
    return {
        "scope": "traffic_recorded_report",
        "scope_note": (
            "This report uses locally recorded traffic delta samples. It only "
            "covers periods where proxyctl traffic sample/watch was running."
        ),
        "backend": backend_name,
        "store": store,
        "since": args.since,
        "cutoff": cutoff.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "group_by": args.group_by,
        "filters": _filter_dict(args.filters),
        "totals": _totals(filtered),
        "groups": groups,
        "events": filtered,
    }


def _store_paths(config: dict[str, Any], args: TrafficArgs) -> dict[str, str]:
    """Return traffic cache paths."""
    return _raw_store_paths(config, args.store_dir)


def _filter_dict(filters: ConnectionArgs) -> dict[str, list[str]]:
    """Return the JSON filter contract."""
    return {
        "app": filters.app_filters,
        "host": filters.host_filters,
        "chain": filters.chain_filters,
        "route": filters.route_filters,
        "preset": filters.preset_filters,
        "agent": filters.agent_filters,
        "query": filters.query_filters,
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
    row_fields = {
        "target_host": row["host"] or "",
        "destination_ip": row["destination_ip"] or "",
        "app": str(attribution.get("app") or ""),
        "process": str(attribution.get("owner_app") or ""),
        "command": "",
        "target_port": None,
        "source_port": None,
        "pid": attribution.get("owner_pid"),
    }
    return _matches_filter_dimensions(
        process_text,
        " ".join([target_text, _detail_text(detail)]),
        contexts,
        filters,
        chain_text=_chain_text(detail),
        route_kind=_route_kind_text(detail),
        row_fields=row_fields,
    ) is not None


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
        key=lambda item: (-item["total"], _dimension_sort_key(item)),
    )


def _dimension_sort_key(bucket: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Return a stable aggregate sort key for dimension values."""
    return tuple(
        (name, str(value))
        for name, value in bucket["dimensions"].items()
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
    """Render human-readable traffic output for all scopes."""
    scope = report.get("scope")
    if scope == "traffic_delta_sample":
        _emit_sample_human(report)
        return
    if scope == "traffic_watch":
        _emit_watch_human(report)
        return
    if scope == "traffic_recorded_report":
        _emit_report_human(report)
        return
    _emit_snapshot_human(report)


def _emit_snapshot_human(report: dict[str, Any]) -> None:
    """Render a human-readable active traffic snapshot."""
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


def _emit_sample_human(report: dict[str, Any]) -> None:
    """Render one recorded sample result."""
    totals = report["totals"]
    store = report["store"]
    print(f"proxyctl traffic sample  backend={report['backend']}")
    print("  说明：新增连接先建立基线；只有连续两次采样之间的字节增量会入库。")
    print(
        "  本次："
        f"活跃={report['active_connection_count']} 条  "
        f"新基线={report['baseline_connection_count']} 条  "
        f"记录增量={report['recorded_event_count']} 条"
    )
    print(
        "  增量："
        f"下载={_format_bytes(totals['download'])}  "
        f"上传={_format_bytes(totals['upload'])}  "
        f"合计={_format_bytes(totals['total'])}"
    )
    print(f"  状态文件：{store['state_path']}")
    print(f"  事件文件：{store['events_path']}")


def _emit_watch_human(report: dict[str, Any]) -> None:
    """Render repeated sampling summary."""
    totals = report["totals"]
    last = report.get("last_sample") or {}
    store = (last.get("store") or {}) if isinstance(last, dict) else {}
    print(
        "proxyctl traffic watch summary  "
        f"backend={report['backend']}  "
        f"采样={report['sample_count']} 次  "
        f"间隔={report['interval']}s"
    )
    print(
        "  增量："
        f"记录={totals['connection_count']} 条  "
        f"下载={_format_bytes(totals['download'])}  "
        f"上传={_format_bytes(totals['upload'])}  "
        f"合计={_format_bytes(totals['total'])}"
    )
    if store.get("events_path"):
        print(f"  事件文件：{store['events_path']}")


def _emit_watch_progress(sample_index: int, sample: dict[str, Any]) -> None:
    """Render one live watch progress line."""
    totals = sample["totals"]
    api_status = sample.get("api", {}).get("status", "unknown")
    print(
        f"[{sample_index}] "
        f"api={api_status}  "
        f"活跃={sample['active_connection_count']}  "
        f"新基线={sample['baseline_connection_count']}  "
        f"记录={sample['recorded_event_count']}  "
        f"下载={_format_bytes(totals['download'])}  "
        f"上传={_format_bytes(totals['upload'])}  "
        f"合计={_format_bytes(totals['total'])}",
        flush=True,
    )


def _emit_report_human(report: dict[str, Any]) -> None:
    """Render a report built from recorded events."""
    totals = report["totals"]
    store = report["store"]
    print(
        "proxyctl traffic report  "
        f"backend={report['backend']}  "
        f"窗口={report['since']}  分组={','.join(report['group_by'])}"
    )
    print("  说明：只统计本机已记录的采样增量；未运行 sample/watch 的时间段无法补算。")
    print(
        "  总计："
        f"{totals['connection_count']} 条增量  "
        f"下载={_format_bytes(totals['download'])}  "
        f"上传={_format_bytes(totals['upload'])}  "
        f"合计={_format_bytes(totals['total'])}"
    )
    print(f"  事件文件：{store['events_path']}")
    if not report["groups"]:
        print("  没有匹配的已记录流量")
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
    progress = _watch_progress_callback(parsed)
    report = _build_report_with_optional_lock(
        backend.name, config, parsed, progress)
    if _io.is_json_mode():
        _io.emit_json(_io.envelope("traffic", data=report))
        return
    emit_human(report)


def _watch_progress_callback(args: TrafficArgs) -> WatchProgress | None:
    """Return the human watch progress callback when stdout may stream text."""
    if args.subcmd == "watch" and not _io.is_json_mode():
        return _emit_watch_progress
    return None


def _build_report_with_optional_lock(backend_name: str, config: dict[str, Any],
                                     args: TrafficArgs,
                                     on_sample: WatchProgress | None = None
                                     ) -> dict[str, Any]:
    """Build traffic output, locking sampler writes."""
    if args.subcmd not in ("sample", "watch"):
        return _build_report_for_subcmd(backend_name, config, args)
    try:
        with _io.with_lock("traffic"):
            return _build_report_for_subcmd(
                backend_name, config, args, on_sample)
    except _io.LockedError as error:
        _io.fail(
            "另一个 proxyctl traffic 采样写操作正在进行（lock: traffic）",
            hints=[
                f"锁文件: {error.lock_path}",
                f"排查: lsof {error.lock_path}",
            ],
            doc="locks",
            code=_io.LOCKED,
            cmd="traffic",
        )


def _build_report_for_subcmd(backend_name: str, config: dict[str, Any],
                             args: TrafficArgs,
                             on_sample: WatchProgress | None = None
                             ) -> dict[str, Any]:
    """Dispatch parsed traffic args to the matching builder."""
    if args.subcmd == "snapshot":
        return build_snapshot(backend_name, config, args)
    if args.subcmd == "sample":
        return record_sample(backend_name, config, args)
    if args.subcmd == "watch":
        return run_watch(backend_name, config, args, on_sample)
    return build_report(backend_name, config, args)
