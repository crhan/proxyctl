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
from dataclasses import dataclass
from typing import Any

from proxyctl import _io
from proxyctl._io import maybe_disable_module_colors

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
CYAN = "\033[0;36m"
NC = "\033[0m"

maybe_disable_module_colors(__name__)

DEFAULT_APP_FILTERS = ["Codex", "Claude", "ChatGPT"]
APP_REMOTE_PATTERNS = {
    "codex": re.compile(r"openai|chatgpt", re.I),
    "chatgpt": re.compile(r"openai|chatgpt", re.I),
    "claude": re.compile(r"anthropic|mcp-proxy", re.I),
}
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

    def matches_app(self, filters: list[str]) -> bool:
        """Return whether this row matches any requested ``--app`` filter."""
        if not filters:
            return True
        haystack = " ".join(
            [self.app, os.path.basename(self.process), self.process, self.command]
        ).lower()
        return any(f.lower() in haystack for f in filters)

    def to_dict(self, proxy_port: int) -> dict[str, Any]:
        """Convert the lsof row to the JSON contract used by this command."""
        return {
            "app": self.app,
            "pid": self.pid,
            "fd": self.fd,
            "process": self.process,
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

    def matches_app(self, filters: list[str]) -> bool:
        """Return whether this owner matches any requested ``--app`` filter."""
        if not filters:
            return True
        haystack = " ".join(
            [self.app, os.path.basename(self.process), self.process, self.command]
        ).lower()
        return any(f.lower() in haystack for f in filters)

    def to_dict(self, app_filters: list[str]) -> dict[str, Any]:
        """Convert the owner row to the JSON contract used by this command."""
        return {
            "app": self.app,
            "pid": self.pid,
            "process": self.process,
            "command": self.command,
            "source": "netstat",
            "state": self.state,
            "local_source_port": self.source_port,
            "target_port": self.target_port,
            "matches_app_filter": self.matches_app(app_filters),
            "system_extension_owner": _is_system_extension_owner(self),
            "raw_netstat_line": self.raw_line,
        }


@dataclass
class ConnectionArgs:
    """Parsed arguments for ``proxyctl connections``.

    Attributes:
        app_filters: Process/app filters; empty means all processes.
        all_apps: Whether ``--all`` was explicitly requested.
    """

    app_filters: list[str]
    all_apps: bool = False


def parse_args(args: list[str]) -> ConnectionArgs:
    """Parse ``proxyctl connections`` arguments.

    Supported syntax:
        ``--app NAME`` may appear more than once.
        ``--all`` disables the default AI app filter.
    """
    apps: list[str] = []
    all_apps = False
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg == "--all":
            all_apps = True
            idx += 1
            continue
        if arg == "--app":
            if idx + 1 >= len(args):
                _io.fail("connections --app 需要一个应用名",
                         hint="proxyctl connections --app Codex --json",
                         doc="agent-protocol", code=_io.USAGE,
                         cmd="connections")
            apps.append(args[idx + 1])
            idx += 2
            continue
        _io.fail(f"未识别 connections 参数：{arg}",
                 hints=["proxyctl connections --app Codex --app Claude",
                        "proxyctl connections --all --json",
                        "proxyctl connections --json"],
                 doc="agent-protocol", code=_io.USAGE, cmd="connections")
    if all_apps and apps:
        _io.fail("connections 的 --all 与 --app 不能同时使用",
                 hint="proxyctl connections --all --json",
                 doc="agent-protocol", code=_io.USAGE, cmd="connections")
    return ConnectionArgs(
        app_filters=[] if all_apps else (apps or DEFAULT_APP_FILTERS),
        all_apps=all_apps,
    )


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
    if owner.matches_app(app_filters):
        return "owner_matches_app_filter"
    if _is_system_extension_owner(owner):
        return "system_extension_owner_original_app_hidden"
    if app_filters:
        return "owner_does_not_match_app_filter"
    return "visible_owner"


def _proxy_owner_connections(remote_rows: list[dict[str, Any]], proxy_port: int,
                             app_filters: list[str]) -> list[dict[str, Any]]:
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
        selection_reason = _proxy_owner_selection_reason(conn, owner, app_filters)
        if not selection_reason:
            continue
        detail = _mihomo_detail(conn) or {}
        items.append({
            "local_source_port": port,
            "connects_proxy_port": True,
            "target_port": proxy_port,
            "owner": owner.to_dict(app_filters),
            "attribution": _owner_attribution(owner, app_filters),
            "selection_reason": selection_reason,
            "original_app_visible": owner.matches_app(app_filters),
            "via_proxy_engine": True,
            "routed_via_proxy": detail.get("routed_via_proxy"),
            "mihomo": detail,
        })
    return items


def _proxy_owner_selection_reason(conn: dict[str, Any], owner: ProxyOwner,
                                  app_filters: list[str]) -> str | None:
    """Return why a reverse-owned row belongs in the current report."""
    if owner.matches_app(app_filters):
        return "owner_matches_app_filter"
    if not app_filters:
        return "all_apps"
    metadata = conn.get("metadata") or {}
    target = " ".join([
        str(metadata.get("host") or ""),
        str(metadata.get("destinationIP") or ""),
    ])
    for app_filter in app_filters:
        pattern = APP_REMOTE_PATTERNS.get(app_filter.lower())
        if pattern and pattern.search(target):
            return "host_matches_app_context"
    return None


def build_report(backend_name: str, config: dict[str, Any],
                 parsed_args: ConnectionArgs) -> dict[str, Any]:
    """Build the full joined connections report."""
    proxy_port = int(config.get("proxy_port", 7890))
    api_base = config.get("api_base", "http://127.0.0.1:9090")
    api_secret = config.get("api_secret", "")

    local_rows = [
        row for row in collect_lsof_connections(parsed_args.app_filters)
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
        joined.append({
            **row.to_dict(proxy_port),
            "matched": matched is not None,
            "unmatched_reason": unmatched_reason,
            "mihomo": _mihomo_detail(matched),
        })

    proxy_count = sum(1 for row in local_rows if row.target_port == proxy_port)
    proxy_owner_rows = _proxy_owner_connections(
        remote_rows, proxy_port, parsed_args.app_filters)
    return {
        "proxy_port": proxy_port,
        "backend": backend_name,
        "apps": parsed_args.app_filters,
        "all_apps": parsed_args.all_apps,
        "api": api_status,
        "connections": joined,
        "proxy_owner_connections": proxy_owner_rows,
        "summary": {
            "local_count": len(local_rows),
            "proxy_port_count": proxy_count,
            "non_proxy_port_count": len(local_rows) - proxy_count,
            "all_via_proxy_port": bool(local_rows) and proxy_count == len(local_rows),
            "matched_count": sum(1 for item in joined if item["matched"]),
            "unmatched_count": sum(1 for item in joined if not item["matched"]),
            "proxy_owner_connection_count": len(proxy_owner_rows),
            "system_extension_owner_count": sum(
                1 for item in proxy_owner_rows
                if item["owner"]["system_extension_owner"]),
            "routed_via_proxy_count": sum(
                1 for item in proxy_owner_rows if item["routed_via_proxy"]),
        },
    }


def emit_human(report: dict[str, Any]) -> None:
    """Render a compact human-readable report."""
    api = report["api"]
    summary = report["summary"]
    all_proxy = "yes" if summary["all_via_proxy_port"] else "no"
    print(f"{BOLD}proxyctl connections{NC}  backend={report['backend']} "
          f"proxy_port={report['proxy_port']} all_via_proxy={all_proxy}")
    if not api.get("ok"):
        print(f"  {YELLOW}degraded:{NC} {api.get('error')}")
    if not report["connections"] and not report["proxy_owner_connections"]:
        print(f"  {DIM}no local connections to proxy_port matched filters{NC}")
        return
    if not report["connections"]:
        print(f"  {DIM}no app-owned local sockets matched filters{NC}")
    for item in report["connections"]:
        status = f"{GREEN}matched{NC}" if item["matched"] else f"{YELLOW}unmatched{NC}"
        target = "proxy" if item["connects_proxy_port"] else item["target_host"]
        print(f"  {CYAN}{item['app']}{NC} pid={item['pid']} fd={item['fd']} "
              f"src={item['local_source_port']} -> {target}:{item['target_port']} "
              f"{status}")
        if item["matched"]:
            m = item["mihomo"] or {}
            dest = m.get("host") or m.get("destination_ip") or "?"
            print(f"    dest={dest} rule={m.get('rule') or '?'} "
                  f"payload={m.get('rule_payload') or '-'} "
                  f"chains={','.join(m.get('chains') or []) or '-'} "
                  f"up={m.get('upload')} down={m.get('download')} "
                  f"start={m.get('start') or '-'}")
        else:
            print(f"    reason={item['unmatched_reason']}")
    _emit_proxy_owner_human(report)


def _emit_proxy_owner_human(report: dict[str, Any]) -> None:
    """Render reverse-owned proxy connections from macOS netstat."""
    rows = report["proxy_owner_connections"]
    if not rows:
        return
    print(f"  {BOLD}proxy-owned mihomo connections{NC}")
    for item in rows[:12]:
        owner = item["owner"]
        m = item["mihomo"] or {}
        dest = m.get("host") or m.get("destination_ip") or "?"
        route = m.get("route_kind") or "unknown"
        print(f"  {CYAN}{owner['app']}{NC} pid={owner['pid']} "
              f"src={item['local_source_port']} -> proxy:{report['proxy_port']} "
              f"state={owner['state']} route={route}")
        print(f"    dest={dest} rule={m.get('rule') or '?'} "
              f"payload={m.get('rule_payload') or '-'} "
              f"chains={','.join(m.get('chains') or []) or '-'} "
              f"attribution={item['attribution']}")
    if len(rows) > 12:
        print(f"  {DIM}... {len(rows) - 12} more proxy-owned connections omitted{NC}")


def cmd_connections(args: list[str], backend, config: dict[str, Any]) -> None:
    """Entry point for ``proxyctl connections``."""
    parsed_args = parse_args(args)
    report = build_report(backend.name, config, parsed_args)
    if _io.is_json_mode():
        _io.emit_json(_io.envelope("connections", data=report))
        return
    emit_human(report)
