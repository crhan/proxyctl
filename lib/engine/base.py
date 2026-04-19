"""proxyctl engine.base - 后端抽象接口"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class Backend(ABC):
    """代理后端抽象基类

    定义所有后端必须实现的接口，上层工具通过此接口与后端交互，
    实现与具体后端（Mihomo/Sing-box）的解耦。
    """

    def __init__(self, name: str, config_dir: str):
        """初始化后端

        Args:
            name: 后端名称（mihomo/singbox）
            config_dir: 配置目录根路径
        """
        self.name = name
        self.config_dir = config_dir

    @property
    @abstractmethod
    def label(self) -> str:
        """launchd Label 名称"""
        pass

    @property
    @abstractmethod
    def plist(self) -> str:
        """launchd plist 文件路径"""
        pass

    @property
    @abstractmethod
    def config_file(self) -> str:
        """配置文件路径"""
        pass

    @property
    @abstractmethod
    def cache_file(self) -> str:
        """缓存文件路径"""
        pass

    @property
    @abstractmethod
    def log_file(self) -> str:
        """日志文件路径"""
        pass

    @abstractmethod
    def get_mode(self) -> str:
        """获取当前运行模式

        Returns:
            "tun" - TUN 模式（全局接管）
            "proxy" - 代理模式（仅端口）
            "mixed" - 混合模式
            "unknown" - 未知
        """
        pass

    @abstractmethod
    def check_config(self) -> bool:
        """验证配置文件语法

        Returns:
            True if valid, False otherwise
        """
        pass

    @abstractmethod
    def get_api_url(self) -> str:
        """获取 API 基础 URL

        Returns:
            API base URL (e.g., http://127.0.0.1:9090)
        """
        pass

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"