"""proxyctl.suggest_rules — 安全/引擎/数据/分组类规则（v0.5.0+）。

容纳 controller / engine / data / proxy_group 四组规则的纯函数：
  controller.empty_secret      — external-controller secret == ""
  controller.weak_secret       — secret 长度 < 16
  controller.public_bind       — external-controller bind 到 0.0.0.0 / 公网
  engine.outdated              — 当前版本 < known_versions.json 的 safe_min
  data.geo_stale               — geoip.dat / geosite.dat mtime > 30 天
  proxy_group.mostly_dead      — 单组 ≥ 70% 节点 delay==0（多组各自指纹）

每条规则都是纯函数：输入预解析过的字典，输出 Suggestion list。
读 mihomo config / known_versions.json / geo 文件 mtime / /proxies API
的副作用统一在 inspect_* / fetch_*。
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

GEO_STALE_DAYS = 30
GEO_FILES = ("geoip.dat", "geosite.dat", "geoip.metadb")


# ────────────────────────────────────────────────────────────────────────────
# Mihomo config 解析 — controller 三规则的输入
# ────────────────────────────────────────────────────────────────────────────

def inspect_engine_config(config_file: str) -> dict[str, Any]:
    """读 mihomo / sing-box config，提取 controller 字段。

    设计立场：不引入新依赖（yaml 库 proxyctl 已经在用），
    但解析失败时静默降级——doctor 不能因为用户 config 损坏就死。

    Returns:
        {
          "config_exists": bool,
          "controller_host": "127.0.0.1" | "0.0.0.0" | None,
          "controller_port": int | None,
          "controller_secret": str | None,   # 可为 ""
          "errors": list[str],
        }
    """
    out: dict[str, Any] = {
        "config_exists": False,
        "controller_host": None,
        "controller_port": None,
        "controller_secret": None,
        "errors": [],
    }
    if not config_file or not os.path.isfile(config_file):
        return out
    out["config_exists"] = True
    try:
        with open(config_file, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        out["errors"].append(f"config read failed: {e}")
        return out

    # 用正则解析（不强求完整 YAML —— 用户 config 可能有 anchors / merge 复杂结构）
    # external-controller: 127.0.0.1:9090 / 0.0.0.0:9090 / :9090
    m = re.search(r"^external-controller:\s*['\"]?([^'\"\s#]+)['\"]?\s*$",
                  text, re.M)
    if m:
        spec = m.group(1).strip()
        if spec.startswith(":"):
            out["controller_host"] = "127.0.0.1"
            try:
                out["controller_port"] = int(spec[1:])
            except ValueError:
                pass
        else:
            # host:port
            parts = spec.rsplit(":", 1)
            if len(parts) == 2:
                out["controller_host"] = parts[0]
                try:
                    out["controller_port"] = int(parts[1])
                except ValueError:
                    pass

    # secret: "xxxx" / secret: xxxx / secret: '' / 缺失即 None
    m = re.search(r"^secret:\s*['\"]?([^'\"\n#]*)['\"]?\s*$", text, re.M)
    if m:
        out["controller_secret"] = m.group(1).strip()
    return out


def _is_public_bind(host: str | None) -> bool:
    """判断 host 是否为"公网/任意"绑定。

    包含：0.0.0.0 / :: / 非环回的具体 IP / 非环回的具体 hostname。
    """
    if not host:
        return False
    h = host.strip()
    if h in ("0.0.0.0", "::", "*"):
        return True
    if h in ("127.0.0.1", "::1", "localhost"):
        return False
    # 具体 IP 如 192.168.x.x / 公网 IP —— 也算 public bind（暴露给局域网/外网）
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", h):
        return True
    # 非环回 hostname（少见但可能）
    return True


def controller_rules(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """controller 规则 — v0.5.1 修复复合判定 bug。

    哥 2026-05-19 指出的设计问题："只有本机才能访问为什么还要看密钥复杂度？"

    旧逻辑（v0.5.0）独立判断 empty_secret / weak_secret / public_bind，
    导致 bind=127.0.0.1 + secret 短也会触发 weak_secret advisory ——
    但本地 attack surface 不存在，规则属于误报。

    新逻辑（v0.5.1）：以 attack surface 为单一判定轴。
      bind 127.0.0.1 → 任何 secret（含空）都不报
      bind public    → 视 secret 强度报 warn / advisory，public_bind 永远 warn
    """
    if not cfg.get("config_exists"):
        return []
    out: list[dict[str, Any]] = []
    host = cfg.get("controller_host")
    port = cfg.get("controller_port")
    secret = cfg.get("controller_secret")

    # controller 未配置时整组规则跳过
    if not (host or port):
        return out

    public = _is_public_bind(host)

    # 127.0.0.1 / ::1 → attack surface 不存在，secret 强度不评估
    if not public:
        return out

    # ── 以下分支仅当 bind 暴露给非本机时触发 ─────────────────────────

    # public_bind 本身永远报 warn（明确告诉用户"你的 API 暴露给了外面"）
    out.append({
        "id": "controller.public_bind",
        "severity": "warn",
        "actor": "user",
        "title": f"Clash API bind 到非环回地址：{host}",
        "evidence": {"controller_host": host, "controller_port": port},
        "doc": "suggestion:controller.public_bind",
        "since": "0.5.0",
    })

    if secret == "" or secret is None:
        # 公网 bind + 无认证 = 任何人可控制代理 → warn
        out.append({
            "id": "controller.empty_secret",
            "severity": "warn",
            "actor": "user",
            "title": f"Clash API bind 公网（{host}）但 secret 为空",
            "evidence": {
                "controller_host": host,
                "controller_port": port,
                "secret_set": False,
            },
            "fix_command": "proxyctl explain suggestion:controller.empty_secret",
            "doc": "suggestion:controller.empty_secret",
            "since": "0.5.0",
        })
    elif len(secret) < 16:
        # 公网 bind + 弱 secret = 易爆破 → advisory（有 secret 总比无好，
        # 但应该升级）
        out.append({
            "id": "controller.weak_secret",
            "severity": "advisory",
            "actor": "user",
            "title": (f"Clash API bind 公网（{host}）且 secret 仅 "
                      f"{len(secret)} 字符（< 16）"),
            "evidence": {
                "controller_host": host,
                "secret_length": len(secret),
            },
            "doc": "suggestion:controller.weak_secret",
            "since": "0.5.0",
        })
    return out


# ────────────────────────────────────────────────────────────────────────────
# engine.outdated — 读 known_versions.json 契约文件
# ────────────────────────────────────────────────────────────────────────────

KNOWN_VERSIONS_PATH = "~/.cache/proxyctl/known_versions.json"


def known_versions_path() -> str:
    p = os.environ.get("PROXYCTL_KNOWN_VERSIONS_PATH") or KNOWN_VERSIONS_PATH
    return os.path.expanduser(p)


def load_known_versions() -> dict[str, Any] | None:
    """读 known_versions.json。设计同 subscription.json 契约：
    用户脚本写、proxyctl 只读、缺失即静默。

    Schema:
        {
          "safe_min_version": "1.18.0",
          "unsafe_versions": ["1.19.18", "1.19.19"],
          "updated_at": "2026-05-19T..."
        }
    """
    import json
    p = known_versions_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _parse_version(v: str) -> tuple:
    """简易语义版本解析。'1.18.10' → (1, 18, 10)；不规范字段当 0。"""
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts[:3]) + (0,) * (3 - min(len(parts), 3))


def engine_rules(current_version: str | None,
                  known: dict[str, Any] | None) -> list[dict[str, Any]]:
    """engine.outdated 规则。current_version 缺失或 known 缺失即跳过。"""
    if not current_version or not known:
        return []
    safe_min = known.get("safe_min_version")
    unsafe = known.get("unsafe_versions") or []
    cur_t = _parse_version(current_version)

    if current_version in unsafe:
        return [{
            "id": "engine.outdated",
            "severity": "warn",
            "actor": "user",
            "title": f"当前引擎版本 {current_version} 已知存在问题",
            "evidence": {
                "current_version": current_version,
                "unsafe_versions": unsafe,
            },
            "doc": "suggestion:engine.outdated",
            "since": "0.5.0",
        }]
    if safe_min and cur_t < _parse_version(safe_min):
        return [{
            "id": "engine.outdated",
            "severity": "info",
            "actor": "user",
            "title": (f"引擎版本 {current_version} 低于已知安全基线 "
                      f"{safe_min}"),
            "evidence": {
                "current_version": current_version,
                "safe_min_version": safe_min,
            },
            "doc": "suggestion:engine.outdated",
            "since": "0.5.0",
        }]
    return []


# ────────────────────────────────────────────────────────────────────────────
# data.geo_stale — GeoIP / GeoSite 文件 mtime
# ────────────────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────────────────
# proxy_group.mostly_dead — 调 mihomo /proxies API
# ────────────────────────────────────────────────────────────────────────────

# 哪些 group 类型纳入判定：自动选/手动选/降级/负载/Smart
DEAD_CHECK_GROUP_TYPES = ("URLTest", "Selector", "Fallback", "LoadBalance",
                          "Smart")

# 伪节点（DIRECT/REJECT/Pass），不参与 dead 统计——它们没有"可达性"概念
PSEUDO_NODE_TYPES = ("Direct", "Reject", "Pass", "Compatible")

# 节点数少于此值的组不报告（小组本来就容易脏）
MIN_GROUP_SIZE_FOR_DEAD_CHECK = 3

# 死率阈值（≥70% 节点 delay==0 即报）
DEAD_PCT_THRESHOLD = 70.0


def _collect_leaves(group_name: str, proxies: dict[str, Any],
                    seen: set[str]) -> list[str]:
    """穿透 selector/URLTest/Fallback 子组，递归收集所有真叶子节点。

    返回去重后的叶子节点名列表。伪节点（DIRECT/REJECT）排除。
    seen 跟踪本次调用栈访问过的组名，防止循环引用死循环。
    """
    if group_name in seen:
        return []
    seen = seen | {group_name}
    info = proxies.get(group_name)
    if not isinstance(info, dict):
        return []
    typ = info.get("type")
    if typ in PSEUDO_NODE_TYPES:
        return []
    if typ not in DEAD_CHECK_GROUP_TYPES:
        # 真叶子（Shadowsocks / Vmess / Trojan / TUIC / Hysteria / ...）
        return [group_name]
    leaves: list[str] = []
    for m in info.get("all") or []:
        if not isinstance(m, str):
            continue
        for leaf in _collect_leaves(m, proxies, seen):
            if leaf not in leaves:
                leaves.append(leaf)
    return leaves


def fetch_proxies(api_base: str, api_secret: str = "", *,
                  timeout: float = 0.5) -> dict[str, Any] | None:
    """调 mihomo `/proxies` API，本地 HTTP，0 外网。

    失败（API 不通 / 鉴权失败 / 超时 / JSON 损坏）静默返回 None ——
    doctor 不能因为 API 暂时不可达就死。

    Args:
        api_base: 如 'http://127.0.0.1:9090'
        api_secret: Bearer token；空字符串表示未配
        timeout: 短超时，本地 API 通常 <50ms
    """
    if not api_base:
        return None
    import json as _json
    import urllib.error
    import urllib.request
    url = f"{api_base.rstrip('/')}/proxies"
    headers = {}
    if api_secret:
        headers["Authorization"] = f"Bearer {api_secret}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        data = _json.loads(raw)
    except (urllib.error.URLError, OSError, _json.JSONDecodeError,
            UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def proxy_group_rules(proxies_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """proxy_group.mostly_dead 规则（v0.5.4 起穿透子组到叶子统计）。

    每个挂掉的组输出**独立**一条 suggestion（fingerprint 含 group_name），
    agent 可分别跟踪。

    v0.5.4 行为变更：判定从"直接成员 delay==0"改为"穿透到叶子节点 delay==0"。
    GLOBAL 这种 selector-of-selectors 组下的 13 个分流子组（电报/苹果/Steam
    等）若无 latency history（mihomo 没给它们测过延迟）不再被算作 dead，
    伪节点（DIRECT/REJECT）也排除。dead_pct 现在基于真叶子节点数。

    Args:
        proxies_payload: fetch_proxies() 返回；None 表示跳过整组
    """
    if not proxies_payload:
        return []
    proxies = proxies_payload.get("proxies")
    if not isinstance(proxies, dict):
        return []
    out: list[dict[str, Any]] = []
    for name, info in proxies.items():
        if not isinstance(info, dict):
            continue
        if info.get("type") not in DEAD_CHECK_GROUP_TYPES:
            continue
        leaves = _collect_leaves(name, proxies, seen=set())
        if len(leaves) < MIN_GROUP_SIZE_FOR_DEAD_CHECK:
            continue
        dead = 0
        for leaf in leaves:
            node = proxies.get(leaf)
            if not isinstance(node, dict):
                continue
            history = node.get("history") or []
            if not history:
                dead += 1
                continue
            last = history[-1] if isinstance(history[-1], dict) else {}
            if last.get("delay", 0) == 0:
                dead += 1
        dead_pct = (dead / len(leaves)) * 100.0
        if dead_pct >= DEAD_PCT_THRESHOLD:
            out.append({
                "id": "proxy_group.mostly_dead",
                "severity": "warn",
                "actor": "user",
                "title": (f"代理组 {name} 中 {dead}/{len(leaves)} 叶子节点不可达 "
                          f"({dead_pct:.0f}%)"),
                "evidence": {
                    "group_name": name,            # 进 fingerprint 的稳定字段
                    "group_type": info.get("type"),
                    "dead_count": dead,
                    "total_count": len(leaves),
                    # dead_pct 不进 fingerprint（抖动字段），仅供人看
                    "dead_pct_at_check": round(dead_pct, 1),
                },
                "inspect_command": "proxyctl check --json | jq .data.stages.groups",
                "fix_command": "proxyctl bench  # 重测延迟，必要时切换订阅",
                "doc": "suggestion:proxy_group.mostly_dead",
                "since": "0.5.0",
            })
    return out


# ────────────────────────────────────────────────────────────────────────────
# data.geo_stale — GeoIP / GeoSite 文件 mtime
# ────────────────────────────────────────────────────────────────────────────

def geo_rules(engine_config_dir: str | None,
              *, now: float | None = None) -> list[dict[str, Any]]:
    """data.geo_stale 规则。

    Args:
        engine_config_dir: 引擎 config 目录（含 geoip.dat / geosite.dat）
        now: time.time() 注入点（测试用）
    """
    if not engine_config_dir or not os.path.isdir(engine_config_dir):
        return []
    now_t = now if now is not None else time.time()
    stale: list[tuple[str, int]] = []
    for fn in GEO_FILES:
        p = os.path.join(engine_config_dir, fn)
        try:
            st = os.stat(p)
        except OSError:
            continue
        age_days = int((now_t - st.st_mtime) // 86400)
        if age_days > GEO_STALE_DAYS:
            stale.append((fn, age_days))
    if not stale:
        return []
    files_str = ", ".join(f"{n}({d}d)" for n, d in stale)
    return [{
        "id": "data.geo_stale",
        "severity": "info",
        "actor": "cron",
        "title": f"GeoIP/GeoSite 数据 > {GEO_STALE_DAYS} 天未更新：{files_str}",
        "evidence": {
            "stale_files": [{"name": n, "age_days": d} for n, d in stale],
        },
        "fix_command": (
            "# 在用户脚本/cron 中拉取最新 GeoIP / GeoSite："
            " https://github.com/MetaCubeX/meta-rules-dat"),
        "doc": "suggestion:data.geo_stale",
        "since": "0.5.0",
    }]
