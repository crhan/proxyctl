"""proxyctl — Proxy configuration lifecycle management."""

from importlib.metadata import PackageNotFoundError, version as _v

try:
    __version__ = _v("proxyctl")
except PackageNotFoundError:  # editable / source tree without metadata
    __version__ = "unknown"
