"""Tests for `proxyctl connections` local socket ↔ mihomo join."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from contextlib import redirect_stdout

from proxyctl import _io, connections, explain


LSOF_OUTPUT = "\n".join([
    "p123",
    "cCodex",
    "f27u",
    "nTCP 127.0.0.1:54321->127.0.0.1:7890 (ESTABLISHED)",
    "f28u",
    "nTCP 30.230.81.9:54323->203.0.113.10:443 (ESTABLISHED)",
    "p456",
    "cClaude",
    "f31u",
    "nTCP 127.0.0.1:54322->127.0.0.1:7890 (ESTABLISHED)",
    "p789",
    "cOther",
    "f9u",
    "nTCP 127.0.0.1:60000->127.0.0.1:8080 (ESTABLISHED)",
])


def _install_lsof_and_ps(fake_subprocess):
    fake_subprocess.set_result(
        ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpcfn"],
        stdout=LSOF_OUTPUT,
    )
    fake_subprocess.set_result(
        ["ps", "-p", "123", "-o", "comm="],
        stdout="/Applications/Codex.app/Contents/MacOS/Codex\n",
    )
    fake_subprocess.set_result(
        ["ps", "-p", "123", "-o", "command="],
        stdout="/Applications/Codex.app/Contents/MacOS/Codex --type=renderer\n",
    )
    fake_subprocess.set_result(
        ["ps", "-p", "456", "-o", "comm="],
        stdout="/Applications/Claude.app/Contents/MacOS/Claude\n",
    )
    fake_subprocess.set_result(
        ["ps", "-p", "456", "-o", "command="],
        stdout="/Applications/Claude.app/Contents/MacOS/Claude\n",
    )


def _install_connections_api(monkeypatch, payload: dict):
    body = json.dumps(payload).encode()

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return body

    class _Opener:
        def open(self, req, timeout=1.0):
            assert req.full_url == "http://127.0.0.1:9090/connections"
            return _Resp()

    monkeypatch.setattr(
        connections.urllib.request, "build_opener", lambda *a, **kw: _Opener())


def test_connections_command_metadata_registered():
    by_name = {item["name"]: item for item in explain.COMMANDS_META}
    meta = by_name["connections"]
    assert meta["supports_json"] is True
    assert meta["side_effects"] == []
    assert meta["needs_sudo"] is False


def test_connections_join_matches_proxy_port_source_port(fake_subprocess, monkeypatch):
    _install_lsof_and_ps(fake_subprocess)
    _install_connections_api(monkeypatch, {
        "connections": [{
            "id": "abc",
            "metadata": {
                "sourcePort": "54321",
                "host": "api.openai.com",
                "destinationIP": "1.2.3.4",
                "destinationPort": "443",
                "network": "tcp",
                "type": "HTTP",
            },
            "rule": "DomainSuffix",
            "rulePayload": "openai.com",
            "chains": ["OpenAI", "Proxy"],
            "upload": 11,
            "download": 22,
            "start": "2026-05-21T10:00:00Z",
        }]
    })

    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090",
         "api_secret": "secret"},
        connections.ConnectionArgs(["Codex"]),
    )

    assert report["summary"] == {
        "local_count": 2,
        "proxy_port_count": 1,
        "non_proxy_port_count": 1,
        "all_via_proxy_port": False,
        "matched_count": 1,
        "unmatched_count": 1,
    }
    row = report["connections"][0]
    assert row["app"] == "Codex"
    assert row["local_source_port"] == 54321
    assert row["connects_proxy_port"] is True
    assert row["matched"] is True
    assert row["mihomo"]["host"] == "api.openai.com"
    assert row["mihomo"]["rule_payload"] == "openai.com"
    assert row["mihomo"]["chains"] == ["OpenAI", "Proxy"]
    direct = report["connections"][1]
    assert direct["connects_proxy_port"] is False
    assert direct["matched"] is False
    assert direct["unmatched_reason"] == "not_proxyctl_proxy_port"


def test_connections_unmatched_when_source_port_missing(fake_subprocess, monkeypatch):
    _install_lsof_and_ps(fake_subprocess)
    _install_connections_api(monkeypatch, {"connections": []})

    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.ConnectionArgs(["Claude"]),
    )

    row = report["connections"][0]
    assert row["app"] == "Claude"
    assert row["matched"] is False
    assert row["unmatched_reason"] == "no_mihomo_source_port_match"
    assert row["mihomo"] is None


def test_connections_api_failure_degrades_to_unmatched(fake_subprocess, monkeypatch):
    _install_lsof_and_ps(fake_subprocess)

    class _Opener:
        def open(self, req, timeout=1.0):
            raise urllib.error.URLError("controller down")

    monkeypatch.setattr(
        connections.urllib.request, "build_opener", lambda *a, **kw: _Opener())

    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.ConnectionArgs(["Codex"]),
    )

    assert report["api"]["status"] == "error"
    assert report["api"]["ok"] is False
    assert report["connections"][0]["unmatched_reason"] == "mihomo_api_unavailable"
    assert report["connections"][1]["unmatched_reason"] == "not_proxyctl_proxy_port"


def test_connections_non_mihomo_backend_degrades_without_api(fake_subprocess, monkeypatch):
    _install_lsof_and_ps(fake_subprocess)

    def _boom(*args, **kwargs):
        raise AssertionError("non-mihomo backend must not call /connections")

    monkeypatch.setattr(connections, "fetch_mihomo_connections", _boom)
    report = connections.build_report(
        "singbox",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.ConnectionArgs(["Codex"]),
    )

    assert report["api"]["status"] == "skipped"
    assert report["api"]["ok"] is False
    assert report["connections"][0]["unmatched_reason"] == "backend_not_mihomo"
    assert report["connections"][1]["unmatched_reason"] == "not_proxyctl_proxy_port"


def test_connections_default_filters_are_ai_apps():
    parsed = connections.parse_args([])
    assert parsed.app_filters == ["Codex", "Claude", "ChatGPT"]
    assert parsed.all_apps is False


def test_connections_all_disables_default_filters():
    parsed = connections.parse_args(["--all"])
    assert parsed.app_filters == []
    assert parsed.all_apps is True


def test_cmd_connections_json_outputs_envelope(monkeypatch):
    class _Backend:
        name = "mihomo"

    monkeypatch.setattr(connections, "build_report",
                        lambda *a, **kw: {
                            "proxy_port": 7890,
                            "backend": "mihomo",
                            "apps": ["Codex"],
                            "api": {"ok": True, "status": "ok"},
                            "connections": [],
                            "summary": {"local_count": 0,
                                        "matched_count": 0,
                                        "unmatched_count": 0},
                        })
    _io.set_json_mode(True)
    _io.set_no_color(True)
    out = io.StringIO()
    with redirect_stdout(out):
        connections.cmd_connections(["--app", "Codex"], _Backend(), {})
    env = json.loads(out.getvalue())
    assert env["schema_version"] == _io.SCHEMA_VERSION
    assert env["cmd"] == "connections"
    assert env["ok"] is True
    assert env["data"]["apps"] == ["Codex"]
