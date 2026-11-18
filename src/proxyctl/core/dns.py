"""DNS endpoint used by Linux mixed-mode diagnostics."""

import sys
from urllib.parse import urlsplit

import yaml


def independent_dns_listener(mode, backend) -> tuple[str, int] | None:
    """Find the dedicated Mihomo listener when the host manages its own DNS."""
    if sys.platform != "linux" or mode != "mixed" or backend.name != "mihomo":
        return None
    try:
        with open(backend.config_file) as stream:
            config = yaml.safe_load(stream)
        tun = config.get("tun", {})
        dns = config.get("dns", {})
        if tun.get("enable", False) is not False or dns.get("enable") is not True:
            return None
        endpoint = urlsplit("//" + dns["listen"])
        host, port = endpoint.hostname, endpoint.port
        if not port or port == 53:
            return None
        if host in (None, "*", "0.0.0.0"):
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        return host, port
    except (AttributeError, KeyError, OSError, TypeError, ValueError, yaml.YAMLError):
        return None
