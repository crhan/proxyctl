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


def to_suggestions(sub: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从订阅状态推导结构化 Suggestion 列表（v0.5.0+ 单一事实源）。

    summarize_hints() 和 doctor.suggest 都基于此函数，避免文案漂移。
    sub 为 None 时返回 [subscription.missing] 提示（仅当调用方明确传 None）；
    无 sub 时由调用方决定是否调用本函数。

    Suggestion 字段集见 src/proxyctl/suggest.py 的 Schema v1 文档。
    本函数只填充 id/severity/actor/title/evidence/inspect_command/doc/since，
    不填 fingerprint/first_seen（由 suggest.py 统一计算）。
    """
    out: list[dict[str, Any]] = []
    if sub is None:
        out.append({
            "id": "subscription.missing",
            "severity": "info",
            "actor": "user",
            "title": "未配置订阅快照，状态未知",
            "evidence": {},
            "inspect_command": "proxyctl explain subscription",
            "doc": "suggestion:subscription.missing",
            "since": "0.5.0",
        })
        return out

    host = sub.get("url_host") or "?"

    # fetch 失败：短路输出，过期/流量数据不可信
    if not sub.get("fetch_ok", True):
        err = sub.get("fetch_error") or "unknown error"
        out.append({
            "id": "subscription.last_fetch_error",
            "severity": "warn",
            "actor": "cron",
            "title": f"最近一次订阅拉取失败：{err}",
            "evidence": {
                "fetch_error": err,
                "fetch_http_status": sub.get("fetch_http_status"),
                "url_host": host,
            },
            "inspect_command": "proxyctl explain subscription",
            "doc": "suggestion:subscription.last_fetch_error",
            "since": "0.5.0",
        })
        return out

    # 过期判定（expired 优先于 expiring_soon）
    days = sub.get("expire_days_left")
    if days is not None:
        if days < 0:
            out.append({
                "id": "subscription.expired",
                "severity": "warn",
                "actor": "user",
                "title": f"订阅已过期 {abs(days)} 天",
                "evidence": {
                    "expire_at": sub.get("expire_at"),
                    "days_left": days,
                    "url_host": host,
                },
                "inspect_command": "proxyctl status --json",
                "doc": "suggestion:subscription.expired",
                "since": "0.5.0",
            })
        elif days <= 7:
            out.append({
                "id": "subscription.expiring_soon",
                "severity": "advisory",
                "actor": "user",
                "title": f"订阅 {days} 天内到期",
                "evidence": {
                    "expire_at": sub.get("expire_at"),
                    "days_left": days,
                    "url_host": host,
                },
                "inspect_command": "proxyctl status --json",
                "doc": "suggestion:subscription.expiring_soon",
                "since": "0.5.0",
            })

    # 流量判定（traffic_exhausted > traffic_warn > traffic_high）
    pct = sub.get("traffic_used_pct")
    if pct is not None:
        if pct >= 100.0:
            out.append({
                "id": "subscription.traffic_exhausted",
                "severity": "warn",
                "actor": "user",
                "title": f"套餐流量已用尽（{pct:.1f}%）",
                "evidence": {"used_pct": pct, "url_host": host},
                "inspect_command": "proxyctl status --json",
                "doc": "suggestion:subscription.traffic_exhausted",
                "since": "0.5.0",
            })
        elif pct >= 90.0:
            out.append({
                "id": "subscription.traffic_warn",
                "severity": "warn",
                "actor": "user",
                "title": f"流量已用 {pct:.1f}%",
                "evidence": {"used_pct": pct, "url_host": host},
                "inspect_command": "proxyctl status --json",
                "doc": "suggestion:subscription.traffic_warn",
                "since": "0.5.0",
            })
        elif pct >= 70.0:
            out.append({
                "id": "subscription.traffic_high",
                "severity": "advisory",
                "actor": "user",
                "title": f"流量已用 {pct:.1f}%",
                "evidence": {"used_pct": pct, "url_host": host},
                "inspect_command": "proxyctl status --json",
                "doc": "suggestion:subscription.traffic_high",
                "since": "0.5.0",
            })

    # stale 判定：快照超过 24h 未更新（机场脚本断了）
    updated_at = sub.get("updated_at")
    if updated_at:
        try:
            dt = datetime.fromisoformat(updated_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_sec = (datetime.now(timezone.utc) - dt).total_seconds()
            if age_sec > 24 * 3600:
                hours = int(age_sec // 3600)
                out.append({
                    "id": "subscription.stale",
                    "severity": "info",
                    "actor": "cron",
                    "title": f"订阅快照 {hours}h 未更新",
                    "evidence": {
                        "updated_at": updated_at,
                        "age_hours": hours,
                    },
                    "inspect_command": "proxyctl explain subscription",
                    "doc": "suggestion:subscription.stale",
                    "since": "0.5.0",
                })
        except ValueError:
            pass

    return out


def summarize_hints(sub: dict[str, Any]) -> list[str]:
    """给 envelope.hints 拼短句（向后兼容包装）。

    v0.5.0+ 实现委托给 to_suggestions() 派生短文案；
    保留独立函数签名是为了 status.py 现有调用不破坏。
    """
    out: list[str] = []
    for s in to_suggestions(sub):
        # fetch 失败：保留 "subscription fetch failed: <err>" 历史格式
        if s["id"] == "subscription.last_fetch_error":
            err = s["evidence"].get("fetch_error", "unknown error")
            out.append(f"subscription fetch failed: {err}")
            continue
        if s["id"] == "subscription.expired":
            days = abs(s["evidence"].get("days_left", 0))
            host = s["evidence"].get("url_host", "?")
            out.append(f"subscription EXPIRED {days}d ago — renew at {host}")
            continue
        if s["id"] == "subscription.expiring_soon":
            days = s["evidence"].get("days_left", 0)
            out.append(f"subscription expires in {days}d — consider renewing")
            continue
        if s["id"] == "subscription.traffic_exhausted":
            pct = s["evidence"].get("used_pct", 0.0)
            out.append(f"subscription traffic exhausted ({pct:.1f}%)")
            continue
        if s["id"] in ("subscription.traffic_warn", "subscription.traffic_high"):
            pct = s["evidence"].get("used_pct", 0.0)
            out.append(f"subscription traffic at {pct:.1f}%")
            continue
        # stale / missing 等 info 级不进 hints（保持 0.4.x 行为：hints 仅含 warn/critical）
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
