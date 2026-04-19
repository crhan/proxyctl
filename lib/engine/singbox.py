"""proxyctl engine.singbox - Sing-box 后端实现（预留）

Sing-box 后端支持目前处于预留状态。
如需使用 Sing-box，请参考 MihomoBackend 实现相应接口。
"""

import os
import re
import json
from typing import Dict, Any

from .base import Backend


class SingboxBackend(Backend):
    """Sing-box 后端实现（预留）

    Sing-box 是新一代代理内核，支持多种协议。
    此实现目前处于预留状态，完整功能待开发。
    """

    def __init__(self, config_dir: str):
        """初始化 Sing-box 后端

        Args:
            config_dir: 配置目录根路径（通常为 ~/.config）
        """
        super().__init__("singbox", config_dir)
        self.singbox_dir = os.path.join(config_dir, "sing-box")

    @property
    def label(self) -> str:
        return "system/com.singbox.tun"

    @property
    def plist(self) -> str:
        return "/Library/LaunchDaemons/com.singbox.tun.plist"

    @property
    def config_file(self) -> str:
        return os.path.join(self.singbox_dir, "config.json")

    @property
    def cache_file(self) -> str:
        return os.path.join(self.singbox_dir, "cache.db")

    @property
    def log_file(self) -> str:
        return os.path.join(self.singbox_dir, "sing-box.log")

    @property
    def api_url(self) -> str:
        """默认 API URL"""
        return "http://127.0.0.1:9090"

    def get_mode(self) -> str:
        """从配置文件读取当前模式

        Returns:
            "tun" - TUN 模式 (auto_route + fakeip)
            "proxy" - 代理模式 (仅端口)
            "mixed" - 混合模式
            "unknown" - 无法解析
        """
        try:
            if not os.path.isfile(self.config_file):
                return "unknown"

            cfg = json.load(open(self.config_file))

            # 检查 TUN 配置
            ar = True  # 默认 auto_route 开启
            for ib in cfg.get("inbounds", []):
                if ib.get("type") == "tun":
                    ar = ib.get("auto_route", True)
                    break

            # 检查 fakeip 配置
            fakeip = any(
                r.get("server") == "fakeip-dns"
                for r in cfg.get("dns", {}).get("rules", [])
            )

            if ar and fakeip:
                return "tun"
            elif not ar and not fakeip:
                return "proxy"
            return "mixed"
        except Exception:
            return "unknown"

    def check_config(self) -> bool:
        """验证 Sing-box 配置文件语法

        使用 sing-box 自带的 check 命令进行语法检查。

        Returns:
            True if valid, False otherwise
        """
        import subprocess
        try:
            result = subprocess.run(
                ["sing-box", "check", "-c", self.config_file],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_api_url(self) -> str:
        """获取 Sing-box API URL

        从配置文件中读取 experimental.clash_api.external_controller，
        如果未配置则返回默认值。

        Returns:
            API base URL
        """
        try:
            if not os.path.isfile(self.config_file):
                return self.api_url

            cfg = json.load(open(self.config_file))
            controller = cfg.get("experimental", {}).get("clash_api", {}).get("external_controller", "")
            if controller:
                if controller.startswith(':'):
                    return f"http://127.0.0.1{controller}"
                elif not controller.startswith('http'):
                    return f"http://{controller}"
                return controller
            return self.api_url
        except Exception:
            return self.api_url