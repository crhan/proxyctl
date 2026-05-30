"""通用连通性基线插件。

提供一组跨所有用户都通用的连通性测试点：
- 海外（走代理）：google / github
- 国内（直连）：baidu
- AI 线路：openai / anthropic

本机特例（discord/telegram、企业内网等）请走用户插件。

代理组：默认提供 "proxy"（绝大多数 mihomo/sing-box 配置都有这个组）。
命名组（如 claude / residential-* 等）和针对它们的 expected_proxy 校验、
出口探测都属本机特例，请放到 ~/.config/proxyctl/plugins/ 的用户插件里，
不要 hardcode 进这个通用基线插件。

出口探测：默认只提供 proxy（走主代理）/ direct（绕代理）两路对照，
足够做分流校验；其它命名出口由用户插件按 name 追加或覆盖。
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
            CheckTarget(name="openai",
                        url="https://api.openai.com/v1/models",
                        mode="proxy"),
            CheckTarget(name="baidu",  url="https://www.baidu.com",  mode="direct"),
            CheckTarget(name="anthropic",
                        url="https://api.anthropic.com/v1/models",
                        mode="proxy"),
        ]

    def check_outbound_probes(self, ctx: dict) -> list[OutboundProbe]:
        return [
            OutboundProbe(name="proxy", mode="proxy", url="https://api.ipify.org"),
            OutboundProbe(name="direct",
                          mode="direct",
                          url="https://myip.ipip.net",
                          extract_re=r"\d+\.\d+\.\d+\.\d+"),
        ]
