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
    "p999",
    "cmihomo",
    "f99u",
    "nTCP 127.0.0.1:7890->127.0.0.1:54321 (ESTABLISHED)",
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
    fake_subprocess.set_result(
        ["ps", "-p", "789", "-o", "comm="],
        stdout="/usr/bin/other\n",
    )
    fake_subprocess.set_result(
        ["ps", "-p", "789", "-o", "command="],
        stdout="/usr/bin/other\n",
    )
    fake_subprocess.set_result(
        ["ps", "-p", "999", "-o", "comm="],
        stdout="/opt/homebrew/bin/mihomo\n",
    )
    fake_subprocess.set_result(
        ["ps", "-p", "999", "-o", "command="],
        stdout="/opt/homebrew/bin/mihomo -d ~/.config/mihomo\n",
    )
    fake_subprocess.set_result(
        ["netstat", "-anv", "-p", "tcp"],
        stdout="",
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
        "proxy_owner_connection_count": 0,
        "system_extension_owner_count": 0,
        "routed_via_proxy_count": 0,
        "proxy_owner_group_count": 0,
        "inconsistent_proxy_owner_group_count": 0,
        "mixed_route_group_count": 0,
    }
    row = report["connections"][0]
    assert row["app"] == "Codex"
    assert row["app_contexts"] == ["codex_app"]
    assert row["local_source_port"] == 54321
    assert row["connects_proxy_port"] is True
    assert row["matched"] is True
    assert row["mihomo"]["host"] == "api.openai.com"
    assert row["mihomo"]["rule_payload"] == "openai.com"
    assert row["mihomo"]["chains"] == ["OpenAI", "Proxy"]
    assert row["mihomo"]["route_kind"] == "proxy"
    assert row["mihomo"]["routed_via_proxy"] is True
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
    assert row["app_contexts"] == ["claude_app"]
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


def test_connections_all_ignores_proxy_server_side_socket(fake_subprocess, monkeypatch):
    _install_lsof_and_ps(fake_subprocess)
    _install_connections_api(monkeypatch, {"connections": []})

    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.ConnectionArgs([], all_apps=True),
    )

    assert all(row["local_source_port"] != 7890 for row in report["connections"])
    assert {row["app"] for row in report["connections"]} == {
        "Codex", "Claude", "Other"}


def test_connections_default_filters_are_ai_apps():
    parsed = connections.parse_args([])
    assert parsed.app_filters == [
        "Codex App",
        "Codex CLI",
        "Claude App",
        "Claude CLI",
        "ChatGPT App",
    ]
    assert parsed.all_apps is False


def test_connections_detects_app_and_cli_contexts():
    assert connections._detect_app_contexts(
        "/Applications/Codex.app/Contents/MacOS/Codex"
    ) == ["codex_app"]
    assert connections._detect_app_contexts(
        "/Users/me/.local/bin/codex-aarch64-apple-darwin"
    ) == ["codex_cli"]
    assert connections._detect_app_contexts(
        "/Applications/Claude.app/Contents/MacOS/Claude"
    ) == ["claude_app"]
    assert connections._detect_app_contexts(
        "node /opt/homebrew/bin/claude --dangerously-skip-permissions"
    ) == ["claude_cli"]


def test_connections_legacy_app_filter_expands_to_app_and_cli():
    assert connections._effective_contexts(["Codex"]) == [
        "codex_app",
        "codex_cli",
    ]
    assert connections._effective_contexts(["Claude"]) == [
        "claude_app",
        "claude_cli",
    ]


def test_connections_all_disables_default_filters():
    parsed = connections.parse_args(["--all"])
    assert parsed.app_filters == []
    assert parsed.all_apps is True


def test_connections_parse_linux_ss_output():
    text = (
        'ESTAB 0 0 127.0.0.1:54321 127.0.0.1:7890 '
        'users:(("Codex",pid=123,fd=27))\n'
        'ESTAB 0 0 30.230.81.9:54323 203.0.113.10:443 '
        'users:(("Codex",pid=123,fd=28))\n'
    )
    rows = connections.parse_ss_lines(text)
    assert len(rows) == 2
    assert rows[0].app == "Codex"
    assert rows[0].pid == 123
    assert rows[0].fd == "27"
    assert rows[0].source_port == 54321
    assert rows[0].target_port == 7890
    assert rows[1].target_host == "203.0.113.10"


