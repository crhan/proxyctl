"""proxyctl.suggest — Doctor 引导建议引擎（v0.5.0+）。

设计立场：
  - doctor 仍是 5 项布尔 score + ≤500ms + 纯只读；suggestions 与 score 解耦
  - 永不影响 exit code（21 条 warn 全亮也是 exit 0）
  - 全部规则纯本地文件 / 本地 API 读取，零外网
  - 永不自动修复——只播报，rm/download 留给独立写命令
  - 数据/文案单一事实源（subscription.to_suggestions → status / doctor 共用）

Suggestion Schema v1:
    {
      "id":              str   稳定枚举 "<area>.<situation>"
      "severity":        str   "info" | "advisory" | "warn"
      "actor":           str   "user" | "agent" | "cron" | "engine"
      "title":           str   人类一句话
      "evidence":        dict  结构化事实（agent 不必 regex title）
      "inspect_command": str|None  只读诊断命令，可复读
      "fix_command":     str|None  写操作命令，可能需 sudo / 改文件
      "auto_fixable":    bool  agent 据此决策"自己干 vs 问用户"
      "doc":             str   "suggestion:<id>"，CI 校验对应 explain topic
      "fingerprint":     str   sha1(id)[:12]，跨次去重的唯一字段
      "first_seen":      str   ISO8601，从 state 文件读，缺失即 now
      "since":           str   引入版本
    }

排序契约（写进 AGENTS.md，agent 可稳定 diff）：
    severity desc (warn > advisory > info), id asc

State 文件（CLI 维护，agent 不必碰）：
    ~/.cache/proxyctl/suggestions_state.json   {fingerprint: first_seen_iso}
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
SEVERITY_ORDER = {"warn": 0, "advisory": 1, "info": 2}
ACTOR_ENUM = ("user", "agent", "cron", "engine")
SEVERITY_ENUM = ("info", "advisory", "warn")

_STATE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "proxyctl")
_STATE_FILE = os.path.join(_STATE_DIR, "suggestions_state.json")


def state_file_path() -> str:
    """state 文件路径。允许 PROXYCTL_SUGGEST_STATE_PATH 覆盖（测试用）。"""
    return os.environ.get("PROXYCTL_SUGGEST_STATE_PATH") or _STATE_FILE


# 规则级声明：哪些 evidence 字段进 fingerprint hash。
# 默认空（仅 hash id）。需要"多实例同类问题分别去重"的规则（如多个 proxy_group
# 同时挂掉），在此列出 evidence 中的稳定关键字。**抖动字段（百分比、时间戳、
# 剩余天数）禁止列入**，否则 agent 跨次调用看不到稳定 fingerprint。
FINGERPRINT_EVIDENCE_KEYS: dict[str, tuple[str, ...]] = {
    "proxy_group.mostly_dead": ("group_name",),
}


def _compute_fingerprint(suggestion_or_id) -> str:
    """v0.5.0：fingerprint = sha1(id [+ stable evidence keys])[:12]。

    抖动字段（百分比、剩余天数）不进 hash —— 让 agent 在"同一问题还没解决"
    场景下能稳定去重。需要更细粒度（如多 group 各自指纹）通过
    FINGERPRINT_EVIDENCE_KEYS 显式声明。

    Args:
        suggestion_or_id: 可以是 suggestion dict（推荐）或纯 id 字符串
            （兼容 v0.5.0-rc 旧调用方式）。
    """
    if isinstance(suggestion_or_id, str):
        sid = suggestion_or_id
        ev: dict = {}
    else:
        sid = suggestion_or_id.get("id", "")
        ev = suggestion_or_id.get("evidence", {}) or {}
    keys = FINGERPRINT_EVIDENCE_KEYS.get(sid, ())
    parts = [sid]
    for k in keys:
        parts.append(f"{k}={ev.get(k, '')}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _load_state() -> dict[str, str]:
    """读 state 文件。文件不存在 / 损坏时返回 {}（无声，不破坏 doctor）。"""
    path = state_file_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items()
                if isinstance(k, str) and isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, str]) -> None:
    """原子写：先写 tmp 再 rename。失败静默（doctor 是只读命令，丢 state 不致命）。"""
    path = state_file_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".suggest_state.", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return


def _sort_key(s: dict[str, Any]) -> tuple:
    return (SEVERITY_ORDER.get(s.get("severity", "info"), 9), s.get("id", ""))


def _decorate(raw: list[dict[str, Any]], *,
              persist_state: bool = True) -> list[dict[str, Any]]:
    """填 fingerprint / first_seen，按契约排序。

    Args:
        raw: 各规则函数返回的原始 suggestion 列表（未填 fingerprint/first_seen）
        persist_state: True 则把新出现的 fingerprint 写回 state 文件；
                       False 适合测试或 --no-state-write 场景
    """
    state = _load_state()
    now = _now_iso()
    decorated: list[dict[str, Any]] = []
    state_changed = False
    seen_now: set[str] = set()
    for s in raw:
        sid = s.get("id", "")
        if not sid:
            continue
        fp = _compute_fingerprint(s)  # v0.5.0+ 传 dict 让 evidence 关键字进 hash
        first = state.get(fp)
        if first is None:
            first = now
            state[fp] = now
            state_changed = True
        seen_now.add(fp)
        out = dict(s)
        # 默认字段补全（规则函数可省略时给安全默认）
        out.setdefault("inspect_command", None)
        out.setdefault("fix_command", None)
        out.setdefault("auto_fixable", False)
        out.setdefault("evidence", {})
        out["fingerprint"] = fp
        out["first_seen"] = first
        decorated.append(out)

    # 修剪 state：从 state 中删除"本次没出现的旧 fingerprint"——
    # 让 suggestions_state.json 不会无限增长（垃圾积累）。仅删本次未见且仍在 state 中的项。
    # 但这会破坏"问题修了又复发"的 first_seen 连续性。权衡：默认不修剪。
    # 未来通过 ttl 或显式 prune 命令处理。

    if persist_state and state_changed:
        _save_state(state)
    decorated.sort(key=_sort_key)
    return decorated


def build_suggestions(*, sub: dict[str, Any] | None = None,
                      sub_explicit_missing: bool = False,
                      autostart_inspect: dict[str, Any] | None = None,
                      path_binary: str | None = None,
                      path_version: str | None = None,
                      expected_config_dir: str | None = None,
                      engine_config_inspect: dict[str, Any] | None = None,
                      known_versions: dict[str, Any] | None = None,
                      engine_config_dir: str | None = None,
                      proxies_payload: dict[str, Any] | None = None,
                      persist_state: bool = True,
                      _extra_raw: list[dict[str, Any]] | None = None,
                      ) -> list[dict[str, Any]]:
    """聚合所有规则，返回排好序、含 fingerprint/first_seen 的 suggestion 列表。

    Args:
        sub: subscription.load() 的返回；None 表示文件不存在
        sub_explicit_missing: True 才输出 subscription.missing；False 时 None 静默
            （doctor 调用方期望"未配置订阅 = 静默"是默认；用户主动跑 --hint-missing
             才显示）
        autostart_inspect: autostart.inspect_static / inspect_runtime 返回；
            None 表示跳过整组（如非首发平台）
        path_binary: shutil.which(backend_name) 结果（用于 autostart.binary_mismatch）
        path_version: PATH binary 的版本（来自 cli.get_engine_version）
        expected_config_dir: backend 期望的 config_dir
            （用于 autostart.config_dir_mismatch）
        persist_state: 是否写 state 文件（测试可关）
        _extra_raw: 测试/未来扩展注入额外 raw suggestions（不经过 subscription）

    Returns:
        排序后的 suggestion 列表，每条含完整 schema v1 字段。
    """
    from proxyctl import subscription, autostart as _autostart

    raw: list[dict[str, Any]] = []

    if sub is None and sub_explicit_missing:
        raw.extend(subscription.to_suggestions(None))
    elif sub is not None:
        raw.extend(subscription.to_suggestions(sub))

    if autostart_inspect is not None:
        raw.extend(_autostart.to_suggestions(
            autostart_inspect,
            path_binary=path_binary,
            path_version=path_version,
            expected_config_dir=expected_config_dir,
        ))

    if engine_config_inspect is not None:
        from proxyctl import suggest_rules as _rules
        raw.extend(_rules.controller_rules(engine_config_inspect))

    if path_version and known_versions:
        from proxyctl import suggest_rules as _rules
        raw.extend(_rules.engine_rules(path_version, known_versions))

    if engine_config_dir:
        from proxyctl import suggest_rules as _rules
        raw.extend(_rules.geo_rules(engine_config_dir))

    if proxies_payload is not None:
        from proxyctl import suggest_rules as _rules
        raw.extend(_rules.proxy_group_rules(proxies_payload))

    if _extra_raw:
        raw.extend(_extra_raw)

    return _decorate(raw, persist_state=persist_state)
