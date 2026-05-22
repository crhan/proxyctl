"""Tests for `proxyctl traffic` active Mihomo traffic snapshots."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from proxyctl import _io, traffic
from proxyctl.connections import LocalConnection, ProxyOwner


def _conn(source_port: int, host: str, chains: list[str],
          upload: int, download: int) -> dict:
    """Build one fake Mihomo connection row."""
    return {
        "id": f"id-{source_port}",
        "metadata": {
            "sourcePort": str(source_port),
            "host": host,
            "destinationPort": "443",
            "network": "tcp",
            "type": "HTTPS",
        },
        "rule": "DomainSuffix",
        "rulePayload": host.split(".", 1)[-1] if "." in host else host,
        "chains": chains,
        "upload": upload,
        "download": download,
        "start": "2026-05-22T10:00:00+08:00",
    }


def _install_fetch(monkeypatch, rows: list[dict]) -> None:
    """Patch traffic's Mihomo fetcher."""
    monkeypatch.setattr(
        traffic,
        "fetch_mihomo_connections",
        lambda *a, **kw: (
            rows,
            {"ok": True, "status": "ok", "url": "http://x/connections",
             "error": None, "count": len(rows)},
        ),
    )


def _no_local_owners(monkeypatch) -> None:
    """Patch local socket collectors to return no attribution data."""
    monkeypatch.setattr(traffic, "collect_lsof_connections", lambda filters: [])
    monkeypatch.setattr(
        traffic, "collect_netstat_proxy_owners", lambda port, ports: {})


def test_traffic_parse_default_snapshot():
    parsed = traffic.parse_args([])

    assert parsed.subcmd == "snapshot"
    assert parsed.group_by == ["line"]
    assert parsed.filters.has_filters() is False


def test_traffic_parse_by_and_filters():
    parsed = traffic.parse_args([
        "snapshot",
        "--by", "line,app",
        "--chain", "residential-sg",
        "--route", "代理",
        "--preset", "ai",
    ])

    assert parsed.group_by == ["line", "app"]
    assert parsed.filters.chain_filters == ["residential-sg"]
    assert parsed.filters.route_filters == ["代理"]
    assert parsed.filters.preset_filters == ["ai"]


def test_traffic_groups_by_line(monkeypatch):
    _install_fetch(monkeypatch, [
        _conn(1001, "api.anthropic.com",
              ["SG-Residential-01", "residential-sg", "claude"], 100, 900),
        _conn(1002, "chatgpt.com",
              ["OpenAI-01", "proxy"], 50, 450),
        _conn(1003, "intranet.example",
              ["DIRECT"], 5, 10),
    ])
    _no_local_owners(monkeypatch)

    report = traffic.build_snapshot(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        traffic.parse_args([]),
    )

    assert report["scope"] == "active_connections_snapshot"
    assert report["totals"] == {
        "connection_count": 3,
        "upload": 155,
        "download": 1360,
        "total": 1515,
    }
    groups = {item["dimensions"]["line"]: item for item in report["groups"]}
    assert groups["SG-Residential-01"]["total"] == 1000
    assert groups["OpenAI-01"]["total"] == 500
    assert groups["DIRECT"]["total"] == 15


def test_traffic_line_app_attribution_confidence(monkeypatch):
    _install_fetch(monkeypatch, [
        _conn(2001, "api.anthropic.com",
              ["SG-Residential-01", "residential-sg", "claude"], 100, 900),
        _conn(2002, "yuque.antfin.com", ["DIRECT"], 10, 90),
    ])
    monkeypatch.setattr(traffic, "collect_lsof_connections", lambda filters: [])
    monkeypatch.setattr(
        traffic,
        "collect_netstat_proxy_owners",
        lambda port, ports: {
            2001: ProxyOwner(2001, 7890, "ESTABLISHED",
                             "com.antgroup.asp", 47283, "raw"),
            2002: ProxyOwner(2002, 7890, "ESTABLISHED",
                             "DingTalk", 40494, "raw"),
        },
    )

    report = traffic.build_snapshot(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        traffic.parse_args(["--by", "line,app"]),
    )

    dims = [item["dimensions"] for item in report["groups"]]
    assert {"line": "SG-Residential-01",
            "app": "Claude App / Claude CLI"} in dims
    assert {"line": "DIRECT", "app": "DingTalk"} in dims
    by_app = {item["dimensions"]["app"]: item for item in report["groups"]}
    assert by_app["Claude App / Claude CLI"]["app_breakdown"][0][
        "confidence"] == "inferred"
    assert by_app["Claude App / Claude CLI"]["app_breakdown"][0][
        "owner_apps"] == ["com.antgroup.asp"]
    assert by_app["DingTalk"]["app_breakdown"][0]["confidence"] == "socket-owner"


def test_traffic_filters_by_chain(monkeypatch):
    _install_fetch(monkeypatch, [
        _conn(3001, "api.anthropic.com",
              ["SG-Residential-01", "residential-sg", "claude"], 100, 900),
        _conn(3002, "chatgpt.com", ["OpenAI-01", "proxy"], 50, 450),
    ])
    _no_local_owners(monkeypatch)

    report = traffic.build_snapshot(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        traffic.parse_args(["--chain", "residential-sg"]),
    )

    assert [row["line"] for row in report["connections"]] == [
        "SG-Residential-01"
    ]
    assert report["filters"]["chain"] == ["residential-sg"]


def test_traffic_local_process_attribution(monkeypatch):
    _install_fetch(monkeypatch, [
        _conn(4001, "api.openai.com", ["OpenAI-01", "proxy"], 1, 2),
    ])
    monkeypatch.setattr(
        traffic,
        "collect_lsof_connections",
        lambda filters: [
            LocalConnection(123, "Codex", "27u", 4001,
                            "127.0.0.1", 7890, "raw",
                            process="/Applications/Codex.app/Contents/MacOS/Codex")
        ],
    )
    monkeypatch.setattr(
        traffic, "collect_netstat_proxy_owners", lambda port, ports: {})

    report = traffic.build_snapshot(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        traffic.parse_args(["--by", "app"]),
    )

    assert report["groups"][0]["dimensions"] == {"app": "Codex App"}
    assert report["groups"][0]["app_breakdown"][0]["confidence"] == "process"


def test_cmd_traffic_json_outputs_envelope(monkeypatch):
    class _Backend:
        name = "mihomo"

    monkeypatch.setattr(
        traffic,
        "build_snapshot",
        lambda *a, **kw: {
            "scope": "active_connections_snapshot",
            "backend": "mihomo",
            "proxy_port": 7890,
            "group_by": ["line"],
            "filters": {},
            "api": {"ok": True, "status": "ok"},
            "totals": {"connection_count": 0, "upload": 0,
                       "download": 0, "total": 0},
            "groups": [],
            "connections": [],
        },
    )
    _io.set_json_mode(True)
    _io.set_no_color(True)
    out = io.StringIO()
    with redirect_stdout(out):
        traffic.cmd_traffic(["--by", "line"], _Backend(), {})

    env = json.loads(out.getvalue())
    assert env["schema_version"] == _io.SCHEMA_VERSION
    assert env["cmd"] == "traffic"
    assert env["data"]["scope"] == "active_connections_snapshot"
