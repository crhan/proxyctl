"""proxyctl engine - 后端抽象层

支持的后端：
- MihomoBackend (首发支持)
- SingboxBackend (预留)
"""

from .base import Backend
from .mihomo import MihomoBackend
from .singbox import SingboxBackend

__all__ = ['Backend', 'MihomoBackend', 'SingboxBackend']