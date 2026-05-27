"""proxyctl check — 全面健康检查 (4 个阶段并发执行)

通用骨架，所有"本机感知"内容（测哪些 URL、关心哪些组、探哪些出口）都从
PluginRegistry 收集，core 不感知具体业务。
"""

import json
import os
import platform
import re as _re_mod
import socket
import subprocess
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

IS_MACOS = platform.system() == "Darwin"


RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[0;33m"
CYAN   = "\033[0;36m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
NC     = "\033[0m"

from proxyctl._io import maybe_disable_module_colors as _pc_maybe_disable
_pc_maybe_disable(__name__)

HOME = os.path.expanduser("~")

_ANSI_RE = _re_mod.compile(r"\x1b\[[0-9;]*m")
def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s).strip()


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _test_url(url: str, desc: str, mode: str = "proxy", timeout: int = 8,
              proxy_port: int = 7890) -> tuple:
    """
    测试 URL 可达性。
    mode=proxy: 走 socks5h://127.0.0.1:{proxy_port}（需配置 mixed-port 或 socks-port）
    mode=direct: 绕过所有代理 (--noproxy '*')
    返回 (ok: bool, line: str)，调用方负责 print。
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("http_proxy", "https_proxy", "all_proxy",
                        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    cmd = ["curl", "--http1.1", "-s", "-o", "/dev/null", "-w", "%{http_code}",
           "--max-time", str(timeout)]
    if mode == "proxy":
        cmd += ["--proxy", f"socks5h://127.0.0.1:{proxy_port}"]
    else:
        cmd += ["--noproxy", "*"]
    cmd.append(url)

    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    code = r.stdout.strip()

    if code == "000" or not code:
        reason = _curl_failure_reason(r.stderr)
        return False, f"  {RED}✗{NC} {desc:<18s} {url:<44s} {RED}{reason}{NC}"
    elif code.startswith(("2", "3", "4")):
        return True,  f"  {GREEN}✓{NC} {desc:<18s} {url:<44s} {GREEN}{code}{NC}"
    elif code.startswith("5"):
        # 5xx = 链路通了，服务端报错，不算代理故障
        return True,  f"  {YELLOW}✓{NC} {desc:<18s} {url:<44s} {YELLOW}{code} (server error){NC}"
    else:
        return False, f"  {YELLOW}?{NC} {desc:<18s} {url:<44s} {YELLOW}{code}{NC}"


def _curl_failure_reason(stderr: str) -> str:
    """Return a concise curl failure reason for HTTP code 000."""
    text = (stderr or "").strip()
    if not text:
        return "timeout"
    first = text.splitlines()[0].strip()
    first = first.removeprefix("curl: ").strip()
    return first[:120] if first else "timeout"


def _api_connections(api_base: str, api_secret: str) -> list[dict]:
    r = subprocess.run(
        ["curl", "-s", "--noproxy", "*",
         "-H", f"Authorization: Bearer {api_secret}",
         f"{api_base.rstrip('/')}/connections"],
        capture_output=True, text=True, timeout=2
    )
    try:
        data = json.loads(r.stdout or "{}")
    except Exception:
        return []
    conns = data.get("connections")
    return conns if isinstance(conns, list) else []


def _route_from_connections(conns: list[dict], host: str) -> dict:
    """Find one Mihomo route for host and expose leaf line + policy group.

    Mihomo reports chains from leaf node to policy group, for example
    ``["TW-Residential-01", "residential-tw", "claude"]``. The human
    "line" is the leaf node, not the policy group.
    """
    for conn in conns:
        meta = conn.get("metadata") or {}
        conn_host = meta.get("host") or ""
        if conn_host != host and not conn_host.endswith("." + host):
            continue
        chains = [str(c) for c in (conn.get("chains") or []) if c]
        return {
            "found": True,
            "line": chains[0] if chains else "?",
            "group": chains[-1] if chains else "",
            "chain": " → ".join(reversed(chains)) if chains else "?",
        }
    return {"found": False, "line": "?", "group": "", "chain": ""}


def _target_route(api_base: str, api_secret: str, url: str) -> dict:
    """Return the observed route for a URL from local Mihomo connections."""
    host = urllib.parse.urlparse(url).hostname or ""
    if not host:
        return {"found": False, "line": "?", "group": "", "chain": ""}

    for _ in range(3):
        route = _route_from_connections(_api_connections(api_base, api_secret),
                                        host)
        if route.get("found"):
            return route
        time.sleep(0.2)
    return {"found": False, "line": "?", "group": "", "chain": ""}


def _route_matches_expected_proxy(route: dict,
                                  expected_proxy: str) -> tuple[bool, str]:
    """Validate an observed route's terminal policy group."""
    if not expected_proxy:
        return True, ""
    if not route.get("found"):
        return False, f" expected {expected_proxy}, no active connection found"

    actual = str(route.get("group") or "")
    if actual.lower() == expected_proxy.lower():
        return True, ""
    return False, f" expected {expected_proxy}, got {actual or '?'}"


def _target_uses_expected_proxy(api_base: str, api_secret: str, url: str,
                                expected_proxy: str) -> tuple[bool, str]:
    if not urllib.parse.urlparse(url).hostname or not expected_proxy:
        return True, ""
    return _route_matches_expected_proxy(
        _target_route(api_base, api_secret, url), expected_proxy)


def _connectivity_line_value(mode: str) -> str:
    """Return the default route-line label for one check target mode."""
    if mode == "direct":
        return "direct"
    if mode == "proxy":
        return "?"
    return "-"


def _append_route_column(line: str, route_line: str,
                         route_chain: str = "") -> str:
    """Append the observed route in the same style as Mihomo chains."""
    value = route_chain or route_line or "-"
    return f"{line}  {DIM}via{NC} {value:<32s}"


def _dedupe_check_targets(targets: list) -> list:
    """Drop exact duplicate connectivity targets from multiple plugins.

    Builtin plugins load before user plugins. Keep the first target's display
    order, but merge an expected_proxy from a duplicate if the first one lacks
    it, so a stale user plugin cannot remove route validation by accident.
    """
    out = []
    seen = {}
    for target in targets:
        key = (getattr(target, "name", ""),
               getattr(target, "url", ""),
               getattr(target, "mode", ""))
        if key in seen:
            kept = seen[key]
            expected = getattr(target, "expected_proxy", "")
            if expected and not getattr(kept, "expected_proxy", ""):
                kept.expected_proxy = expected
            continue
        seen[key] = target
        out.append(target)
    return out


