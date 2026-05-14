"""测试 builtin_plugins.{connectivity_basic, corp_network}。

这两个插件是纯配置驱动，无副作用，可直接断言返回值。
"""

from __future__ import annotations

from proxyctl.builtin_plugins.connectivity_basic import ConnectivityBasic
from proxyctl.builtin_plugins.corp_network import CorpNetwork


# ────────────────────────────────────────────────────────────────────────────
# ConnectivityBasic
# ────────────────────────────────────────────────────────────────────────────

def test_connectivity_basic_metadata():
    p = ConnectivityBasic()
    assert p.name == "connectivity-basic"
    assert p.check_groups() == ["proxy"]


def test_connectivity_basic_targets():
    p = ConnectivityBasic()
    targets = p.check_targets({})
    names = {t.name for t in targets}
    assert names == {"google", "github", "baidu"}
    modes = {t.name: t.mode for t in targets}
    assert modes["google"] == "proxy"
    assert modes["github"] == "proxy"
    assert modes["baidu"] == "direct"


def test_connectivity_basic_probes_only_proxy_out():
    """direct 探测留给用户插件，core 只提供 proxy 出口。"""
    p = ConnectivityBasic()
    probes = p.check_outbound_probes({})
    assert len(probes) == 1
    assert probes[0].name == "proxy"
    assert probes[0].mode == "proxy"


# ────────────────────────────────────────────────────────────────────────────
# CorpNetwork: 默认不启用 / 在企业网才生效
# ────────────────────────────────────────────────────────────────────────────

def test_corp_network_disabled_without_config():
    p = CorpNetwork({})
    assert p.check_targets({"corp_net": True}) == []


def test_corp_network_disabled_when_not_in_corp_net():
    """server 配了但 ctx.corp_net=False → 跳过。"""
    p = CorpNetwork({
        "corp_dns": {
            "server": "10.0.0.53",
            "test_domain": "wiki.corp.example.com",
        }
    })
    assert p.check_targets({"corp_net": False}) == []


def test_corp_network_enabled_in_corp_net():
    p = CorpNetwork({
        "corp_dns": {
            "server": "10.0.0.53",
            "test_domain": "wiki.corp.example.com",
            "check_targets": [
                {"name": "gw", "url": "tcp:10.0.0.1:22", "mode": "tcp"},
                {"name": "wiki", "url": "https://wiki", "mode": "direct"},
            ],
        }
    })
    out = p.check_targets({"corp_net": True})
    assert len(out) == 3  # corp-dns + 两个自定义
    names = {t.name for t in out}
    assert "corp-dns" in names
    assert "gw" in names
    assert "wiki" in names

    dns_t = next(t for t in out if t.name == "corp-dns")
    assert dns_t.url == "dns:10.0.0.53:wiki.corp.example.com"
    assert dns_t.mode == "dns"


def test_corp_network_missing_test_domain_omits_dns_target():
    p = CorpNetwork({
        "corp_dns": {
            "server": "10.0.0.53",
            "check_targets": [],
        }
    })
    out = p.check_targets({"corp_net": True})
    # server 有但 test_domain 缺 → 不加 corp-dns
    names = {t.name for t in out}
    assert "corp-dns" not in names


def test_corp_network_default_target_fields():
    """check_targets 缺字段时填默认值。"""
    p = CorpNetwork({
        "corp_dns": {
            "server": "10.0.0.53",
            "test_domain": "x.corp",
            "check_targets": [
                {},   # 全部缺
            ],
        }
    })
    out = p.check_targets({"corp_net": True})
    extra = [t for t in out if t.name != "corp-dns"]
    assert len(extra) == 1
    assert extra[0].name == "corp-target"
    assert extra[0].url == ""
    assert extra[0].mode == "direct"