def test_connections_parse_netstat_proxy_owner():
    text = (
        "tcp4 0 0 127.0.0.1.58380 127.0.0.1.7890 ESTABLISHED "
        "130 322 392384 131072 com.antgroup.asp:47283 "
        "00182 00000008 00000000075bc7fb 00000081 04000900 2 0 000000\n"
        "tcp4 0 0 127.0.0.1.7890 127.0.0.1.58380 ESTABLISHED "
        "748 39 408064 146988 mihomo:77574 "
        "00182 0000000c 00000000075bc7fd 00000080 01000800 2 0 000000\n"
    )

    owners = connections.parse_netstat_proxy_owners(text, 7890, {58380})

    owner = owners[58380]
    assert owner.app == "com.antgroup.asp"
    assert owner.pid == 47283
    assert owner.state == "ESTABLISHED"


def test_connections_reports_network_extension_owner(fake_subprocess, monkeypatch):
    monkeypatch.setattr(connections.sys, "platform", "darwin")
    fake_subprocess.set_result(
        ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpcfn"],
        stdout="",
        returncode=1,
    )
    fake_subprocess.set_result(
        ["netstat", "-anv", "-p", "tcp"],
        stdout=(
            "tcp4 0 0 127.0.0.1.58380 127.0.0.1.7890 ESTABLISHED "
            "130 322 392384 131072 com.antgroup.asp:47283 "
            "00182 00000008 00000000075bc7fb 00000081 04000900 2 0 000000\n"
            "tcp4 0 0 127.0.0.1.58382 127.0.0.1.7890 ESTABLISHED "
            "130 322 392384 131072 com.antgroup.asp:47283 "
            "00182 00000008 00000000075bc7fc 00000081 04000900 2 0 000000\n"
        ),
    )
    fake_subprocess.set_result(
        ["ps", "-p", "47283", "-o", "comm="],
        stdout=(
            "/Library/SystemExtensions/uuid/"
            "com.antgroup.aspect.server.extension.systemextension/"
            "Contents/MacOS/com.antgroup.aspect.server.extension\n"
        ),
    )
    fake_subprocess.set_result(
        ["ps", "-p", "47283", "-o", "command="],
        stdout=(
            "/Library/SystemExtensions/uuid/"
            "com.antgroup.aspect.server.extension.systemextension/"
            "Contents/MacOS/com.antgroup.aspect.server.extension\n"
        ),
    )
    _install_connections_api(monkeypatch, {
        "connections": [{
            "id": "openai",
            "metadata": {
                "sourcePort": "58380",
                "host": "chatgpt.com",
                "destinationPort": "443",
                "network": "tcp",
                "type": "HTTP",
            },
            "rule": "DomainSuffix",
            "rulePayload": "chatgpt.com",
            "chains": ["电信专用(直连)", "proxy-tuic", "proxy"],
            "upload": 130,
            "download": 322,
            "start": "2026-05-22T08:23:41+08:00",
        }, {
            "id": "openai-ab",
            "metadata": {
                "sourcePort": "58382",
                "host": "ab.chatgpt.com",
                "destinationPort": "443",
                "network": "tcp",
                "type": "HTTPS",
            },
            "rule": "DomainSuffix",
            "rulePayload": "chatgpt.com",
            "chains": ["电信专用(直连)", "proxy-tuic", "proxy"],
            "upload": 10,
            "download": 20,
            "start": "2026-05-22T08:23:42+08:00",
        }, {
            "id": "other",
            "metadata": {
                "sourcePort": "58381",
                "host": "example.com",
                "destinationPort": "443",
                "network": "tcp",
                "type": "HTTPS",
            },
            "rule": "Match",
            "rulePayload": "",
            "chains": ["proxy-tuic", "proxy"],
            "upload": 1,
            "download": 2,
            "start": "2026-05-22T08:23:42+08:00",
        }]
    })

    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.ConnectionArgs(["Codex"]),
    )

    assert report["connections"] == []
    assert report["summary"]["proxy_owner_connection_count"] == 2
    assert report["summary"]["system_extension_owner_count"] == 2
    assert report["summary"]["routed_via_proxy_count"] == 2
    assert report["summary"]["proxy_owner_group_count"] == 1
    assert report["summary"]["inconsistent_proxy_owner_group_count"] == 0
    assert report["summary"]["mixed_route_group_count"] == 0
    row = report["proxy_owner_connections"][0]
    assert row["owner"]["app"] == "com.antgroup.asp"
    assert row["candidate_contexts"] == ["codex_app", "codex_cli"]
    assert row["owner"]["system_extension_owner"] is True
    assert row["owner"]["matches_app_filter"] is False
    assert row["attribution"] == "system_extension_owner_original_app_hidden"
    assert row["selection_reason"] == "host_matches_app_context"
    assert row["original_app_visible"] is False
    assert row["routed_via_proxy"] is True
    assert row["mihomo"]["route_kind"] == "proxy"
    assert row["mihomo"]["chains"] == ["电信专用(直连)", "proxy-tuic", "proxy"]
    group = report["proxy_owner_groups"][0]
    assert group["key"] == "chatgpt.com"
    assert group["key_type"] == "rule_payload"
    assert group["connection_count"] == 2
    assert group["hosts"] == ["ab.chatgpt.com", "chatgpt.com"]
    assert group["contexts"] == ["codex_app", "codex_cli"]
    assert group["candidate_contexts"] == ["codex_app", "codex_cli"]
    assert group["owner_contexts"] == []
    assert group["consistent_chains"] is True
    assert group["consistent_route_kind"] is True
    assert group["warning"] is None


