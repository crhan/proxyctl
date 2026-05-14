"""通用连通性基线插件。

提供一组跨所有用户都通用的连通性测试点：
- 海外（走代理）：google / github
- 国内（直连）：baidu

本机特例（discord/anthropic/telegram、企业内网等）请走用户插件。

代理组：默认提供 "proxy"（绝大多数 mihomo/sing-box 配置都有这个组）。
其他组（claude / residential-* 等）由用户插件提供。
"""

from proxyctl.core.plugin import CheckTarget, OutboundProbe, Plugin


class ConnectivityBasic(Plugin):
    name = "connectivity-basic"

    def check_groups(self) -> list[str]:
        return ["proxy"]

    def check_targets(self, ctx: dict) -> list[CheckTarget]:
        return [
            CheckTarget(name="google", url="https://www.google.com", mode="proxy"),
            CheckTarget(name="github", url="https://github.com",     mode="proxy"),
            CheckTarget(name="baidu",  url="https://www.baidu.com",  mode="direct"),
        ]

    def check_outbound_probes(self, ctx: dict) -> list[OutboundProbe]:
        # 只提供 proxy 出口探测——direct 探测严重依赖本地网络环境
        # （国内用户用 myip.ipip.net 这种国内服务才稳，海外用户用 ipify 更好），
        # 因此 direct 探测由用户插件提供。
        return [
            OutboundProbe(name="proxy", mode="proxy", url="https://api.ipify.org"),
        ]
