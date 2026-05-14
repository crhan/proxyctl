"""企业内网通用插件（只在 config.corp_dns.server 配置时启用）。

把 corp_dns 相关的检查点（企业 DNS 可达性 + check_targets 里列的内网目标）
从 check.py 里剥离，让 core 完全不关心 corp_dns 这个 schema。

config schema（保留在 config.yaml 里供企业用户填）：

  corp_dns:
    server: "10.0.0.53"        # 企业 DNS 服务器 IP
    test_domain: "wiki.corp.example.com"  # 用来 dig 测试的内网域名
    ip_prefix: "10."           # 用于判定"是否在企业网"的 IP 前缀
    check_targets:
      - {url: "tcp:10.0.0.1:22", name: "gw", mode: "tcp"}
      - {url: "https://wiki.corp.example.com", name: "wiki", mode: "direct"}
"""

from proxyctl.core.plugin import CheckTarget, Plugin


class CorpNetwork(Plugin):
    name = "corp-network"

    def _corp(self) -> dict:
        return (self.config.get("corp_dns") or {})

    def _enabled(self) -> bool:
        c = self._corp()
        return bool(c.get("server"))

    def check_targets(self, ctx: dict) -> list[CheckTarget]:
        if not self._enabled():
            return []
        if not ctx.get("corp_net"):
            return []   # 不在企业网时跳过

        c = self._corp()
        targets: list[CheckTarget] = []

        # 1) 企业 DNS 可达性（用 dig 测一个内网域名）
        server = c.get("server", "")
        test_domain = c.get("test_domain", "")
        if server and test_domain:
            targets.append(CheckTarget(
                name="corp-dns",
                url=f"dns:{server}:{test_domain}",
                mode="dns",
            ))

        # 2) 用户在 corp_dns.check_targets 里列的目标
        for t in (c.get("check_targets") or []):
            targets.append(CheckTarget(
                name=t.get("name", "corp-target"),
                url=t.get("url", ""),
                mode=t.get("mode", "direct"),
            ))

        return targets