def test_connections_proxy_owner_group_warns_on_mixed_chains():
    base = {
        "owner": {"app": "com.antgroup.asp"},
        "routed_via_proxy": True,
        "local_source_port": 1,
        "mihomo": {
            "host": "chatgpt.com",
            "rule_payload": "chatgpt.com",
            "route_kind": "proxy",
            "chains": ["proxy-a", "proxy"],
            "upload": 10,
            "download": 1,
        },
    }
    other = {
        **base,
        "local_source_port": 2,
        "mihomo": {
            **base["mihomo"],
            "host": "ab.chatgpt.com",
            "chains": ["proxy-b", "proxy"],
            "upload": 20,
            "download": 2,
        },
    }

    groups = connections._proxy_owner_groups([base, other])

    group = groups[0]
    assert group["key"] == "chatgpt.com"
    assert group["connection_count"] == 2
    assert group["hosts"] == ["ab.chatgpt.com", "chatgpt.com"]
    assert group["consistent_chains"] is False
    assert group["consistent_route_kind"] is True
    assert group["warning"] == "mixed_chains"
    assert len(group["chain_variants"]) == 2
    assert group["upload_sum"] == 30


def test_connections_human_output_uses_chinese_proxy_owner_labels():
    report = {
        "backend": "mihomo",
        "proxy_port": 7890,
        "api": {"ok": True},
        "summary": {"all_via_proxy_port": False},
        "connections": [],
        "proxy_owner_groups": [{
            "key": "chatgpt.com",
            "key_type": "rule_payload",
            "connection_count": 1,
            "contexts": ["codex_app"],
            "candidate_contexts": ["codex_app"],
            "route_kinds": ["proxy"],
            "chain_variants": [{"chains": ["proxy-tuic", "proxy"]}],
            "hosts": ["chatgpt.com"],
            "sample_source_ports": [58380],
            "warning": None,
        }],
        "proxy_owner_connections": [{
            "owner": {"app": "com.antgroup.asp", "pid": 47283,
                      "state": "ESTABLISHED", "app_contexts": []},
            "local_source_port": 58380,
            "candidate_contexts": ["codex_app"],
            "attribution": "system_extension_owner_original_app_hidden",
            "mihomo": {
                "host": "chatgpt.com",
                "rule": "DomainSuffix",
                "rule_payload": "chatgpt.com",
                "route_kind": "proxy",
                "chains": ["proxy-tuic", "proxy"],
            },
        }],
    }

    _io.set_no_color(True)
    out = io.StringIO()
    with redirect_stdout(out):
        connections.emit_human(report)
    text = out.getvalue()

    assert "默认按目的站点汇总，并在每个目的站点下统计持有进程" in text
    assert "目的站点汇总" in text
    assert "持有进程只是本机 socket owner" in text
    assert "系统扩展也按普通进程统计" in text
    assert "源端口样例: 58380" in text
    assert "持有进程:" in text
    assert "com.antgroup.asp(pid=47283)  1 条" in text
    assert "候选: Codex App" in text
    assert "路由=代理" in text
    assert "链路: proxy-tuic -> proxy" in text
    assert "入口进程=" not in text
    assert "proxy-owned" not in text


def test_connections_lsof_missing_falls_back_to_linux_ss(monkeypatch):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if cmd[0] == "lsof":
            raise FileNotFoundError("lsof")
        if cmd[0] == "ss":
            return type("CP", (), {
                "returncode": 0,
                "stdout": (
                    'ESTAB 0 0 127.0.0.1:54321 127.0.0.1:7890 '
                    'users:(("Codex",pid=123,fd=27))\n'
                ),
                "stderr": "",
            })()
        return type("CP", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(connections.subprocess, "run", fake_run)
    rows = connections.collect_lsof_connections(["Codex"])
    assert calls[0][0] == "lsof"
    assert calls[1][0] == "ss"
    assert rows[0].app == "Codex"


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
