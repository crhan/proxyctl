"""proxyctl engine.mihomo - Mihomo (Clash Meta) 后端实现"""

import os
import re
from typing import Dict, Any

from .base import Backend


class MihomoBackend(Backend):
    """Mihomo 后端实现

    Mihomo 是 Clash Meta 内核，提供完整的 Clash API 兼容性。
    这是 proxyctl 的首发支持后端。
    """

    def __init__(self, config_dir: str):
        """初始化 Mihomo 后端

        Args:
            config_dir: 配置目录根路径（通常为 ~/.config）
        """
        super().__init__("mihomo", config_dir)
        self.mihomo_dir = os.path.join(config_dir, "mihomo")

    @property
    def label(self) -> str:
        return "system/com.mihomo.tun"

    @property
    def plist(self) -> str:
        return "/Library/LaunchDaemons/com.mihomo.tun.plist"

    @property
    def config_file(self) -> str:
        return os.path.join(self.mihomo_dir, "config.yaml")

    @property
    def cache_file(self) -> str:
        return os.path.join(self.mihomo_dir, "cache.db")

    @property
    def log_file(self) -> str:
        return os.path.join(self.mihomo_dir, "mihomo.log")

    @property
    def api_url(self) -> str:
        """默认 API URL"""
        return "http://127.0.0.1:9090"

    def get_mode(self) -> str:
        """从配置文件读取当前模式

        Returns:
            "tun" - TUN 模式 (auto_route + fakeip)
            "proxy" - 代理模式 (仅端口，redir-host)
            "mixed" - 混合模式
            "unknown" - 无法解析
        """
        try:
            if not os.path.isfile(self.config_file):
                return "unknown"

            text = open(self.config_file).read()

            # 检查 TUN 配置
            tun_m = re.search(r'^tun:\s*\n((?:\s+.*\n)*)', text, re.M)
            tun_block = tun_m.group(0) if tun_m else ""
            tun_on = bool(re.search(r'enable:\s*true', tun_block))
            auto_rt = bool(re.search(r'auto-route:\s*true', tun_block))

            # 检查 DNS 模式
            fakeip = bool(re.search(r'enhanced-mode:\s*fake-ip', text))

            if tun_on and auto_rt and fakeip:
                return "tun"
            elif not auto_rt and not fakeip:
                return "proxy"
            return "mixed"
        except Exception:
            return "unknown"

    def check_config(self) -> bool:
        """验证 Mihomo 配置文件语法

        使用 mihomo 自带的 -t 参数进行语法检查。

        Returns:
            True if valid, False otherwise
        """
        import subprocess
        try:
            result = subprocess.run(
                ["mihomo", "-t", "-f", self.config_file],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_api_url(self) -> str:
        """获取 Mihomo API URL

        从配置文件中读取 external-controller，如果未配置则返回默认值。

        Returns:
            API base URL
        """
        try:
            if not os.path.isfile(self.config_file):
                return self.api_url

            text = open(self.config_file).read()
            m = re.search(r'external-controller:\s*(\S+)', text)
            if m:
                controller = m.group(1)
                # 如果是 :port 格式，补全为 127.0.0.1:port
                if controller.startswith(':'):
                    return f"http://127.0.0.1{controller}"
                elif not controller.startswith('http'):
                    return f"http://{controller}"
                return controller
            return self.api_url
        except Exception:
            return self.api_url