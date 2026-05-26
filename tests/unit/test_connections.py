"""Tests for `proxyctl connections` local socket ↔ mihomo join."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from contextlib import redirect_stdout

from proxyctl import _io, connections, connections_filters, explain


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


def test_connections_default_has_no_filters():
    parsed = connections.parse_args([])
    assert parsed.app_filters == []
    assert parsed.host_filters == []
    assert parsed.chain_filters == []
    assert parsed.route_filters == []
    assert parsed.preset_filters == []
    assert parsed.agent_filters == []
    assert parsed.query_filters == []
    assert parsed.all_apps is False
    assert parsed.has_filters() is False


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
    assert parsed.has_filters() is False


def test_connections_parse_multidimensional_filters():
    parsed = connections.parse_args([
        "--host", "anthropic.com",
        "--chain", "SG-Residential-01",
        "--line", "residential-sg",
        "--route", "代理",
        "--preset", "ai",
        "--agent", "openclaw",
        "--query", "residential",
        "--filter", "proxy",
    ])

    assert parsed.host_filters == ["anthropic.com"]
    assert parsed.chain_filters == ["SG-Residential-01", "residential-sg"]
    assert parsed.route_filters == ["代理"]
    assert parsed.preset_filters == ["ai"]
    assert parsed.agent_filters == ["openclaw"]
    assert parsed.query_filters == ["residential", "proxy"]
    assert parsed.has_filters() is True


def test_connections_host_filter_matches_mihomo_destination(fake_subprocess,
                                                            monkeypatch):
    _install_lsof_and_ps(fake_subprocess)
    _install_connections_api(monkeypatch, {
        "connections": [{
            "id": "openai",
            "metadata": {
                "sourcePort": "54321",
                "host": "api.openai.com",
                "destinationPort": "443",
                "network": "tcp",
                "type": "HTTPS",
            },
            "rule": "DomainSuffix",
            "rulePayload": "openai.com",
            "chains": ["OpenAI", "Proxy"],
        }]
    })

    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.ConnectionArgs([], host_filters=["openai.com"]),
    )

    assert [row["local_source_port"] for row in report["connections"]] == [54321]
    assert report["filters"]["host"] == ["openai.com"]


def test_connections_chain_filter_matches_mihomo_route(fake_subprocess,
                                                       monkeypatch):
    _install_lsof_and_ps(fake_subprocess)
    _install_connections_api(monkeypatch, {
        "connections": [{
            "id": "openai",
            "metadata": {
                "sourcePort": "54321",
                "host": "api.openai.com",
                "destinationPort": "443",
                "network": "tcp",
                "type": "HTTPS",
            },
            "rule": "DomainSuffix",
            "rulePayload": "openai.com",
            "chains": ["SG-Residential-01", "proxy"],
        }]
    })

    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.ConnectionArgs([], chain_filters=["Residential"]),
    )

    assert [row["local_source_port"] for row in report["connections"]] == [54321]
    assert report["filters"]["chain"] == ["Residential"]


def test_connections_route_filter_matches_mihomo_route_kind(fake_subprocess,
                                                            monkeypatch):
    _install_lsof_and_ps(fake_subprocess)
    _install_connections_api(monkeypatch, {
        "connections": [{
            "id": "openai",
            "metadata": {
                "sourcePort": "54321",
                "host": "api.openai.com",
                "destinationPort": "443",
                "network": "tcp",
                "type": "HTTPS",
            },
            "rule": "DomainSuffix",
            "rulePayload": "openai.com",
            "chains": ["SG-Residential-01", "proxy"],
        }]
    })

    proxy_report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.ConnectionArgs([], route_filters=["代理"]),
    )
    direct_report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.ConnectionArgs([], route_filters=["direct"]),
    )

    assert [row["local_source_port"] for row in proxy_report["connections"]] == [
        54321
    ]
    assert direct_report["connections"] == []


def test_connections_agent_filter_matches_openclaw_process(fake_subprocess,
                                                           monkeypatch):
    fake_subprocess.set_result(
        ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpcfn"],
        stdout="\n".join([
            "p101",
            "cnode",
            "f14u",
            "nTCP 127.0.0.1:60392->127.0.0.1:28789 (ESTABLISHED)",
        ]),
    )
    fake_subprocess.set_result(
        ["ps", "-p", "101", "-o", "comm="],
        stdout="/opt/homebrew/bin/openclaw-node\n",
    )
    fake_subprocess.set_result(
        ["ps", "-p", "101", "-o", "command="],
        stdout="node /opt/homebrew/bin/openclaw-node\n",
    )
    fake_subprocess.set_result(["netstat", "-anv", "-p", "tcp"], stdout="")
    _install_connections_api(monkeypatch, {"connections": []})

    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.ConnectionArgs([], agent_filters=["openclaw"]),
    )

    assert len(report["connections"]) == 1
    assert report["connections"][0]["process"].endswith("openclaw-node")
    assert report["filters"]["agent"] == ["openclaw"]


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
        }, {
            "owner": {"app": "?", "pid": 39707, "process": "/usr/bin/worker",
                      "state": "ESTABLISHED", "app_contexts": []},
            "local_source_port": 58381,
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
    assert "worker(pid=39707)  1 条" in text
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


def _build_openai_report(monkeypatch, fake_subprocess,
                         keywords=None, host_filters=None):
    """Build a report with one OpenAI proxy row and one non-proxy 8080 row."""
    _install_lsof_and_ps(fake_subprocess)
    _install_connections_api(monkeypatch, {
        "connections": [{
            "id": "openai",
            "metadata": {
                "sourcePort": "54321",
                "host": "api.openai.com",
                "destinationIP": "1.2.3.4",
                "destinationPort": "443",
                "network": "tcp",
                "type": "HTTPS",
            },
            "rule": "DomainSuffix",
            "rulePayload": "openai.com",
            "chains": ["OpenAI", "Proxy"],
        }]
    })
    args = connections.ConnectionArgs(
        [], query_filters=list(keywords or []),
        host_filters=list(host_filters or []),
    )
    return connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        args,
    )


def test_positional_arg_single_keyword():
    parsed = connections.parse_args(["codex"])
    assert parsed.query_filters == ["codex"]
    assert parsed.app_filters == []
    assert parsed.has_filters() is True


def test_positional_arg_multiple_and():
    parsed = connections.parse_args(["codex", "443"])
    assert parsed.query_filters == ["codex", "443"]
    assert parsed.has_filters() is True


def test_positional_arg_mixed_with_flag():
    """Positional keywords are independent of which side of a flag they sit on."""
    a = connections.parse_args(["--host", "anthropic.com", "codex"])
    b = connections.parse_args(["codex", "--host", "anthropic.com"])
    assert a.query_filters == ["codex"]
    assert a.host_filters == ["anthropic.com"]
    assert b.query_filters == ["codex"]
    assert b.host_filters == ["anthropic.com"]


def test_positional_unknown_flag_still_rejected(monkeypatch):
    """Anything starting with -- must still be a known flag (no silent typos)."""
    captured = {}

    def fake_fail(msg, **kw):
        captured["msg"] = msg
        raise SystemExit(2)

    monkeypatch.setattr(connections._io, "fail", fake_fail)
    try:
        connections.parse_args(["--unknown", "x"])
    except SystemExit:
        pass
    assert "--unknown" in captured["msg"]


def test_keyword_port_exact_match():
    fields = {"target_port": 443, "source_port": 54321, "pid": 123,
              "target_host": "", "app": "", "process": "", "command": ""}
    assert connections_filters._keyword_dimensions("443", fields) == [
        "target_port"
    ]


def test_keyword_pid_exact_match():
    fields = {"target_port": 443, "source_port": 54321, "pid": 123,
              "target_host": "", "app": "", "process": "", "command": ""}
    assert connections_filters._keyword_dimensions("123", fields) == ["pid"]


def test_keyword_source_port_exact_match():
    fields = {"target_port": 443, "source_port": 54321, "pid": 123,
              "target_host": "", "app": "", "process": "", "command": ""}
    assert connections_filters._keyword_dimensions("54321", fields) == [
        "source_port"
    ]


def test_keyword_process_substring():
    fields = {"target_port": 443, "source_port": 0, "pid": 0,
              "target_host": "", "app": "Codex",
              "process": "/Applications/Codex.app/Contents/MacOS/Codex",
              "command": "/Applications/Codex.app/Contents/MacOS/Codex"}
    dims = connections_filters._keyword_dimensions("codex", fields)
    assert "app" in dims
    assert "process" in dims
    assert "command" in dims


def test_keyword_zero_matches_numeric_only():
    """`0` is digit-only so it does exact compare on numeric fields."""
    fields = {"target_port": 0, "source_port": 0, "pid": 0,
              "target_host": "10.0.0.1", "app": "", "process": "",
              "command": ""}
    dims = connections_filters._keyword_dimensions("0", fields)
    assert "target_port" in dims
    assert "source_port" in dims
    assert "pid" in dims


def test_keyword_ipv6_literal_no_port_confusion():
    """IPv6 host text with embedded numeric segments must not match port 443."""
    fields = {
        "target_port": 8080, "source_port": 60000, "pid": 99,
        "target_host": "[fe80::443]", "app": "", "process": "", "command": "",
    }
    dims = connections_filters._keyword_dimensions("443", fields)
    assert "target_port" not in dims
    assert "source_port" not in dims
    assert "pid" not in dims
    # The IPv6 host text is NOT a numeric field, and `443` is digit-only so
    # the text-field branch is also skipped — wait, _keyword_dimensions does
    # fall through to text fields for digit-only keywords too.
    # Verify the (intentional) behaviour: digit `443` will substring-match
    # the host text `[fe80::443]` because we don't special-case digits there.
    # That is acceptable — match_reasons will list `target_host`, which
    # truthfully explains the hit. The IPv6 risk we wanted to avoid was
    # mis-classifying it as a port number, which we've confirmed above.
    assert "target_host" in dims


def test_keyword_dotted_hostname():
    fields = {"target_port": 0, "source_port": 0, "pid": 0,
              "target_host": "api.openai.com", "app": "", "process": "",
              "command": ""}
    assert "target_host" in connections_filters._keyword_dimensions(
        "openai.com", fields)


def test_row_match_reasons_and_semantics():
    fields = {"target_port": 443, "source_port": 0, "pid": 0,
              "target_host": "api.openai.com", "app": "Codex",
              "process": "", "command": ""}
    reasons = connections_filters.row_match_reasons(["codex", "443"], fields)
    assert reasons == {"codex": ["app"], "443": ["target_port"]}


def test_row_match_reasons_returns_none_on_any_miss():
    fields = {"target_port": 443, "source_port": 0, "pid": 0,
              "target_host": "api.openai.com", "app": "Codex",
              "process": "", "command": ""}
    assert connections_filters.row_match_reasons(
        ["codex", "nope"], fields) is None


def test_row_match_reasons_empty_keywords_returns_empty_dict():
    assert connections_filters.row_match_reasons([], {}) == {}


def test_match_reasons_in_json_output(fake_subprocess, monkeypatch):
    report = _build_openai_report(monkeypatch, fake_subprocess,
                                  keywords=["openai"])
    rows = [r for r in report["connections"] if r["matched"]]
    assert rows, "expected the openai-mapped row to survive filtering"
    assert "match_reasons" in rows[0]
    assert "openai" in rows[0]["match_reasons"]


def test_query_flag_equivalent_to_positional(fake_subprocess, monkeypatch):
    """Both `--query openai` and positional `openai` must keep the same row."""
    via_flag = _build_openai_report(
        monkeypatch, fake_subprocess, keywords=["openai"])
    # Reset fake subprocess + monkeypatch for the second build (fixtures persist).
    _install_lsof_and_ps(fake_subprocess)
    _install_connections_api(monkeypatch, {
        "connections": [{
            "id": "openai",
            "metadata": {
                "sourcePort": "54321",
                "host": "api.openai.com",
                "destinationPort": "443",
                "network": "tcp",
                "type": "HTTPS",
            },
            "rule": "DomainSuffix",
            "rulePayload": "openai.com",
            "chains": ["OpenAI", "Proxy"],
        }]
    })
    via_positional = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.parse_args(["openai"]),
    )
    kept_a = [r["local_source_port"] for r in via_flag["connections"]]
    kept_b = [r["local_source_port"] for r in via_positional["connections"]]
    assert kept_a == kept_b


def test_positional_with_app_flag_is_and(fake_subprocess, monkeypatch):
    """Positional keyword AND --app: both must match or row drops out."""
    _install_lsof_and_ps(fake_subprocess)
    _install_connections_api(monkeypatch, {"connections": []})
    args = connections.parse_args(["--app", "Codex", "nonexistent_kw"])
    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        args,
    )
    # Codex would otherwise match, but `nonexistent_kw` blocks every row.
    assert report["connections"] == []


def test_zero_match_hint_lists_keywords(fake_subprocess, monkeypatch):
    """Human renderer prints attempted keywords + dimensions on 0-match."""
    from proxyctl import connections_human
    fake_subprocess.set_result(
        ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpcfn"], stdout="")
    fake_subprocess.set_result(["netstat", "-anv", "-p", "tcp"], stdout="")
    _install_connections_api(monkeypatch, {"connections": []})
    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.parse_args(["zzz_no_such_thing"]),
    )
    _io.set_no_color(True)
    out = io.StringIO()
    with redirect_stdout(out):
        connections_human.emit_human(report)
    text = out.getvalue()
    assert "zzz_no_such_thing" in text
    assert "target_host" in text  # one of the tried dimensions


def test_highlight_keywords_returns_plain_when_color_off():
    from proxyctl import connections_human
    _io.set_no_color(True)
    assert connections_human._highlight_keywords(
        "hello codex", ["codex"]) == "hello codex"


def test_highlight_keywords_merges_overlapping_spans():
    """Two keywords pointing into the same region must not double-wrap ANSI."""
    from proxyctl import connections_human

    # Force colors on for this assertion regardless of the global state set by
    # earlier tests. should_color() returns False under set_no_color(True);
    # bypass via temporary flip.
    _io.set_no_color(False)
    try:
        import os
        os.environ.pop("NO_COLOR", None)
        # should_color also checks isatty; redirected stdout in pytest is not
        # a tty, so we test the merge logic on the raw helper directly.
        # Skip the should_color gate by temporarily patching the function.
        original = connections_human._io.should_color
        connections_human._io.should_color = lambda *a, **kw: True
        try:
            out = connections_human._highlight_keywords(
                "abc443def", ["443", "44"])
            # Both keywords overlap on the "443" / "44" region; the merged span
            # should be wrapped exactly once.
            assert out.count(connections_human.HL_START) == 1
            assert out.count(connections_human.HL_END) == 1
        finally:
            connections_human._io.should_color = original
    finally:
        _io.set_no_color(True)


def test_row_match_reasons_skips_whitespace_only_keyword():
    """A keyword that is empty after .strip() must not block the match."""
    fields = {"target_port": 443, "source_port": 0, "pid": 0,
              "target_host": "api.openai.com", "app": "Codex",
              "process": "", "command": ""}
    # ``" "`` is whitespace-only — row_match_reasons must treat it as a
    # no-op, not as "an unmatchable keyword that drops the row".
    assert connections_filters.row_match_reasons(
        ["  ", "openai"], fields) == {"openai": ["target_host"]}


def test_keyword_with_embedded_whitespace_matches_substring():
    """A keyword with spaces must still substring-match the relevant field."""
    fields = {"target_port": 0, "source_port": 0, "pid": 0,
              "target_host": "", "app": "",
              "process": "",
              "command": "node /opt/homebrew/bin/openclaw-node --foo"}
    dims = connections_filters._keyword_dimensions(
        "/bin/openclaw", fields)
    assert "command" in dims


def test_zero_match_hint_not_emitted_when_only_structural_filter(
        fake_subprocess, monkeypatch):
    """0-match hint should only fire when keywords were provided.

    With only --app filters and no rows match, the renderer prints the
    short 'no rows' message but must NOT print the keyword/dimension hint
    (there are no keywords to advertise).
    """
    from proxyctl import connections_human
    fake_subprocess.set_result(
        ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpcfn"], stdout="")
    fake_subprocess.set_result(["netstat", "-anv", "-p", "tcp"], stdout="")
    _install_connections_api(monkeypatch, {"connections": []})
    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.parse_args(["--app", "Codex"]),
    )
    _io.set_no_color(True)
    out = io.StringIO()
    with redirect_stdout(out):
        connections_human.emit_human(report)
    text = out.getvalue()
    assert "尝试过的维度" not in text
    assert "匹配关键字" not in text


def test_verbose_flag_parses_into_args():
    parsed = connections.parse_args(["--verbose", "claude"])
    assert parsed.verbose is True
    assert parsed.query_filters == ["claude"]
    # Order-independent.
    parsed2 = connections.parse_args(["claude", "--verbose"])
    assert parsed2.verbose is True
    assert parsed2.query_filters == ["claude"]


def test_verbose_human_view_expands_socket_detail(fake_subprocess, monkeypatch):
    """With --verbose, each destination group should expand socket details
    including process/command and (for proxy-owner rows) Mihomo metadata."""
    from proxyctl import connections_human
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
        ),
    )
    fake_subprocess.set_result(
        ["ps", "-p", "47283", "-o", "comm="],
        stdout="/Library/SystemExtensions/.../com.antgroup.aspect.server.extension\n",
    )
    fake_subprocess.set_result(
        ["ps", "-p", "47283", "-o", "command="],
        stdout="/Library/SystemExtensions/.../com.antgroup.aspect.server.extension --runas-extension\n",
    )
    _install_connections_api(monkeypatch, {
        "connections": [{
            "id": "claude",
            "metadata": {
                "sourcePort": "58380",
                "host": "claude.ai",
                "destinationIP": "1.2.3.4",
                "destinationPort": "443",
                "network": "tcp",
                "type": "HTTPS",
            },
            "rule": "DomainSuffix",
            "rulePayload": "claude.ai",
            "chains": ["TW-Residential-01", "claude"],
            "upload": 11,
            "download": 22,
            "start": "2026-05-23T10:00:00Z",
        }]
    })
    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.parse_args(["claude", "--verbose"]),
    )
    _io.set_no_color(True)
    out = io.StringIO()
    with redirect_stdout(out):
        connections_human.emit_human(report)
    text = out.getvalue()
    assert "socket 明细" in text
    assert "TW-Residential-01" in text
    assert "claude.ai" in text
    # match_reasons line is emitted under verbose because keyword `claude`
    # hit at least one dimension.
    assert "命中:" in text


def test_verbose_off_by_default_keeps_summary_only(fake_subprocess, monkeypatch):
    from proxyctl import connections_human
    monkeypatch.setattr(connections.sys, "platform", "darwin")
    fake_subprocess.set_result(
        ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpcfn"],
        stdout="", returncode=1,
    )
    fake_subprocess.set_result(
        ["netstat", "-anv", "-p", "tcp"],
        stdout=(
            "tcp4 0 0 127.0.0.1.58380 127.0.0.1.7890 ESTABLISHED "
            "130 322 392384 131072 com.antgroup.asp:47283 "
            "00182 00000008 00000000075bc7fb 00000081 04000900 2 0 000000\n"
        ),
    )
    fake_subprocess.set_result(
        ["ps", "-p", "47283", "-o", "comm="], stdout="ext\n")
    fake_subprocess.set_result(
        ["ps", "-p", "47283", "-o", "command="], stdout="ext\n")
    _install_connections_api(monkeypatch, {
        "connections": [{
            "id": "claude",
            "metadata": {
                "sourcePort": "58380", "host": "claude.ai",
                "destinationIP": "1.2.3.4", "destinationPort": "443",
                "network": "tcp", "type": "HTTPS",
            },
            "rule": "DomainSuffix", "rulePayload": "claude.ai",
            "chains": ["TW-Residential-01", "claude"],
        }]
    })
    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090"},
        connections.parse_args(["claude"]),  # no --verbose
    )
    _io.set_no_color(True)
    out = io.StringIO()
    with redirect_stdout(out):
        connections_human.emit_human(report)
    text = out.getvalue()
    # The "socket 明细：" header (with full-width colon) marks the verbose
    # section. The static explanation text mentions "socket 明细" without
    # the colon — match the colon to avoid that false positive.
    assert "socket 明细：" not in text
    # Likewise, the per-socket lines start with "[1]" / "[2]" indices.
    assert "[1]" not in text


def test_format_bytes_renders_human_units():
    from proxyctl import connections_human as h
    assert h._format_bytes(0) == "0 B"
    assert h._format_bytes(512) == "512 B"
    assert h._format_bytes(8228).endswith("KiB")  # ~8.0 KiB
    assert h._format_bytes(71126).endswith("KiB")  # ~69.5 KiB
    assert h._format_bytes(1024 * 1024 * 3).startswith("3.0 MiB")
    # Non-numeric falls back gracefully.
    assert h._format_bytes(None) == "-"
    assert h._format_bytes("abc") == "-"
    assert h._format_bytes("4096") == "4.0 KiB"  # numeric string


def test_format_duration_since_basic():
    from proxyctl import connections_human as h
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 5, 23, 18, 0, 0, tzinfo=timezone.utc)
    # 45 seconds ago
    started = now - timedelta(seconds=45)
    assert h._format_duration_since(started.isoformat(), now=now) == "45s 前"
    # 5 min 20 sec ago
    started = now - timedelta(minutes=5, seconds=20)
    assert h._format_duration_since(started.isoformat(), now=now) == "5m20s 前"
    # 1 hour 30 min ago
    started = now - timedelta(hours=1, minutes=30)
    assert h._format_duration_since(started.isoformat(), now=now) == "1h30m 前"
    # 2 days 3 hours ago
    started = now - timedelta(days=2, hours=3)
    assert h._format_duration_since(started.isoformat(), now=now) == "2d03h 前"


def test_format_duration_since_handles_garbage():
    from proxyctl import connections_human as h
    assert h._format_duration_since("") == ""
    assert h._format_duration_since(None) == ""
    assert h._format_duration_since("not-a-date") == ""


def test_format_start_includes_relative_tag():
    from proxyctl import connections_human as h
    out = h._format_start("2026-05-23T17:20:27.854916+08:00")
    # The relative bit is locale-independent; just confirm format shape.
    assert out.startswith("2026-05-23T17:20:27.854916+08:00 (")
    assert out.endswith("前)")
    assert h._format_start("") == "-"
    assert h._format_start(None) == "-"


def test_summarize_history_events_aggregates_totals():
    events = [
        {"upload": 100, "download": 200, "sample_ts": "2026-05-23T17:00:00Z",
         "state_key": "conn1", "attribution": {"app": "Codex App"}},
        {"upload": 50, "download": 150, "sample_ts": "2026-05-23T17:05:00Z",
         "state_key": "conn1", "attribution": {"app": "Codex App"}},
        {"upload": 25, "download": 75, "sample_ts": "2026-05-23T17:10:00Z",
         "state_key": "conn2", "attribution": {"app": "Claude App"}},
    ]
    summary = connections._summarize_history_events(events)
    assert summary["upload_total"] == 175
    assert summary["download_total"] == 425
    assert summary["event_count"] == 3
    assert summary["connection_count"] == 2
    assert summary["owner_apps"] == ["Claude App", "Codex App"]
    assert summary["first_seen"] == "2026-05-23T17:00:00Z"
    assert summary["last_seen"] == "2026-05-23T17:10:00Z"


def test_attach_history_with_no_events_returns_empty_status(tmp_path,
                                                            monkeypatch):
    config = {"traffic_store_dir": str(tmp_path)}
    status = connections._attach_history_to_rows(config, [], [])
    assert status["loaded"] is True
    assert status["event_count"] == 0
    assert status["exists"] is False


def test_attach_history_matches_rows_by_host(tmp_path):
    import json
    events_path = tmp_path / "traffic_events.ndjson"
    events_path.write_text("\n".join([
        json.dumps({"host": "claude.ai", "upload": 100, "download": 200,
                    "sample_ts": "2026-05-23T17:00:00Z",
                    "state_key": "c1",
                    "attribution": {"app": "Claude App"}}),
        json.dumps({"host": "claude.ai", "upload": 50, "download": 50,
                    "sample_ts": "2026-05-23T17:05:00Z",
                    "state_key": "c1",
                    "attribution": {"app": "Claude App"}}),
        json.dumps({"host": "openai.com", "upload": 999, "download": 999,
                    "sample_ts": "2026-05-23T17:01:00Z",
                    "state_key": "x1",
                    "attribution": {"app": "Codex App"}}),
    ]) + "\n")
    config = {"traffic_store_dir": str(tmp_path)}
    owner_rows = [{"mihomo": {"host": "claude.ai"}, "owner": {}}]
    status = connections._attach_history_to_rows(config, [], owner_rows)
    assert status["event_count"] == 3
    assert "history" in owner_rows[0]
    history = owner_rows[0]["history"]
    assert history["upload_total"] == 150
    assert history["download_total"] == 250
    assert history["event_count"] == 2
    assert history["owner_apps"] == ["Claude App"]


def test_verbose_human_shows_history_when_available(tmp_path, fake_subprocess,
                                                    monkeypatch):
    """End-to-end: verbose + non-empty traffic events => history block visible."""
    from proxyctl import connections_human
    import json
    events_path = tmp_path / "traffic_events.ndjson"
    events_path.write_text(json.dumps({
        "host": "claude.ai", "upload": 1024, "download": 2048,
        "sample_ts": "2026-05-23T17:00:00Z", "state_key": "c1",
        "attribution": {"app": "Claude App"},
    }) + "\n")
    monkeypatch.setattr(connections.sys, "platform", "darwin")
    fake_subprocess.set_result(
        ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpcfn"],
        stdout="", returncode=1,
    )
    fake_subprocess.set_result(
        ["netstat", "-anv", "-p", "tcp"],
        stdout=(
            "tcp4 0 0 127.0.0.1.58380 127.0.0.1.7890 ESTABLISHED "
            "130 322 392384 131072 com.antgroup.asp:47283 "
            "00182 00000008 00000000075bc7fb 00000081 04000900 2 0 000000\n"
        ),
    )
    fake_subprocess.set_result(
        ["ps", "-p", "47283", "-o", "comm="], stdout="ext\n")
    fake_subprocess.set_result(
        ["ps", "-p", "47283", "-o", "command="], stdout="ext\n")
    _install_connections_api(monkeypatch, {
        "connections": [{
            "id": "claude",
            "metadata": {
                "sourcePort": "58380", "host": "claude.ai",
                "destinationIP": "1.2.3.4", "destinationPort": "443",
                "network": "tcp", "type": "HTTPS",
            },
            "rule": "DomainSuffix", "rulePayload": "claude.ai",
            "chains": ["TW-Residential-01", "claude"],
            "upload": 11, "download": 22,
            "start": "2026-05-23T17:20:27.854916+08:00",
        }]
    })
    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090",
         "traffic_store_dir": str(tmp_path)},
        connections.parse_args(["claude", "--verbose"]),
    )
    _io.set_no_color(True)
    out = io.StringIO()
    with redirect_stdout(out):
        connections_human.emit_human(report)
    text = out.getvalue()
    assert "历史: 累计" in text
    assert "1.0 KiB" in text  # upload_total formatted
    assert "2.0 KiB" in text  # download_total formatted
    assert "Claude App" in text


def test_verbose_human_emits_no_data_hint_when_traffic_empty(tmp_path,
                                                             fake_subprocess,
                                                             monkeypatch):
    from proxyctl import connections_human
    fake_subprocess.set_result(
        ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpcfn"], stdout="")
    fake_subprocess.set_result(["netstat", "-anv", "-p", "tcp"], stdout="")
    _install_connections_api(monkeypatch, {"connections": []})
    # Empty store dir → no events file → emit_history_status_hint should
    # print the "先跑 proxyctl traffic watch" guidance.
    report = connections.build_report(
        "mihomo",
        {"proxy_port": 7890, "api_base": "http://127.0.0.1:9090",
         "traffic_store_dir": str(tmp_path)},
        connections.parse_args(["--verbose"]),
    )
    _io.set_no_color(True)
    out = io.StringIO()
    with redirect_stdout(out):
        connections_human.emit_human(report)
    text = out.getvalue()
    assert "proxyctl traffic watch" in text


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
