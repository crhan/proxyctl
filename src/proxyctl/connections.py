"""proxyctl connections — join local app sockets with mihomo connections.

The command is intentionally read-only: it reads lsof/ps output and, for the
mihomo backend, the local Clash-compatible `/connections` controller endpoint.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from proxyctl import _io
from proxyctl.connections_filters import (
    APP_CONTEXTS,
    APP_CONTEXT_LABELS,
    APP_FILTER_ALIASES,
    APP_REMOTE_PATTERNS,
    DEFAULT_APP_FILTERS,
    PRESET_AGENTS,
    ROUTE_FILTER_ALIASES,
    _chain_text,
    _contexts_for_filter,
    _detect_app_contexts,
    _detail_text,
    _effective_contexts,
    _matches_app_text,
    _matches_filter_dimensions,
    _merge_contexts,
    _normalize_app_filter,
    _normalize_filter_value,
    _normalize_route_filter,
    _remote_candidate_contexts,
    _remote_candidate_contexts_for_args,
    _route_kind_text,
)

KNOWN_SYSTEM_EXTENSION_OWNERS = ("com.antgroup.asp",)


@dataclass
class LocalConnection:
    """One local TCP connection reported by lsof.

    Attributes:
        pid: Owning process id.
        app: Short process name from lsof.
        fd: File descriptor label from lsof, for example ``12u``.
        source_port: Local ephemeral port used to connect to proxy_port.
        target_host: Destination host from lsof's NAME field.
        target_port: Destination port from lsof's NAME field.
        raw_name: Original lsof NAME field.
        process: Full executable path from ps, when available.
        command: Full command line from ps, when available.
    """

    pid: int
    app: str
    fd: str
    source_port: int
    target_host: str
    target_port: int
    raw_name: str
    process: str = ""
    command: str = ""

    def _match_text(self) -> str:
        """Return searchable process text for context detection."""
        return " ".join(
            [self.app, os.path.basename(self.process), self.process, self.command]
        )

    def matches_app(self, filters: list[str]) -> bool:
        """Return whether this row matches any requested ``--app`` filter."""
        return _matches_app_text(self._match_text(), filters)

    def to_dict(self, proxy_port: int) -> dict[str, Any]:
        """Convert the lsof row to the JSON contract used by this command."""
        return {
            "app": self.app,
            "pid": self.pid,
            "fd": self.fd,
            "process": self.process,
            "app_contexts": _detect_app_contexts(self._match_text()),
            "local_source_port": self.source_port,
            "target_host": self.target_host,
            "target_port": self.target_port,
            "connects_proxy_port": self.target_port == proxy_port,
            "raw_lsof_name": self.raw_name,
        }


@dataclass
class ProxyOwner:
    """One proxy-client socket owner reported by macOS ``netstat``.

    Attributes:
        source_port: Local client port that connects to ``proxy_port``.
        target_port: Local proxy listener port.
        state: Kernel TCP state, for example ``ESTABLISHED``.
        app: Process name as reported by ``netstat``.
        pid: Process id as reported by ``netstat`` when available.
        raw_line: Original ``netstat`` row.
        process: Full executable path from ``ps``, when available.
        command: Full command line from ``ps``, when available.
    """

    source_port: int
    target_port: int
    state: str
    app: str
    pid: int | None
    raw_line: str
    process: str = ""
    command: str = ""

    def _match_text(self) -> str:
        """Return searchable owner text for context detection."""
        return " ".join(
            [self.app, os.path.basename(self.process), self.process, self.command]
        )

    def matches_app(self, filters: list[str]) -> bool:
        """Return whether this owner matches any requested ``--app`` filter."""
        return _matches_app_text(self._match_text(), filters)

    def to_dict(self, app_filters: list[str]) -> dict[str, Any]:
        """Convert the owner row to the JSON contract used by this command."""
        return {
            "app": self.app,
            "pid": self.pid,
            "process": self.process,
            "command": self.command,
            "app_contexts": _detect_app_contexts(self._match_text()),
            "source": "netstat",
            "state": self.state,
            "local_source_port": self.source_port,
            "target_port": self.target_port,
            "matches_app_filter": (
                bool(app_filters) and self.matches_app(app_filters)
            ),
            "system_extension_owner": _is_system_extension_owner(self),
            "raw_netstat_line": self.raw_line,
        }


@dataclass
class ConnectionArgs:
    """Parsed arguments for ``proxyctl connections``.

    Attributes:
        app_filters: Legacy process/app filters from ``--app``.
        host_filters: Host/rule-payload/destination filters from ``--host``.
        chain_filters: Mihomo chain/node filters from ``--chain`` / ``--line``.
        route_filters: Mihomo route-kind filters from ``--route``.
        preset_filters: Named filter presets from ``--preset``.
        agent_filters: Agent/tool filters from ``--agent``.
        query_filters: Free-text filters from ``--query`` / ``--filter``.
        all_apps: Whether ``--all`` was explicitly requested.
    """

    app_filters: list[str]
    host_filters: list[str] = field(default_factory=list)
    chain_filters: list[str] = field(default_factory=list)
    route_filters: list[str] = field(default_factory=list)
    preset_filters: list[str] = field(default_factory=list)
    agent_filters: list[str] = field(default_factory=list)
    query_filters: list[str] = field(default_factory=list)
    all_apps: bool = False

    def has_filters(self) -> bool:
        """Return whether any narrowing filter is active."""
        return any([
            self.app_filters,
            self.host_filters,
            self.chain_filters,
            self.route_filters,
            self.preset_filters,
            self.agent_filters,
            self.query_filters,
        ])


def parse_args(args: list[str]) -> ConnectionArgs:
    """Parse ``proxyctl connections`` arguments.

    Supported syntax:
        ``--host`` / ``--chain`` / ``--route`` / ``--preset`` / ``--agent`` /
        ``--query`` are repeatable. ``--line`` aliases ``--chain``.
        ``--app`` is kept as a legacy alias for process/app filtering.
    """
    parsed = ConnectionArgs(app_filters=[])
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg == "--all":
            parsed.all_apps = True
            idx += 1
            continue
        if arg in ("--app", "--host", "--chain", "--line", "--route",
                   "--preset", "--agent",
                   "--query", "--filter"):
            _append_filter_arg(parsed, arg, args, idx)
            idx += 2
            continue
        _io.fail(f"未识别 connections 参数：{arg}",
                 hints=["proxyctl connections --host anthropic.com",
                        "proxyctl connections --chain SG-Residential-01",
                        "proxyctl connections --route proxy",
                        "proxyctl connections --preset ai",
                        "proxyctl connections --agent codex --json",
                        "proxyctl connections --json"],
                 doc="agent-protocol", code=_io.USAGE, cmd="connections")
    return parsed


def _append_filter_arg(parsed: ConnectionArgs, arg: str,
                       args: list[str], idx: int) -> None:
    """Append one repeatable filter argument."""
    if idx + 1 >= len(args):
        _io.fail(f"connections {arg} 需要一个值",
                 hint="proxyctl connections --host anthropic.com --json",
                 doc="agent-protocol", code=_io.USAGE, cmd="connections")
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
    _io.fail(f"未知 connections preset：{value}",
             hint=f"可用 preset: {known}",
             doc="agent-protocol", code=_io.USAGE, cmd="connections")


def _validate_route(value: str) -> None:
    """Fail early when a route filter cannot match a Mihomo route_kind."""
    if _normalize_route_filter(value):
        return
    known = ", ".join(sorted(ROUTE_FILTER_ALIASES))
    _io.fail(f"未知 connections route：{value}",
             hint=f"可用 route: {known}",
             doc="agent-protocol", code=_io.USAGE, cmd="connections")


def _parse_lsof_name(name: str) -> tuple[int, str, int] | None:
    """Parse lsof NAME into ``(source_port, target_host, target_port)``.

    The format differs slightly across macOS/Linux and IPv4/IPv6, but the
    stable part for established TCP is ``local:port->remote:port``.
    """
    if "->" not in name:
        return None
    left, right = name.split("->", 1)
    right = right.split(" ", 1)[0]
    source_match = re.search(r":(\d+)$", left.strip("[]"))
    target_match = re.search(r"(.+):(\d+)$", right.strip())
    if not source_match or not target_match:
        return None
    target_host = target_match.group(1).strip("[]")
    return int(source_match.group(1)), target_host, int(target_match.group(2))


def _parse_endpoint(endpoint: str) -> tuple[str, int] | None:
    """Parse ``host:port`` or ``[ipv6]:port`` endpoint text."""
    endpoint = endpoint.strip()
    if endpoint.startswith("["):
        m = re.match(r"^\[([^\]]+)\]:(\d+)$", endpoint)
        if not m:
            return None
        return m.group(1), int(m.group(2))
    if ":" not in endpoint:
        return None
    host, port = endpoint.rsplit(":", 1)
    try:
        return host.strip("[]"), int(port)
    except ValueError:
        return None


def parse_lsof_fields(text: str) -> list[LocalConnection]:
    """Parse ``lsof -Fpcfn`` established TCP output."""
    rows: list[LocalConnection] = []
    pid: int | None = None
    app = ""
    fd = ""
    for line in text.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            try:
                pid = int(value)
            except ValueError:
                pid = None
            app = ""
            fd = ""
        elif tag == "c":
            app = value
        elif tag == "f":
            fd = value
        elif tag == "n" and pid is not None:
            parsed = _parse_lsof_name(value)
            if not parsed:
                continue
            source_port, target_host, target_port = parsed
            rows.append(LocalConnection(
                pid=pid, app=app, fd=fd, source_port=source_port,
                target_host=target_host, target_port=target_port,
                raw_name=value,
            ))
    return rows


def parse_ss_lines(text: str) -> list[LocalConnection]:
    """Parse Linux ``ss -Htnp`` established TCP output."""
    rows: list[LocalConnection] = []
    proc_re = re.compile(r'"(?P<app>[^"]+)",pid=(?P<pid>\d+),fd=(?P<fd>\d+)')
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "ESTAB":
            continue
        local = _parse_endpoint(parts[3])
        peer = _parse_endpoint(parts[4])
        if not local or not peer:
            continue
        proc_match = proc_re.search(" ".join(parts[5:]))
        if proc_match:
            app = proc_match.group("app")
            pid = int(proc_match.group("pid"))
            fd = proc_match.group("fd")
        else:
            app, pid, fd = "", 0, ""
        rows.append(LocalConnection(
            pid=pid,
            app=app,
            fd=fd,
            source_port=local[1],
            target_host=peer[0],
            target_port=peer[1],
            raw_name=f"{parts[3]}->{parts[4]}",
        ))
    return rows


def _parse_netstat_endpoint(endpoint: str) -> tuple[str, int] | None:
    """Parse macOS ``netstat`` endpoints such as ``127.0.0.1.54321``."""
    host, sep, port_text = endpoint.rpartition(".")
    if not sep or not host:
        return None
    try:
        return host, int(port_text)
    except ValueError:
        return None


def _split_netstat_owner(owner: str) -> tuple[str, int | None]:
    """Split ``netstat`` owner text into ``(process_name, pid)``."""
    name, sep, pid_text = owner.rpartition(":")
    if not sep:
        return owner, None
    try:
        return name, int(pid_text)
    except ValueError:
        return owner, None


def parse_netstat_proxy_owners(text: str, proxy_port: int,
                               source_ports: set[int]) -> dict[int, ProxyOwner]:
    """Parse macOS ``netstat -anv -p tcp`` rows for proxy-client owners."""
    owners: dict[int, ProxyOwner] = {}
    row_re = re.compile(
        r"^tcp\d?\s+\S+\s+\S+\s+(?P<local>\S+)\s+(?P<peer>\S+)\s+"
        r"(?P<state>\S+)\s+\S+\s+\S+\s+\S+\s+\S+\s+"
        r"(?P<owner>.+?)\s+[0-9a-fA-F]{3,}(?:\s|$)"
    )
    for line in text.splitlines():
        match = row_re.match(line)
        if not match:
            continue
        local = _parse_netstat_endpoint(match.group("local"))
        peer = _parse_netstat_endpoint(match.group("peer"))
        if not local or not peer:
            continue
        if local[0] not in ("127.0.0.1", "::1"):
            continue
        if peer[0] not in ("127.0.0.1", "::1"):
            continue
        if local[1] not in source_ports or peer[1] != proxy_port:
            continue
        app, pid = _split_netstat_owner(match.group("owner").strip())
        owners[local[1]] = ProxyOwner(
            source_port=local[1], target_port=peer[1],
            state=match.group("state"), app=app, pid=pid,
            raw_line=line,
        )
    return owners


def collect_lsof_connections(app_filters: list[str]) -> list[LocalConnection]:
    """Collect local established TCP connections for the requested apps."""
    cmd = ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpcfn"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except FileNotFoundError:
        return collect_ss_connections(app_filters)
    if proc.returncode == 127:
        return collect_ss_connections(app_filters)
    if proc.returncode not in (0, 1):
        _io.fail("lsof 读取本机连接失败",
                 hints=[proc.stderr.strip()] if proc.stderr else None,
                 doc="troubleshooting", code=_io.DEPENDENCY_MISSING,
                 cmd="connections")
    rows = parse_lsof_fields(proc.stdout)
    _enrich_with_ps(rows)
    return [row for row in rows if row.matches_app(app_filters)]


def collect_netstat_proxy_owners(proxy_port: int,
                                 source_ports: set[int]) -> dict[int, ProxyOwner]:
    """Collect macOS proxy-client owners for selected Mihomo source ports."""
    if sys.platform != "darwin" or not source_ports:
        return {}
    try:
        proc = subprocess.run(["netstat", "-anv", "-p", "tcp"],
                              capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    owners = parse_netstat_proxy_owners(proc.stdout, proxy_port, source_ports)
    _enrich_proxy_owners(owners.values())
    return owners


def collect_ss_connections(app_filters: list[str]) -> list[LocalConnection]:
    """Collect Linux established TCP connections with ``ss``."""
    cmd = ["ss", "-Htnp"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except FileNotFoundError:
        _io.fail("读取本机连接需要 lsof 或 ss",
                 hint="安装 lsof，或在 Linux 上安装 iproute2(ss)",
                 doc="troubleshooting", code=_io.DEPENDENCY_MISSING,
                 cmd="connections")
    if proc.returncode != 0:
        _io.fail("ss 读取本机连接失败",
                 hints=[proc.stderr.strip()] if proc.stderr else None,
                 doc="troubleshooting", code=_io.DEPENDENCY_MISSING,
                 cmd="connections")
    rows = parse_ss_lines(proc.stdout)
    _enrich_with_ps([row for row in rows if row.pid])
    return [row for row in rows if row.matches_app(app_filters)]


def _enrich_with_ps(rows: list[LocalConnection]) -> None:
    """Add process path and command line from ps; failures leave lsof data intact."""
    seen: set[int] = set()
    for row in rows:
        if row.pid in seen:
            continue
        seen.add(row.pid)
        process = _ps_field(row.pid, "comm=")
        command = _ps_field(row.pid, "command=")
        for same_pid in rows:
            if same_pid.pid == row.pid:
                same_pid.process = process
                same_pid.command = command


def _enrich_proxy_owners(rows) -> None:
    """Add process path and command line to netstat owners when possible."""
    seen: set[int] = set()
    for row in rows:
        if row.pid is None or row.pid in seen:
            continue
        seen.add(row.pid)
        row.process = _ps_field(row.pid, "comm=")
        row.command = _ps_field(row.pid, "command=")


def _ps_field(pid: int, field: str) -> str:
    """Read one ps output field for ``pid`` and return an empty string on failure."""
    try:
        proc = subprocess.run(["ps", "-p", str(pid), "-o", field],
                              capture_output=True, text=True, timeout=1)
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def fetch_mihomo_connections(api_base: str, api_secret: str,
                             *, timeout: float = 1.0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch local mihomo `/connections` data without using system proxies."""
    url = f"{api_base.rstrip('/')}/connections"
    headers = {}
    if api_secret:
        headers["Authorization"] = f"Bearer {api_secret}"
    req = urllib.request.Request(url, headers=headers)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError,
            json.JSONDecodeError) as exc:
        return [], {"ok": False, "status": "error", "url": url,
                    "error": str(exc)}
    conns = payload.get("connections")
    if not isinstance(conns, list):
        return [], {"ok": False, "status": "error", "url": url,
                    "error": "response missing connections[]"}
    return conns, {"ok": True, "status": "ok", "url": url,
                   "error": None, "count": len(conns)}


