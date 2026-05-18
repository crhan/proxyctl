"""订阅状态读取（v0.4.4 起）。

proxyctl 自身不主动拉远端订阅 —— 那是用户脚本（如 update-subscription.sh）的职责。
proxyctl 只读取一份契约 JSON 文件，把状态呈现在 `status` 命令里：

    ~/.config/proxyctl/subscription.json   （路径由 PROXYCTL_SUBSCRIPTION_PATH 覆盖）

更新者（用户的 cron 脚本）每次拉订阅后必须更新此文件——成功或失败都写。
失败快照保留 fetch_ok=false + fetch_error，让 proxyctl 能区分"过期"vs"网络挂"。

Schema v1 字段（全部可选，缺失即 None；订阅服务方不返回某字段时也是 None）：

    schema_version           int      —— 当前 1
    updated_at               str      —— ISO 8601 时间
    fetch_ok                 bool     —— 最近一次拉取是否成功
    fetch_http_status        int      —— HTTP 状态码（0 = 连接级失败）
    fetch_channel            str|None —— "proxy-7890" / "claude-proxy-7891" / "direct"
    fetch_error              str|None —— 失败原因（fetch_ok=false 时填）
    url_host                 str      —— 订阅 URL 的 hostname
    expire_at                str|None —— ISO 8601 套餐到期时间
    expire_days_left         int|None —— 距离到期天数（负数表示已过期）
    traffic_upload_bytes     int|None —— 已上传字节
    traffic_download_bytes   int|None —— 已下载字节
    traffic_used_bytes       int|None —— 总已用 = upload + download
    traffic_total_bytes      int|None —— 套餐总流量
    traffic_used_pct         float|None —— 使用百分比（0.0-100.0+）
    info_nodes               list[str] —— 机场塞进节点列表的元信息行
    node_count               int      —— 主节点数
    relay_node_count         int      —— relay 节点数（可选）
    http_relay_sub_ok        bool     —— relay 订阅是否成功（可选）

设计原则：
- proxyctl 不拉网络、不解析 URL、不知道订阅协议——一切由脚本完成
- 文件不存在时返回 None（不报错，订阅状态非必需）
- 文件损坏时返回 None + warning（防御性，不破坏 status 主流程）
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_PATH = "~/.config/proxyctl/subscription.json"


def status_file_path() -> str:
    """订阅状态契约文件路径。允许 PROXYCTL_SUBSCRIPTION_PATH 覆盖（测试用）。"""
    p = os.environ.get("PROXYCTL_SUBSCRIPTION_PATH") or DEFAULT_PATH
    return os.path.expanduser(p)


def load() -> dict[str, Any] | None:
    """读取订阅状态。文件不存在或损坏时返回 None（无声，不破坏 status 主流程）。"""
    path = status_file_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # 兼容性：未来 schema bump 时仍能读旧文件
    if data.get("schema_version") not in (None, SCHEMA_VERSION):
        return data  # 由调用方决定怎么处理
    return data


def fmt_bytes(n: int | None) -> str:
    """1234567 → '1.18M' / 536870912000 → '500.00G'。None / 0 返回 '?'。"""
    if not n:
        return "?"
    units = (("G", 1024 ** 3), ("M", 1024 ** 2), ("K", 1024))
    for unit, base in units:
        if n >= base:
            return f"{n / base:.2f}{unit}"
    return f"{n}B"


def severity(sub: dict[str, Any]) -> str:
    """根据 fetch / expire / traffic 判定订阅状态严重度。

    Returns:
        "ok"      —— 一切正常
        "warn"    —— 到期 ≤ 7 天 / 流量 ≥ 80% / fetch fail
        "critical" —— 已过期 / 流量超限 / fetch_ok=false
    """
    if not sub.get("fetch_ok", True):
        return "critical"
    days = sub.get("expire_days_left")
    pct = sub.get("traffic_used_pct")
    if days is not None and days < 0:
        return "critical"
    if pct is not None and pct >= 100.0:
        return "critical"
    if days is not None and days <= 7:
        return "warn"
    if pct is not None and pct >= 80.0:
        return "warn"
    return "ok"


def summarize_hints(sub: dict[str, Any]) -> list[str]:
    """给 envelope.hints 拼短句。无问题返回 []。"""
    out: list[str] = []
    if not sub.get("fetch_ok", True):
        err = sub.get("fetch_error") or "unknown error"
        out.append(f"subscription fetch failed: {err}")
        return out
    days = sub.get("expire_days_left")
    if days is not None:
        if days < 0:
            out.append(f"subscription EXPIRED {abs(days)}d ago — renew at {sub.get('url_host', '?')}")
        elif days <= 7:
            out.append(f"subscription expires in {days}d — consider renewing")
    pct = sub.get("traffic_used_pct")
    if pct is not None:
        if pct >= 100.0:
            out.append(f"subscription traffic exhausted ({pct:.1f}%)")
        elif pct >= 80.0:
            out.append(f"subscription traffic at {pct:.1f}%")
    return out


def format_line(sub: dict[str, Any]) -> str:
    """组织一行人类可读的订阅状态摘要。

    示例输出（多种情况）：
        Subscription: expire 2026-08-18 (91d left) · traffic 0.15G/500.00G (0.03%) · n2ray.dev
        Subscription: EXPIRED 3d ago · last fetch HTTP 404 · n2ray.dev
        Subscription: fetch failed — HTTP 500 · n2ray.dev
    """
    parts: list[str] = []

    # 状态部分
    if not sub.get("fetch_ok", True):
        http = sub.get("fetch_http_status")
        err = sub.get("fetch_error") or "unknown"
        parts.append(f"fetch failed (HTTP {http} {err})")
    else:
        days = sub.get("expire_days_left")
        if days is not None and days < 0:
            parts.append(f"EXPIRED {abs(days)}d ago")
        else:
            expire_at = sub.get("expire_at")
            if expire_at:
                # 截取日期部分（去掉时区时间细节）
                date_str = expire_at[:10]
                if days is not None:
                    parts.append(f"expire {date_str} ({days}d left)")
                else:
                    parts.append(f"expire {date_str}")

        used = sub.get("traffic_used_bytes")
        total = sub.get("traffic_total_bytes")
        pct = sub.get("traffic_used_pct")
        if total:
            parts.append(f"traffic {fmt_bytes(used)}/{fmt_bytes(total)} ({pct or 0}%)")

    host = sub.get("url_host")
    if host:
        parts.append(host)

    return " · ".join(parts) if parts else "no data"


def updated_at_human(sub: dict[str, Any]) -> str | None:
    """返回 'updated 2h ago' 这种相对时间，无法解析时返回 None。"""
    ts = sub.get("updated_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(tz=dt.tzinfo) - dt
        sec = int(delta.total_seconds())
        if sec < 60:
            return f"updated {sec}s ago"
        if sec < 3600:
            return f"updated {sec // 60}m ago"
        if sec < 86400:
            return f"updated {sec // 3600}h ago"
        return f"updated {sec // 86400}d ago"
    except (ValueError, TypeError):
        return None