def _dedupe_outbound_probes(probes: list) -> list:
    """Drop exact duplicate outbound probes from multiple plugins."""
    out = []
    seen = set()
    for probe in probes:
        key = (getattr(probe, "name", ""),
               getattr(probe, "mode", ""),
               getattr(probe, "url", ""),
               getattr(probe, "extract_re", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(probe)
    return out


def _test_dns(desc: str, server: str, domain: str) -> tuple:
    """通用 DNS 可达性测试：dig @server domain（用于 CheckTarget mode='dns'）。

    url 格式：dns:<server>:<domain>
    """
    r = subprocess.run(
        ["dig", f"@{server}", "+short", "+timeout=3", domain],
        capture_output=True, text=True, timeout=5
    )
    ok = r.returncode == 0 and r.stdout.strip()
    if ok:
        return True,  f"  {GREEN}✓{NC} {desc:<18s} {server:<44s} {GREEN}ok{NC}"
    else:
        return False, f"  {RED}✗{NC} {desc:<18s} {server:<44s} {RED}timeout{NC}"


def _test_tcp(host: str, port: int, desc: str) -> tuple:
    """返回 (ok: bool, line: str)，调用方负责 print。"""
    addr = f"{host}:{port}"
    try:
        with socket.create_connection((host, port), timeout=3):
            return True,  f"  {GREEN}✓{NC} {desc:<18s} {addr:<44s} {GREEN}ok{NC}"
    except OSError:
        return False, f"  {RED}✗{NC} {desc:<18s} {addr:<44s} {RED}unreachable{NC}"


def _rule_target_groups_from_config(config_path: str) -> list[str]:
    """Return proxy groups that are used as mihomo rule targets.

    `check` is a maintenance view: if a top-level rule can route traffic into a
    group, that group should be visible even when it is not a child of `proxy`.
    """
    if not config_path or not os.path.isfile(config_path):
        return []

    try:
        import yaml
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    group_names: set[str] = set()
    for group in data.get("proxy-groups") or []:
        if isinstance(group, dict) and isinstance(group.get("name"), str):
            group_names.add(group["name"])

    if not group_names:
        return []

    # Mihomo rule strings use comma-separated fields; the policy is normally
    # the last field, except trailing options such as `no-resolve`.
    option_tokens = {"no-resolve", "src", "dst", "in", "out"}
    targets: list[str] = []
    seen: set[str] = set()

    def add_target(name: str) -> None:
        if name in group_names and name not in seen:
            seen.add(name)
            targets.append(name)

    for rule in data.get("rules") or []:
        if isinstance(rule, str):
            parts = [p.strip() for p in rule.split(",") if p.strip()]
            for token in reversed(parts[1:]):
                if token.lower() in option_tokens:
                    continue
                add_target(token)
                break
        elif isinstance(rule, dict):
            for key in ("policy", "target", "proxy", "outbound"):
                value = rule.get(key)
                if isinstance(value, str):
                    add_target(value)
                    break

    return targets


def _merge_check_groups(base: list[str] | None, rule_targets: list[str]) -> list[str]:
    """Merge plugin groups and config-derived rule target groups preserving order."""
    merged: list[str] = []
    seen: set[str] = set()
    for name in list(base or []) + list(rule_targets or []):
        if name and name not in seen:
            seen.add(name)
            merged.append(name)
    return merged


def _proxy_groups_section(api_base: str, api_secret: str,
                           groups: list[str] | None = None,
                           collect_into: list | None = None) -> bool:
    """检查并自动修复代理组，打印节点明细。返回是否全部正常。

    Args:
        groups: 要展示的组名列表。None 时回退到 ["proxy"]。
        collect_into: 可选 list；如提供，把每组结构化数据 append 进去：
            {"name": str, "now": str, "members": [{"name", "delay_ms", "is_now"}],
             "tested_ago": str}
    """
    r = subprocess.run(
        ["curl", "-s", "--noproxy", "*",
         "-H", f"Authorization: Bearer {api_secret}",
         f"{api_base}/proxies"],
        capture_output=True, text=True, timeout=5
    )
    if not r.stdout.strip():
        print(f"  {YELLOW}—{NC} Clash API 不可达")
        return False

    try:
        data = json.loads(r.stdout)
    except Exception:
        print(f"  {YELLOW}—{NC} API 响应解析失败")
        return False

    proxies = data.get("proxies", {})

    from datetime import datetime, timezone

    def get_delay(name: str, src: dict = None) -> int:
        """取节点延迟；若 name 是组则穿透到它的 now 节点。"""
        p = (src or proxies).get(name, {})
        # 如果是组（有 all 字段），穿透到 now 节点
        if p.get("all") and p.get("now"):
            p = proxies.get(p["now"], {})
        h = p.get("history", [])
        return h[-1].get("delay", 0) if h else -1

    def ds(d: int) -> str:
        if d < 0: return f"{YELLOW}—{NC}"
        if d == 0: return f"{RED}✗{NC}"
        if d < 200: return f"{GREEN}{d}{NC}"
        if d < 500: return f"{YELLOW}{d}{NC}"
        return f"{RED}{d}{NC}"

    def group_tested_ago(members: list) -> str:
        """返回组内最近一次测试时间，格式如 '3m ago'；无数据返回空字符串。"""
        latest = None
        for m in members:
            h = proxies.get(m, {}).get("history", [])
            if not h:
                continue
            t_str = h[-1].get("time", "")
            if not t_str:
                continue
            try:
                t = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                if latest is None or t > latest:
                    latest = t
            except ValueError:
                pass
        if latest is None:
            return ""
        diff = int((datetime.now(timezone.utc) - latest).total_seconds())
        if diff < 60:
            return f"{diff}s ago"
        if diff < 3600:
            return f"{diff // 60}m ago"
        return f"{diff // 3600}h ago"

    # 输出所有组
    def dw(s: str) -> int:
        """计算终端显示宽度（中文双宽）。"""
        return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)

    def print_members(members: list, now: str):
        """打印组成员列表（4 列表格）。"""
        if not members:
            return
        max_w = max(dw(m) for m in members)
        col_w = max_w + 8
        line = "   "
        col = 0
        for m in members:
            d = get_delay(m)
            marker = "→" if m == now else " "
            raw_d = str(d) if d > 0 else ("✗" if d == 0 else "—")
            raw = f"{marker}{m}:{raw_d}"
            pad = max(1, col_w - dw(raw))
            line += f"{marker}{m}:{ds(d)}{' ' * pad}"
            col += 1
            if col >= 4:
                print(line.rstrip())
                line = "   "
                col = 0
        if col > 0:
            print(line.rstrip())

    # 跟踪本次 check 输出已经展开过节点列表的子组（跨 print_group 调用复用）
    expanded_subgroups: set[str] = set()

    def print_group(gname: str):
        """打印单个组的摘要 + 成员；若 selector 成员也是组则递归展开。"""
        g = proxies.get(gname)
        if not g:
            return
        _type_map = {"URLTest": "url", "Selector": "sel", "Fallback": "fb", "LoadBalance": "lb"}
        gtype = _type_map.get(g.get("type"), g.get("type", "?")[:3].lower())
        gnow  = g.get("now", "?")
        gnow_d = get_delay(gnow)
        gmembers = g.get("all", [])

        # selector/fallback 成员如果是子组，用子组内部的节点做 alive/dead 统计
        if g.get("type") in ("Selector", "Fallback"):
            alive = dead = nodata = total = 0
            for m in gmembers:
                sub = proxies.get(m, {})
                if sub.get("all"):
                    # 子组：统计它里面的叶子节点
                    for leaf in sub["all"]:
                        d = get_delay(leaf)
                        total += 1
                        if d > 0: alive += 1
                        elif d == 0: dead += 1
                        else: nodata += 1
                else:
                    # 普通节点
                    d = get_delay(m)
                    total += 1
                    if d > 0: alive += 1
                    elif d == 0: dead += 1
                    else: nodata += 1
        else:
            alive  = sum(1 for m in gmembers if get_delay(m) > 0)
            dead   = sum(1 for m in gmembers if get_delay(m) == 0)
            nodata = sum(1 for m in gmembers if get_delay(m) < 0)
            total  = len(gmembers)
        counts = []
        if alive:  counts.append(f"{GREEN}{alive}✓{NC}")
        if dead:   counts.append(f"{RED}{dead}✗{NC}")
        if nodata: counts.append(f"{YELLOW}{nodata}—{NC}")
        count_str = "/".join(counts)
        tested = group_tested_ago(gmembers)
        tested_str = f"  {DIM}{tested}{NC}" if tested else ""
        print(f"  {CYAN}{gname}{NC}({gtype}) → {BOLD}{gnow}{NC} "
              f"{ds(gnow_d)}ms  [{count_str} of {total}]{tested_str}")

        def print_sub_summary(sub_name: str, sub: dict, is_now: bool) -> bool:
            """打印一行子组 summary（含 → now 标记 + 健康度 + tested ago）。
            返回该子组之前是否已展开过叶子。"""
            sub_type = "url" if sub.get("type") == "URLTest" else "sel"
            sub_now = sub.get("now", "?")
            sub_now_d = get_delay(sub_now)
            sub_members = sub.get("all", [])
            s_alive = sum(1 for x in sub_members if get_delay(x) > 0)
            s_dead  = sum(1 for x in sub_members if get_delay(x) == 0)
            s_nodata = sum(1 for x in sub_members if get_delay(x) < 0)
            sc = []
            if s_alive:  sc.append(f"{GREEN}{s_alive}✓{NC}")
            if s_dead:   sc.append(f"{RED}{s_dead}✗{NC}")
            if s_nodata: sc.append(f"{YELLOW}{s_nodata}—{NC}")
            st = group_tested_ago(sub_members)
            st_str = f"  {DIM}{st}{NC}" if st else ""
            already = sub_name in expanded_subgroups
            tail = f"  {DIM}(详见上方){NC}" if already else ""
            marker = "→" if is_now else " "
            print(f"   {marker}{CYAN}{sub_name}{NC}({sub_type}) → "
                  f"{BOLD}{sub_now}{NC} "
                  f"{ds(sub_now_d)}ms  [{'/'.join(sc)} of {len(sub_members)}]"
                  f"{st_str}{tail}")
            return already

        # v0.5.5：selector/fallback 含子组时，每个子组都打一行 summary（让
        # 用户看到兄弟子组存在 + 健康度），但只对 now 子组展开叶子（保留
        # v0.5.3 精简意图）。混在子组里的真节点（如 local-N）按 4 列表格
        # flush。selector/fallback 全为真节点 / 其他类型组：原 print_members
        # 行为不变。
        if g.get("type") in ("Selector", "Fallback"):
            has_subgroup = any(proxies.get(m, {}).get("all") for m in gmembers)
            if not has_subgroup:
                print_members(gmembers, gnow)
            else:
                leaf_buf: list = []

                def flush_leaves():
                    if leaf_buf:
                        print_members(leaf_buf, gnow)
                        leaf_buf.clear()

                for m in gmembers:
                    sub = proxies.get(m, {})
                    if sub.get("all"):
                        flush_leaves()
                        already = print_sub_summary(m, sub, is_now=(m == gnow))
                        if m == gnow and not already:
                            print_members(sub.get("all", []),
                                          sub.get("now", "?"))
                        expanded_subgroups.add(m)
                    else:
                        leaf_buf.append(m)
                flush_leaves()
        else:
            print_members(gmembers, gnow)
        # 顶层组本身展开完后也标记，避免被下层组重新展开
        expanded_subgroups.add(gname)

    # 跟踪哪些组已经作为 selector 子组展开过，避免重复
    shown = set()
    for gname in (groups or ["proxy"]):
        if gname in shown:
            continue
        g = proxies.get(gname)
        if not g:
            continue
        print_group(gname)
        if collect_into is not None:
            members = g.get("all", [])
            now = g.get("now", "")
            collect_into.append({
                "name": gname,
                "type": g.get("type", ""),
                "now": now,
                "tested_ago": group_tested_ago(members),
                "members": [
                    {"name": m, "delay_ms": get_delay(m), "is_now": m == now}
                    for m in members
                ],
            })
        # selector/fallback 展开的子组标记为已显示
        if g.get("type") in ("Selector", "Fallback"):
            for m in g.get("all", []):
                if proxies.get(m, {}).get("all"):
                    shown.add(m)


    return True


def _ipgeo(ip: str, cache_file: str, api_secret: str,
            proxy_port: int = 7890) -> str:
    """查询 IP 归属地，带文件缓存。返回 'city,country|org' 格式。"""
    if not ip:
        return ""
    if os.path.isfile(cache_file):
        for line in open(cache_file):
            if line.startswith(f"{ip}|"):
                return line.split("|", 1)[1].strip()
    env = {k: v for k, v in os.environ.items()
           if k not in ("http_proxy", "https_proxy", "all_proxy",
                        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    # Geo lookup is metadata lookup for an already-known IP; it should not be
    # routed through the proxy under test. In particular, users may route
    # ipinfo.io itself through the claude group, so a claude outage must not
    # hide the direct/proxy geo labels.
    r = subprocess.run(
        ["curl", "-s", "--max-time", "6", "--noproxy", "*",
         f"http://ip-api.com/json/{ip}?fields=status,countryCode,city,isp,org,query"],
        capture_output=True, text=True, env=env, timeout=10
    )
    if not r.stdout:
        return ""
    try:
        d = json.loads(r.stdout)
        city    = d.get("city", "")
        country = d.get("countryCode") or d.get("country", "")
        org     = d.get("isp") or d.get("org", "")
        if org and _re_mod.match(r"^AS\d+\s+", org):
            parts = org.split(" ", 1)
            org = parts[1] if len(parts) > 1 else org
        loc = ",".join(filter(None, [city, country]))
        result = f"{loc}|{org}"
        with open(cache_file, "a") as f:
            f.write(f"{ip}|{result}\n")
        return result
    except Exception:
        return ""


def _fmt_ip(ip: str, geo: str) -> str:
    if not ip:
        return f"{YELLOW}{'failed':<15s}{NC}"
    loc = geo.split("|")[0] if geo else ""
    org = geo.split("|")[1] if geo and "|" in geo else ""
    detail = ", ".join(filter(None, [loc, org]))
    if detail:
        return f"{ip:<15s} {CYAN}{detail}{NC}"
    return f"{ip:<15s}"


def cmd_bench(api: str, api_secret: str, groups: list = None,
              default_groups: list[str] | None = None):
    """
    proxyctl bench [group...] — 对指定代理组的全部节点发起测速。

    --json 模式：每节点完成立即输出一行 NDJSON
        {"node": str, "rtt_ms": int|null, "ok": bool, "error": str|null}
    末尾再输出一行 envelope（schema v1）汇总 summary。
    """
    import sys as _sys
    try:
        from proxyctl.explain import GLOBAL_FLAGS_REF as _gf
        as_json = bool(_gf().get("json"))
    except Exception:
        as_json = False

    DEFAULT_GROUPS = default_groups or ["proxy"]
    TEST_URL       = "https://www.gstatic.com/generate_204"
    TIMEOUT_MS     = 5000
    MAX_WORKERS    = 16

    target_groups = groups if groups else DEFAULT_GROUPS

    r = subprocess.run(
        ["curl", "-s", "--noproxy", "*",
         "-H", f"Authorization: Bearer {api_secret}",
         f"{api}/proxies"],
        capture_output=True, text=True, timeout=5
    )
    if not r.stdout.strip():
        if as_json:
            from proxyctl import _io
            _io.fail("Clash API unreachable",
                     hint="proxyctl start",
                     doc="troubleshooting",
                     code=_io.NETWORK_ERR, cmd="bench", as_json=True)
        print(f"  {YELLOW}—{NC} Clash API 不可达")
        return

    try:
        proxies = json.loads(r.stdout).get("proxies", {})
    except Exception:
        if as_json:
            from proxyctl import _io
            _io.fail("API response parse failed",
                     hint="proxyctl log --tail 50 --no-follow",
                     doc="troubleshooting",
                     code=_io.NETWORK_ERR, cmd="bench", as_json=True)
        print(f"  {YELLOW}—{NC} API 响应解析失败")
        return

    # v0.5.3：穿透 selector 子组到真叶子节点；伪节点（DIRECT/REJECT）排除
    from proxyctl.suggest_rules import _collect_leaves, PSEUDO_NODE_TYPES
    group_members: dict = {}
    raw_count_total = 0  # 去重前总和（用于显示"省了多少次"）
    for gname in target_groups:
        g = proxies.get(gname)
        if not g:
            if not as_json:
                print(f"  {YELLOW}—{NC} 组 {BOLD}{gname}{NC} 不存在，跳过")
            continue
        leaves = [
            l for l in _collect_leaves(gname, proxies, seen=set())
            if proxies.get(l, {}).get("type") not in PSEUDO_NODE_TYPES
        ]
        if not leaves:
            if not as_json:
                print(f"  {YELLOW}—{NC} 组 {BOLD}{gname}{NC} 无可测叶子节点")
            continue
        group_members[gname] = leaves
        raw_count_total += len(leaves)

    if not group_members:
        if as_json:
            from proxyctl import _io
            _io.fail("No testable group",
                     hint="proxyctl status --json  # 看已知组名",
                     doc="nodes",
                     code=_io.NOT_FOUND, cmd="bench", as_json=True)
        print(f"  {RED}✗{NC} 无可测组")
        return

    seen: set = set()
    all_nodes: list = []
    for members in group_members.values():
        for m in members:
            if m not in seen:
                all_nodes.append(m)
                seen.add(m)

    total = len(all_nodes)
    dedup_saved = raw_count_total - total  # 去重省掉的探测次数
    group_names = ", ".join(group_members.keys())
    if not as_json:
        dedup_note = (f"  {DIM}(去重省 {dedup_saved} 次){NC}"
                      if dedup_saved > 0 else "")
        print(f"{BOLD}测速{NC}  组: {CYAN}{group_names}{NC}  "
              f"节点: {BOLD}{total}{NC}{dedup_note}")

    import threading
    done_count = [0]
    lock = threading.Lock()
    results: list = []

    def _test_node(name: str):
        encoded_name = urllib.parse.quote(name, safe="")
        encoded_url  = urllib.parse.quote(TEST_URL, safe="")
        endpoint = (f"{api}/proxies/{encoded_name}/delay"
                    f"?url={encoded_url}&timeout={TIMEOUT_MS}")
        rr = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "--max-time",
             str(TIMEOUT_MS // 1000 + 3),
             "-H", f"Authorization: Bearer {api_secret}",
             endpoint],
            capture_output=True, text=True, timeout=TIMEOUT_MS // 1000 + 5
        )
        rtt: int | None = None
        err: str | None = None
        try:
            body = json.loads(rr.stdout)
            if "delay" in body:
                rtt = int(body["delay"])
            else:
                err = body.get("message") or "no-delay"
        except Exception as e:
            err = f"parse-error: {e}"

        with lock:
            done_count[0] += 1
            n = done_count[0]
            entry = {"node": name, "rtt_ms": rtt,
                     "ok": rtt is not None, "error": err}
            results.append(entry)
            if as_json:
                # NDJSON：每节点完成立即流式输出一行
                _sys.stdout.write(json.dumps(entry, ensure_ascii=False) + "\n")
                _sys.stdout.flush()
            else:
                bar_len = 24
                filled  = int(bar_len * n / total)
                bar     = f"{GREEN}{'█' * filled}{NC}{'░' * (bar_len - filled)}"
                print(f"\r  [{bar}] {n}/{total}", end="", flush=True)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        list(pool.map(_test_node, all_nodes))

    if as_json:
        from proxyctl._io import emit_json, envelope, OK
        ok_count = sum(1 for r in results if r["ok"])
        rtts = [r["rtt_ms"] for r in results if r["ok"]]
        summary = {
            "groups": list(group_members.keys()),
            "total": total,
            "raw_count": raw_count_total,  # 去重前各组叶子数之和
            "dedup_saved": dedup_saved,    # 重复线路省下的探测次数
            "ok_count": ok_count,
            "fail_count": total - ok_count,
            "avg_rtt_ms": (sum(rtts) // len(rtts)) if rtts else None,
            "min_rtt_ms": min(rtts) if rtts else None,
            "max_rtt_ms": max(rtts) if rtts else None,
            "results": results,
        }
        emit_json(envelope("bench", data=summary, ok=ok_count > 0,
                           code=OK if ok_count > 0 else 1))
        _sys.exit(0 if ok_count > 0 else 1)

    print()
    print()
    _proxy_groups_section(api, api_secret, groups=list(group_members.keys()))


def _fetch_probe(probe, env_clean: dict, proxy_port: int = 7890) -> str:
    """根据 OutboundProbe 配置发起一次 IP 查询，返回提取后的 IP。"""
    cmd = ["curl", "-s", "--max-time", str(probe.timeout)]
    if probe.mode == "proxy":
        cmd += ["--proxy", f"socks5h://127.0.0.1:{proxy_port}"]
    else:
        cmd += ["--noproxy", "*"]
    cmd.append(probe.url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env=env_clean, timeout=probe.timeout + 4)
    except subprocess.TimeoutExpired:
        return ""
    text = (r.stdout or "").strip()
    if probe.extract_re:
        m = _re_mod.search(probe.extract_re, text)
        return m.group(0) if m else ""
    return text


def _collect_fail_hints(collector: dict, *, dns_bad: bool, failed: bool) -> list[str]:
    """从 check 的 collector 提取每个失败 stage 的简短摘要。

    设计目标：envelope.ok=False 时 agent 不需要挖 stages.*.ok 自行定位真凶。
    本函数为 cmd_check 在 emit_json 之前调用，输出 hints 列表。

    Args:
        collector: cmd_check 沉淀的事实快照（见 cmd_check.collector）
        dns_bad:   basic.network.dns 不健康
        failed:    整体是否任一 stage fail（fail 变量真值）

    Returns:
        摘要 hint 行列表。整体 pass 时返回空列表。
    """
    hints: list[str] = []
    if not failed:
        return hints

    stages = collector.get("stages") or {}
    basic = stages.get("basic") or {}
    ports = basic.get("ports") or {}
    fail_ports = ports.get("fail") or []
    if fail_ports:
        hints.append(f"missing ports: {' '.join(fail_ports)}")

    if basic.get("daemon_up") is False:
        hints.append("engine not running")

    # connectivity: 列表形式，每项 {name, url, mode, line, route_chain, ok, message}
    conn = stages.get("connectivity") or []
    failed_conns = [c.get("name", "?") for c in conn
                    if isinstance(c, dict) and not c.get("ok")]
    if failed_conns:
        hints.append(f"connectivity failed: {','.join(failed_conns)}")

    split = stages.get("split_routing")
    if isinstance(split, dict) and split.get("ok") is False:
        hints.append("split routing inactive (proxy == direct egress)")

    if dns_bad:
        # 保留旧行为：DNS 异常时引导 proxyctl fix
        hints.append("DNS unhealthy — try `proxyctl fix`")

    dead = stages.get("dead_groups") or []
    if dead:
        names = ",".join(
            f"{d.get('name','?')}({d.get('dead_count')}/{d.get('total_count')})"
            for d in dead if isinstance(d, dict))
        hints.append(f"proxy groups mostly dead: {names} — try `proxyctl bench`")

    return hints


def cmd_check(engine, api: str, api_secret: str,
              config: dict, mode_str: str = "", registry=None):
    """proxyctl check — 4 阶段全面健康检查。

    --json 模式：丢弃人类打印，收集关键事实到 collector，末尾输出 envelope。
    """
    import io as _io_mod
    import sys as _sys
    try:
        from proxyctl.explain import GLOBAL_FLAGS_REF as _gf
        _flags = _gf()
        as_json = bool(_flags.get("json"))
        as_plain = bool(_flags.get("plain"))
    except Exception:
        as_json = False
        as_plain = False
    if as_json and as_plain:
        from proxyctl import _io as _pc_io
        _pc_io.fail("--json 与 --plain 互斥",
                    hint="选择其一", code=_pc_io.USAGE, cmd="check")
    collector: dict = {
        "engine": engine.name,
        "mode": mode_str,
        "stages": {
            "basic": {"daemon_up": False, "pid": "", "uptime": "",
                      "ports": {"ok": [], "fail": []},
                      "claude_proxy": None,
                      "network": {}},
            "groups": "skipped_in_json_v1",
            "connectivity": [],
            "outbound_ip": {},
            "split_routing": None,
        },
    }
    _real_stdout = _sys.stdout
    if as_json or as_plain:
        _sys.stdout = _io_mod.StringIO()

    dns_lock_label = config.get("dns_lock_label", "com.proxyctl.dns-lock")
    claude_proxy_label = config.get("claude_proxy_label", "com.proxyctl.claude-proxy")
    sb_dir = config.get("config_dir", f"{HOME}/.config") + "/proxyctl"
    corp_dns = config.get("corp_dns", {}) or {}
    fail = False

    import threading

    env_clean = {k: v for k, v in os.environ.items()
                 if k not in ("http_proxy", "https_proxy", "all_proxy",
                              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}

    # [4/4] 出口 IP 探测：从插件收集到的 probes 决定有哪些出口
    probes = []
    if registry is not None:
        probes = _dedupe_outbound_probes(
            registry.collect("check_outbound_probes", ctx={}))
    probe_ips: dict[str, str] = {p.name: "" for p in probes}

    # 临时常量：probes 在端口解析之前启动，所以这里用 config 直接拿
    _probe_proxy_port = int(config.get("proxy_port", 7890))

    def _make_fetcher(probe):
        def _run():
            probe_ips[probe.name] = _fetch_probe(probe, env_clean,
                                                  proxy_port=_probe_proxy_port)
        return _run

    ip_threads = [threading.Thread(target=_make_fetcher(p)) for p in probes]
    for t in ip_threads:
        t.start()

    # ── 1. 基础状态 ──────────────────────────────────────────────────────────
    mode = mode_str  # 由调用方传入
    if mode == "tun":
        mode_tag = f"{GREEN}tun{NC}"
    elif mode == "proxy":
        mode_tag = f"{CYAN}proxy{NC}"
    else:
        mode_tag = f"{YELLOW}{mode}{NC}"
    print(f"{BOLD}[1/4] 基础状态{NC}  {BOLD}{GREEN}{engine.name}{NC} · {mode_tag}")

    # daemon 检测（平台感知）
    daemon_up = False
    pid = ""
    if IS_MACOS:
        r = subprocess.run(["launchctl", "print", engine.label],
                           capture_output=True, text=True)
        pid = next((l.split()[-1] for l in r.stdout.splitlines() if "pid =" in l), "")
        daemon_up = bool(pid and pid != "0")
    else:
        r = subprocess.run(["systemctl", "--user", "show", engine.unit,
                            "-p", "MainPID", "--value"],
                           capture_output=True, text=True)
        pid = r.stdout.strip()
        daemon_up = bool(pid and pid != "0")

    if daemon_up:
        r2 = subprocess.run(["ps", "-o", "etime=", "-p", pid],
                             capture_output=True, text=True)
        etime = r2.stdout.strip()
        print(f"  {GREEN}✓{NC} daemon PID {pid}, uptime {etime or '?'}")
        collector["stages"]["basic"]["daemon_up"] = True
        collector["stages"]["basic"]["pid"] = pid
        collector["stages"]["basic"]["uptime"] = etime
    else:
        print(f"  {RED}✗{NC} daemon not running — 执行 proxyctl start")
        collector["stages"]["basic"]["daemon_up"] = False
        if as_json:
            _sys.stdout = _real_stdout
            from proxyctl import _io
            _io.fail("daemon not running",
                     hint="proxyctl start",
                     doc="troubleshooting",
                     data=collector,
                     code=_io.ENGINE_DOWN, cmd="check", as_json=True)
        return

    # 端口检测（proxy 模式不检查 53）— 端口来自 config + api_base
    from urllib.parse import urlparse
    proxy_port = int(config.get("proxy_port", 7890))
    api_port = urlparse(api).port or 9090
    dns_hijack = mode in ("tun", "mixed")
    check_ports = [(53, "dns"), (proxy_port, "proxy"), (api_port, "api")] if dns_hijack \
                  else [(proxy_port, "proxy"), (api_port, "api")]
    ok_ports, fail_ports = [], []
    for port, desc in check_ports:
        if _port_listening(port):
            ok_ports.append(f"{desc}:{port}")
        else:
            fail_ports.append(f"{desc}:{port}")
            fail = True

    # claude-proxy（仅 macOS）
    cp_status = ""
    if IS_MACOS:
        cp_label = f"system/{claude_proxy_label}"
        r = subprocess.run(["sudo", "launchctl", "print", cp_label], capture_output=True)
        cp_found = r.returncode == 0
        # fallback：兼容 sb 遗留 label
        if not cp_found and claude_proxy_label != "com.singbox.claude-proxy":
            r = subprocess.run(["sudo", "launchctl", "print",
                                "system/com.singbox.claude-proxy"], capture_output=True)
            cp_found = r.returncode == 0
        if cp_found:
            cp_status = (f"{GREEN}claude-proxy✓{NC}" if _port_listening(7891)
                         else f"{YELLOW}claude-proxy(no-port){NC}")
        else:
            cp_status = f"{YELLOW}claude-proxy✗{NC}"

    if ok_ports:
        parts = f"  {GREEN}✓{NC} ports: {' '.join(ok_ports)}"
        if cp_status:
            parts += f"  {cp_status}"
        print(parts)
    if fail_ports:
        parts = f"  {RED}✗{NC} missing: {' '.join(fail_ports)}"
        if cp_status and not ok_ports:
            parts += f"  {cp_status}"
        print(parts)
    collector["stages"]["basic"]["ports"]["ok"] = ok_ports
    collector["stages"]["basic"]["ports"]["fail"] = fail_ports
    collector["stages"]["basic"]["claude_proxy"] = bool(cp_status and "✓" in cp_status) \
                                                    if IS_MACOS else None

    # 网络环境状态行
    if IS_MACOS:
        en0_r = subprocess.run(["ifconfig", "en0"], capture_output=True, text=True)
        en0_addr = next((l.split()[1] for l in en0_r.stdout.splitlines()
                         if l.strip().startswith("inet ") and len(l.split()) >= 2), "")
    else:
        # Linux：获取默认出口 IP
        en0_addr = ""
        r_route = subprocess.run(["ip", "route", "show", "default"],
                                 capture_output=True, text=True)
        default_iface = ""
        for i, tok in enumerate(r_route.stdout.split()):
            if tok == "dev" and i + 1 < len(r_route.stdout.split()):
                default_iface = r_route.stdout.split()[i + 1]
                break
        if default_iface:
            r_ip = subprocess.run(["ip", "-4", "addr", "show", default_iface],
                                  capture_output=True, text=True)
            for line in r_ip.stdout.splitlines():
                if "inet " in line:
                    en0_addr = line.split()[1].split("/")[0]
                    break
    # 企业网络检测：仅当配置了 corp_dns.server 时启用
    corp_net = False
    corp_via = ""
    corp_server = corp_dns.get("server", "")
    corp_test_domain = corp_dns.get("test_domain", "")
    corp_prefix = corp_dns.get("ip_prefix", "")  # 如 "30." "10.0."

    if corp_server and corp_prefix:
        # 检测是否在企业网络中（通过 IP 前缀匹配）
        if en0_addr.startswith(corp_prefix):
            corp_net = True
            corp_via = f"直连({en0_addr})"
        elif IS_MACOS:
            r2 = subprocess.run(["ifconfig", "-l"], capture_output=True, text=True)
            for iface in r2.stdout.split():
                if not iface.startswith("utun"):
                    continue
                ri = subprocess.run(["ifconfig", iface], capture_output=True, text=True)
                for line in ri.stdout.splitlines():
                    if f"inet {corp_prefix}" in line:
                        parts = line.split()
                        if len(parts) >= 2:
                            corp_net = True
                            corp_via = f"VPN({iface},{parts[1]})"
                            break
                if corp_net:
                    break

    infra = corp_via if corp_net else f"{YELLOW}no-corp{NC}"

    # DNS：仅在 tun/mixed 模式下检查系统 DNS 是否指向 127.0.0.1
    dns_hijack = mode in ("tun", "mixed")
    dns_bad = False

    if dns_hijack and IS_MACOS:
        r3 = subprocess.run(["scutil", "--dns"], capture_output=True, text=True)
        sys_dns = ""
        for line in r3.stdout.splitlines():
            if "nameserver[0]" in line:
                sys_dns = line.split()[-1]
                break
        if sys_dns == "127.0.0.1":
            infra += "  DNS✓"
        else:
            infra += f"  {RED}DNS→{sys_dns or '?'}{NC}"
            dns_bad = True
            fail = True
    elif dns_hijack:
        infra += f"  {YELLOW}DNS(需手动检查){NC}"
    else:
        infra += "  DNS(proxy模式)"

    # dns-lock 看门狗状态 — 任何模式下都重要（这是观察 watchdog 是否在跑的关键指标）
    if IS_MACOS:
        r4 = subprocess.run(["launchctl", "print", f"system/{dns_lock_label}"],
                            capture_output=True)
        # fallback：兼容 sb 时代的 com.singbox.dns-lock label
        lock_ok = r4.returncode == 0
        if not lock_ok and dns_lock_label != "com.singbox.dns-lock":
            r4b = subprocess.run(["launchctl", "print",
                                  "system/com.singbox.dns-lock"],
                                 capture_output=True)
            lock_ok = r4b.returncode == 0
        infra += ("  lock✓" if lock_ok else f"  {YELLOW}no-lock{NC}")

    # 最近 watchdog tuic-recover/tuic-stuck 时间（昨天加的观察点）
    for log_path in (f"{sb_dir}/dns-watchdog.log",
                      f"{HOME}/.config/sing-box/dns-watchdog.log"):
        if os.path.isfile(log_path):
            try:
                for line in reversed(open(log_path).readlines()):
                    if "[tuic-recover]" in line or "[tuic-stuck]" in line:
                        ts = " ".join(line.split()[:2])
                        infra += f"  {CYAN}last-recover:{ts}{NC}"
                        break
                else:
                    continue
                break
            except (OSError, ValueError):
                pass
    print(f"  {infra}")
    collector["stages"]["basic"]["network"] = {
        "en0": en0_addr, "corp_net": corp_net, "corp_via": corp_via,
        "dns_bad": dns_bad, "mode_dns_hijack": dns_hijack,
    }

    # ── 2. 代理组 ─────────────────────────────────────────────────────────────
    print(f"{BOLD}[2/4] 代理组{NC}")
    groups = []
    if registry is not None:
        groups = registry.collect("check_groups")
    groups = _merge_check_groups(
        groups,
        _rule_target_groups_from_config(getattr(engine, "config_file", "")),
    )
    groups_data: list = []
    _proxy_groups_section(api, api_secret, groups=groups,
                          collect_into=groups_data if as_json else None)
    if as_json:
        collector["stages"]["groups"] = groups_data

    # 任何组 ≥70% 节点挂掉的全局判定（不限于 check_groups 显示的组）
    # 与 doctor 的 proxy_group.mostly_dead suggestion 共用底层规则。
    from proxyctl import suggest_rules as _sr
    proxies_payload = _sr.fetch_proxies(api, api_secret, timeout=1.0)
    dead_groups = _sr.proxy_group_rules(proxies_payload)
    if dead_groups:
        for s in dead_groups:
            ev = s.get("evidence") or {}
            print(f"  {RED}✗{NC} {ev.get('group_name','?')}: "
                  f"{ev.get('dead_count')}/{ev.get('total_count')} 节点不可达 "
                  f"({ev.get('dead_pct_at_check')}%)")
        fail = True
    collector["stages"]["dead_groups"] = [
        {"name": (s.get("evidence") or {}).get("group_name"),
         "dead_count": (s.get("evidence") or {}).get("dead_count"),
         "total_count": (s.get("evidence") or {}).get("total_count"),
         "dead_pct": (s.get("evidence") or {}).get("dead_pct_at_check")}
        for s in dead_groups
    ]

    # ── 3. 连通性 ─────────────────────────────────────────────────────────────
    # 从所有插件收集 check_targets。corp-network 等内置插件根据 ctx.corp_net 决定是否启用。
    ctx = {"corp_net": corp_net, "mode": mode, "engine": engine.name}
    targets = []
    if registry is not None:
        targets = _dedupe_check_targets(
            registry.collect("check_targets", ctx=ctx))

    # 应用 only_when 过滤
    def _target_enabled(t):
        if not getattr(t, "only_when", None):
            return True
        try:
            return bool(t.only_when(ctx))
        except Exception:
            return True
    targets = [t for t in targets if _target_enabled(t)]

    # [3/4] 连通性：每个测试完成立刻打印，不等其他测试
    print(f"{BOLD}[3/4] 连通性{NC}")

    if not targets:
        print(f"  {YELLOW}—{NC} 无连通性测试项（请在 ~/.config/proxyctl/plugins/ "
              f"放插件提供 check_targets）")

    results  = [None] * len(targets)
    ready    = [threading.Event() for _ in targets]

    def _run_test(idx, target):
        route_line = _connectivity_line_value(target.mode)
        route_chain = ""
        route_note = ""
        try:
            if target.mode == "dns":
                # url 格式: dns:<server>:<domain>
                _, server, domain = target.url.split(":", 2)
                ok, line = _test_dns(target.name, server, domain)
            elif target.mode == "tcp":
                parts = target.url.removeprefix("tcp:").rsplit(":", 1)
                ok, line = _test_tcp(parts[0], int(parts[1]), target.name)
            else:
                ok, line = _test_url(target.url, target.name,
                                     target.mode, target.timeout,
                                     proxy_port=proxy_port)
                expected = getattr(target, "expected_proxy", "")
                if target.mode == "proxy" and ok:
                    route = _target_route(api, api_secret, target.url)
                    route_line = str(route.get("line") or "?")
                    route_chain = str(route.get("chain") or "")
                    if expected:
                        route_ok, route_msg = _route_matches_expected_proxy(
                            route, expected)
                        ok = route_ok
                        if route_ok:
                            route_note = f"  {GREEN}{route_msg}{NC}"
                        else:
                            route_note = f"  {RED}{route_msg}{NC}"
            line = _append_route_column(line, route_line, route_chain)
            line += route_note
        except Exception as e:
            ok, line = False, f"  {RED}✗{NC} {target.name}  error: {e}"
            line = _append_route_column(line, route_line, route_chain)
        results[idx] = (line, ok, route_line, route_chain)
        ready[idx].set()

    if targets:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for idx, target in enumerate(targets):
                pool.submit(_run_test, idx, target)
            for idx in range(len(targets)):
                ready[idx].wait()
                line, ok, route_line, route_chain = results[idx]
                print(line)
                if not ok:
                    fail = True
                collector["stages"]["connectivity"].append({
                    "name": targets[idx].name,
                    "url": targets[idx].url,
                    "mode": targets[idx].mode,
                    "line": route_line,
                    "route_chain": route_chain,
                    "ok": bool(ok),
                    "message": _strip_ansi(line),
                })

    # [4/4] 出口 IP：等 IP 线程结束并展示每个 probe 的结果
    print(f"{BOLD}[4/4] 出口 IP{NC}")
    for t in ip_threads:
        t.join()

    cache_file = os.path.join(sb_dir, ".ipgeo-cache")
    if not probes:
        print(f"  {YELLOW}—{NC} 无出口探测项")
    geo_results: dict[str, str] = {p.name: "" for p in probes}
    geo_threads = [
        threading.Thread(
            target=lambda p=probe: geo_results.__setitem__(
                p.name,
                _ipgeo(probe_ips.get(p.name, ""), cache_file, api_secret,
                       proxy_port=proxy_port),
            )
        )
        for probe in probes
    ]
    for t in geo_threads:
        t.start()
    for t in geo_threads:
        t.join()
    for probe in probes:
        ip = probe_ips.get(probe.name, "")
        geo = geo_results.get(probe.name, "")
        print(f"  {probe.name:<7s}{_fmt_ip(ip, geo)}")
        collector["stages"]["outbound_ip"][probe.name] = {
            "ip": ip, "geo": geo, "mode": getattr(probe, "mode", ""),
        }

    # 分流校验：当同时有 proxy 出口和 direct 出口时比较
    proxy_probe_ip  = next((probe_ips[p.name] for p in probes
                            if p.mode == "proxy" and p.name == "proxy"), "")
    direct_probe_ip = next((probe_ips[p.name] for p in probes
                            if p.mode == "direct"), "")
    if proxy_probe_ip and direct_probe_ip:
        if proxy_probe_ip != direct_probe_ip:
            print(f"  {GREEN}✓{NC} 分流正常")
            collector["stages"]["split_routing"] = {
                "ok": True,
                "proxy_ip": proxy_probe_ip, "direct_ip": direct_probe_ip,
            }
        else:
            print(f"  {YELLOW}!{NC} 出口相同 — 检查分流规则")
            collector["stages"]["split_routing"] = {
                "ok": False,
                "proxy_ip": proxy_probe_ip, "direct_ip": direct_probe_ip,
            }

    # ── 结果 ──────────────────────────────────────────────────────────────────
    print()
    if not fail:
        print(f"{GREEN}{BOLD}All checks passed.{NC}")
    else:
        print(f"{YELLOW}{BOLD}Some checks failed.{NC}")
        if dns_bad:
            print(f"{CYAN}DNS 异常，执行 {BOLD}sb fix{NC}{CYAN} 修复。{NC}")

    if not as_json and not as_plain:
        _sys.exit(0 if not fail else 1)

    if as_json:
        _sys.stdout = _real_stdout
        from proxyctl._io import emit_json, envelope, OK, GENERIC
        # 失败时聚合每个失败 stage 的简短摘要到 hints。
        # 不让 agent 在 envelope.ok=False 时还要挖 stages.*.ok 才定位真凶。
        hints = _collect_fail_hints(collector, dns_bad=dns_bad, failed=fail)
        emit_json(envelope("check", data=collector,
                           ok=(not fail),
                           code=OK if not fail else GENERIC,
                           hints=hints))
        _sys.exit(0 if not fail else 1)
    if as_plain:
        _sys.stdout = _real_stdout
        from proxyctl._io import emit_tsv
        # 4 行 TSV：stage / ok / detail
        stages = collector["stages"]
        basic = stages.get("basic", {})
        conn = stages.get("connectivity") or []
        out_ip = stages.get("outbound_ip") or {}
        rows = [
            {"stage": "basic", "ok": bool(basic.get("daemon_up")),
             "detail": f"pid={basic.get('pid','?')} uptime={basic.get('uptime','?')}"},
            {"stage": "groups", "ok": True,  # groups 阶段在 --json/--plain 模式被跳过
             "detail": "skipped_in_structured_modes"},
            {"stage": "connectivity",
             "ok": all(c.get("ok") for c in conn) if conn else False,
             "detail": ";".join(f"{c.get('name')}={'ok' if c.get('ok') else 'X'}"
                                 for c in conn)},
            {"stage": "outbound_ip",
             "ok": bool(out_ip),
             "detail": ";".join(f"{k}={(v or {}).get('ip','?')}"
                                  for k, v in out_ip.items())},
        ]
        emit_tsv(rows, cols=["stage", "ok", "detail"])
        _sys.exit(0 if not fail else 1)