def _connection_source_port(conn: dict[str, Any]) -> int | None:
    """Return mihomo metadata.sourcePort as int when present."""
    metadata = conn.get("metadata") or {}
    port = metadata.get("sourcePort")
    try:
        return int(port)
    except (TypeError, ValueError):
        return None


def _mihomo_detail(conn: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract the stable mihomo fields needed by agents."""
    if not conn:
        return None
    metadata = conn.get("metadata") or {}
    route_kind = _mihomo_route_kind(conn)
    return {
        "host": metadata.get("host") or "",
        "destination_ip": metadata.get("destinationIP") or "",
        "destination_port": metadata.get("destinationPort"),
        "network": metadata.get("network") or "",
        "type": metadata.get("type") or "",
        "rule": conn.get("rule") or "",
        "rule_payload": conn.get("rulePayload") or "",
        "chains": conn.get("chains") or [],
        "upload": conn.get("upload"),
        "download": conn.get("download"),
        "start": conn.get("start") or "",
        "id": conn.get("id") or "",
        "route_kind": route_kind,
        "via_proxy_engine": True,
        "routed_via_proxy": route_kind == "proxy",
    }


def _mihomo_route_kind(conn: dict[str, Any]) -> str:
    """Classify whether Mihomo routed a connection through a proxy or DIRECT."""
    chains = conn.get("chains") or []
    upper = {str(item).upper() for item in chains}
    if "REJECT" in upper:
        return "reject"
    if "DIRECT" in upper:
        return "direct"
    if chains:
        return "proxy"
    return "unknown"


def _is_system_extension_owner(owner: ProxyOwner) -> bool:
    """Return whether a socket owner is likely a macOS System Extension."""
    if owner.app in KNOWN_SYSTEM_EXTENSION_OWNERS:
        return True
    text = " ".join([owner.app, owner.process, owner.command]).lower()
    return "/library/systemextensions/" in text or ".systemextension" in text


def _owner_attribution(owner: ProxyOwner, app_filters: list[str]) -> str:
    """Explain what the kernel owner means for app-level attribution."""
    if app_filters and owner.matches_app(app_filters):
        return "owner_matches_app_filter"
    if _is_system_extension_owner(owner):
        return "system_extension_owner_original_app_hidden"
    if app_filters:
        return "owner_does_not_match_app_filter"
    return "visible_owner"


def _original_app_visible(owner: ProxyOwner, app_filters: list[str]) -> bool:
    """Return whether the kernel owner exposes a recognizable original app."""
    if _detect_app_contexts(owner._match_text()):
        return True
    return bool(app_filters) and owner.matches_app(app_filters)


def _proxy_owner_connections(remote_rows: list[dict[str, Any]], proxy_port: int,
                             parsed_args: ConnectionArgs) -> list[dict[str, Any]]:
    """Build reverse-owned proxy connections from Mihomo rows and netstat."""
    ports = {
        port for conn in remote_rows
        if (port := _connection_source_port(conn)) is not None
    }
    owners = collect_netstat_proxy_owners(proxy_port, ports)
    items: list[dict[str, Any]] = []
    for conn in remote_rows:
        port = _connection_source_port(conn)
        owner = owners.get(port) if port is not None else None
        if not owner:
            continue
        selection_reason = _proxy_owner_selection_reason(conn, owner, parsed_args)
        if not selection_reason:
            continue
        detail = _mihomo_detail(conn) or {}
        candidate_contexts = _remote_candidate_contexts_for_args(conn, parsed_args)
        item = {
            "local_source_port": port,
            "connects_proxy_port": True,
            "target_port": proxy_port,
            "owner": owner.to_dict(parsed_args.app_filters),
            "candidate_contexts": candidate_contexts,
            "attribution": _owner_attribution(owner, parsed_args.app_filters),
            "selection_reason": selection_reason,
            "original_app_visible": _original_app_visible(
                owner, parsed_args.app_filters),
            "via_proxy_engine": True,
            "routed_via_proxy": detail.get("routed_via_proxy"),
            "mihomo": detail,
        }
        if _proxy_owner_item_matches_filters(item, parsed_args):
            items.append(item)
    return items


def _proxy_owner_selection_reason(conn: dict[str, Any], owner: ProxyOwner,
                                  parsed_args: ConnectionArgs) -> str | None:
    """Return why a reverse-owned row belongs in the current report."""
    if parsed_args.app_filters and owner.matches_app(parsed_args.app_filters):
        return "owner_matches_app_filter"
    if not parsed_args.has_filters():
        return "all_apps"
    if _remote_candidate_contexts_for_args(conn, parsed_args):
        return "host_matches_app_context"
    return "filter_candidate"


def _proxy_owner_item_matches_filters(item: dict[str, Any],
                                      args: ConnectionArgs) -> bool:
    """Return whether a proxy-owner item satisfies active filters."""
    owner = item.get("owner") or {}
    process_text = " ".join([
        str(owner.get("app") or ""),
        os.path.basename(str(owner.get("process") or "")),
        str(owner.get("process") or ""),
        str(owner.get("command") or ""),
    ])
    contexts = list(item.get("candidate_contexts") or [])
    contexts.extend(owner.get("app_contexts") or [])
    detail = item.get("mihomo")
    return _matches_filter_dimensions(
        process_text, _detail_text(detail), contexts, args,
        chain_text=_chain_text(detail),
        route_kind=_route_kind_text(detail),
    )


def _local_item_matches_filters(item: dict[str, Any],
                                args: ConnectionArgs) -> bool:
    """Return whether a local lsof item satisfies active filters."""
    process_text = " ".join([
        str(item.get("app") or ""),
        os.path.basename(str(item.get("process") or "")),
        str(item.get("process") or ""),
    ])
    target_text = " ".join([
        str(item.get("target_host") or ""),
        _detail_text(item.get("mihomo")),
    ])
    detail = item.get("mihomo")
    return _matches_filter_dimensions(
        process_text, target_text, item.get("app_contexts") or [], args,
        chain_text=_chain_text(detail),
        route_kind=_route_kind_text(detail),
    )


def _proxy_group_key(item: dict[str, Any]) -> tuple[str, str]:
    """Return ``(key_type, key)`` for grouping proxy-owned connections."""
    m = item.get("mihomo") or {}
    if m.get("rule_payload"):
        return "rule_payload", str(m["rule_payload"])
    if m.get("host"):
        return "host", str(m["host"])
    if m.get("destination_ip"):
        return "destination_ip", str(m["destination_ip"])
    return "unknown", "unknown"


def _chain_key(chains: list[Any]) -> tuple[str, ...]:
    """Return a hashable representation of a Mihomo chain."""
    return tuple(str(part) for part in chains)


def _sum_number(items: list[dict[str, Any]], field: str) -> int:
    """Sum numeric Mihomo fields while tolerating missing values."""
    total = 0
    for item in items:
        value = (item.get("mihomo") or {}).get(field)
        if isinstance(value, (int, float)):
            total += int(value)
    return total


def _chain_variants(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize unique route chains in a group."""
    variants: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in items:
        m = item.get("mihomo") or {}
        key = _chain_key(m.get("chains") or [])
        if key not in variants:
            variants[key] = {
                "chains": list(key),
                "route_kind": m.get("route_kind") or "unknown",
                "count": 0,
            }
        variants[key]["count"] += 1
    return sorted(variants.values(), key=lambda row: (-row["count"], row["chains"]))


def _group_warning(consistent_chains: bool, consistent_route_kind: bool) -> str | None:
    """Return the most important route consistency warning for a group."""
    if not consistent_route_kind:
        return "mixed_route_kind"
    if not consistent_chains:
        return "mixed_chains"
    return None


def _proxy_owner_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group reverse-owned proxy connections by destination intent."""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in items:
        buckets.setdefault(_proxy_group_key(item), []).append(item)

    groups: list[dict[str, Any]] = []
    for (key_type, key), rows in buckets.items():
        route_kinds = sorted({(row.get("mihomo") or {}).get("route_kind") or "unknown"
                              for row in rows})
        variants = _chain_variants(rows)
        consistent_chains = len(variants) <= 1
        consistent_route_kind = len(route_kinds) <= 1
        hosts = sorted({(row.get("mihomo") or {}).get("host") or ""
                        for row in rows if (row.get("mihomo") or {}).get("host")})
        candidate_contexts = _merge_contexts(rows, "candidate_contexts")
        owner_contexts = _merge_contexts(
            [row.get("owner") or {} for row in rows], "app_contexts")
        groups.append({
            "key": key,
            "key_type": key_type,
            "connection_count": len(rows),
            "hosts": hosts,
            "contexts": candidate_contexts or owner_contexts,
            "candidate_contexts": candidate_contexts,
            "owner_contexts": owner_contexts,
            "owner_apps": sorted({row["owner"]["app"] for row in rows}),
            "route_kinds": route_kinds,
            "routed_via_proxy_count": sum(1 for row in rows if row["routed_via_proxy"]),
            "direct_count": sum(1 for row in rows
                                if (row.get("mihomo") or {}).get("route_kind") == "direct"),
            "upload_sum": _sum_number(rows, "upload"),
            "download_sum": _sum_number(rows, "download"),
            "chain_variants": variants,
            "consistent_chains": consistent_chains,
            "consistent_route_kind": consistent_route_kind,
            "warning": _group_warning(consistent_chains, consistent_route_kind),
            "sample_source_ports": [row["local_source_port"] for row in rows[:5]],
        })
    return sorted(groups, key=lambda row: (-row["connection_count"], row["key"]))


def build_report(backend_name: str, config: dict[str, Any],
                 parsed_args: ConnectionArgs) -> dict[str, Any]:
    """Build the full joined connections report."""
    proxy_port = int(config.get("proxy_port", 7890))
    api_base = config.get("api_base", "http://127.0.0.1:9090")
    api_secret = config.get("api_secret", "")

    local_rows = [
        row for row in collect_lsof_connections([])
        if row.source_port != proxy_port
    ]
    api_status: dict[str, Any]
    remote_rows: list[dict[str, Any]] = []
    remote_by_port: dict[int, dict[str, Any]] = {}
    if backend_name != "mihomo":
        api_status = {
            "ok": False, "status": "skipped", "url": None,
            "error": f"connections join needs mihomo backend, current backend is {backend_name}",
            "count": 0,
        }
    else:
        remote_rows, api_status = fetch_mihomo_connections(api_base, api_secret)
        remote_by_port = {
            port: conn for conn in remote_rows
            if (port := _connection_source_port(conn)) is not None
        }

    joined: list[dict[str, Any]] = []
    for row in local_rows:
        via_proxy = row.target_port == proxy_port
        matched = remote_by_port.get(row.source_port) if via_proxy else None
        if matched:
            unmatched_reason = None
        elif not via_proxy:
            unmatched_reason = "not_proxyctl_proxy_port"
        elif api_status["status"] == "skipped":
            unmatched_reason = "backend_not_mihomo"
        elif api_status["ok"] is False:
            unmatched_reason = "mihomo_api_unavailable"
        else:
            unmatched_reason = "no_mihomo_source_port_match"
        item = {
            **row.to_dict(proxy_port),
            "matched": matched is not None,
            "unmatched_reason": unmatched_reason,
            "mihomo": _mihomo_detail(matched),
        }
        if _local_item_matches_filters(item, parsed_args):
            joined.append(item)

    proxy_count = sum(1 for item in joined if item["connects_proxy_port"])
    proxy_owner_rows = _proxy_owner_connections(
        remote_rows, proxy_port, parsed_args)
    proxy_owner_group_rows = _proxy_owner_groups(proxy_owner_rows)
    return {
        "proxy_port": proxy_port,
        "backend": backend_name,
        "apps": parsed_args.app_filters,
        "filters": {
            "app": parsed_args.app_filters,
            "host": parsed_args.host_filters,
            "chain": parsed_args.chain_filters,
            "route": parsed_args.route_filters,
            "preset": parsed_args.preset_filters,
            "agent": parsed_args.agent_filters,
            "query": parsed_args.query_filters,
        },
        "all_apps": parsed_args.all_apps,
        "api": api_status,
        "connections": joined,
        "proxy_owner_connections": proxy_owner_rows,
        "proxy_owner_groups": proxy_owner_group_rows,
        "summary": {
            "local_count": len(joined),
            "proxy_port_count": proxy_count,
            "non_proxy_port_count": len(joined) - proxy_count,
            "all_via_proxy_port": bool(joined) and proxy_count == len(joined),
            "matched_count": sum(1 for item in joined if item["matched"]),
            "unmatched_count": sum(1 for item in joined if not item["matched"]),
            "proxy_owner_connection_count": len(proxy_owner_rows),
            "system_extension_owner_count": sum(
                1 for item in proxy_owner_rows
                if item["owner"]["system_extension_owner"]),
            "routed_via_proxy_count": sum(
                1 for item in proxy_owner_rows if item["routed_via_proxy"]),
            "proxy_owner_group_count": len(proxy_owner_group_rows),
            "inconsistent_proxy_owner_group_count": sum(
                1 for item in proxy_owner_group_rows if item["warning"]),
            "mixed_route_group_count": sum(
                1 for item in proxy_owner_group_rows if item["warning"]),
        },
    }


def emit_human(report: dict[str, Any]) -> None:
    """Render a compact human-readable report via the human renderer."""
    from proxyctl.connections_human import emit_human as render_human

    render_human(report)


def cmd_connections(args: list[str], backend, config: dict[str, Any]) -> None:
    """Entry point for ``proxyctl connections``."""
    parsed_args = parse_args(args)
    report = build_report(backend.name, config, parsed_args)
    if _io.is_json_mode():
        _io.emit_json(_io.envelope("connections", data=report))
        return
    emit_human(report)
