"""Agent CLI 代理变量托管（`PI_PROXY_*`）— v0.5.14+。

背景：通用 `HTTP(S)_PROXY` 并非所有 agent CLI 的 provider transport 都读。
omp（pi 系）的 anthropic-messages transport（`cowork-fetch`）只认
`PI_PROXY[_<PROVIDER>]`，没有就直连 → 地区封锁 `403 Request not allowed`；
usage 探针、OAuth 刷新等普通 fetch 却走 env proxy，于是出现"探针活着、
opus 请求全红"的错觉。

proxyctl 是 7890（引擎）与 7891（claude-proxy 逃生通道）的持有者，所以由它
把这组变量写成一个**可 source 的契约文件**：

    ~/.config/proxyctl/agent-env.sh

渲染规则：
  - 目标出口按「引擎端口在听 → 用引擎；否则兜底 daemon 端口在听 → 用它；
    都不在 → 删文件」，所以 start/stop/fix/daemon 之后文件永远指向活着的口。
  - 只写 provider 级变量（`PI_PROXY_ANTHROPIC`），**不写**通用 HTTP_PROXY——
    通用变量由用户 shell 静态管理，引擎停了也不该被 proxyctl 悄悄摘掉。

契约文件首行是机器可读的元信息（source/endpoint/providers），`load()` 依赖它，
不要手改；`proxyctl env --write` 与生命周期命令都会重写。
"""

from __future__ import annotations

import os
import re
import socket
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_PATH = "~/.config/proxyctl/agent-env.sh"
DEFAULT_SHELL_RC = "~/.zprofile"

# config.yaml 的 agent_env: 段默认值（用户段整体覆盖同名字段）
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # provider 名 → 变量名 PI_PROXY_<PROVIDER>（非字母数字转下划线、大写）
    "providers": ["anthropic"],
    # 引擎不可用时的兜底出口：config.extra_daemons 里的名字（None 关闭兜底）
    "fallback_daemon": "claude-proxy",
    # `proxyctl env --install` 写入的 shell rc
    "shell_rc": DEFAULT_SHELL_RC,
}

SHELL_BEGIN = "# >>> proxyctl agent-env >>>"
SHELL_END = "# <<< proxyctl agent-env <<<"

_HEADER_RE = re.compile(r"^#\s*proxyctl-agent-env\s+v(?P<ver>\d+)\s+(?P<kv>.*)$")
_EXPORT_RE = re.compile(r'^export\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)="(?P<value>[^"]*)"$')


# ── 路径 / 配置 ──────────────────────────────────────────────────────────────

def env_path() -> str:
    """契约文件路径。允许 PROXYCTL_AGENT_ENV_PATH 覆盖（测试用）。"""
    raw = os.environ.get("PROXYCTL_AGENT_ENV_PATH") or DEFAULT_PATH
    return os.path.expanduser(raw)


def shell_rc_path(cfg: dict[str, Any]) -> str:
    """shell rc 路径。PROXYCTL_SHELL_RC 优先于配置（测试用）。"""
    raw = (os.environ.get("PROXYCTL_SHELL_RC")
           or cfg.get("shell_rc") or DEFAULT_SHELL_RC)
    return os.path.expanduser(raw)


def resolve(config: dict[str, Any] | None) -> dict[str, Any]:
    """把 config.yaml 的 agent_env 段合并到默认值上，并规范化字段类型。"""
    cfg = dict(DEFAULTS)
    user = (config or {}).get("agent_env")
    if isinstance(user, dict):
        cfg.update(user)

    providers = cfg.get("providers")
    if isinstance(providers, str):
        providers = [p.strip() for p in providers.split(",")]
    cfg["providers"] = [str(p).strip() for p in (providers or []) if str(p).strip()]
    cfg["enabled"] = bool(cfg.get("enabled", True))
    if not cfg.get("fallback_daemon"):
        cfg["fallback_daemon"] = None
    return cfg


def provider_var(provider: str) -> str:
    """anthropic → PI_PROXY_ANTHROPIC（与 pi 系 CLI 的变量名规则一致）。"""
    return "PI_PROXY_" + re.sub(r"[^A-Z0-9]", "_", provider.upper())


def var_names(cfg: dict[str, Any]) -> list[str]:
    return [provider_var(p) for p in cfg.get("providers") or []]


# ── 出口解析 ────────────────────────────────────────────────────────────────

def _port_open(port: int, timeout: float = 0.5) -> bool:
    """TCP 连一下 127.0.0.1:port。测试 monkeypatch 这一层即可离线跑。"""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def target(config: dict[str, Any], cfg: dict[str, Any] | None = None) -> tuple[str | None, str]:
    """当前该指向的出口 → (endpoint, source)，source ∈ engine / daemon:<name> / none。"""
    cfg = cfg or resolve(config)
    engine_port = int((config or {}).get("proxy_port", 7890))
    if _port_open(engine_port):
        return f"http://127.0.0.1:{engine_port}", "engine"

    name = cfg.get("fallback_daemon")
    if name:
        daemon = ((config or {}).get("extra_daemons") or {}).get(name) or {}
        port = daemon.get("port")
        if port and _port_open(int(port)):
            return f"http://127.0.0.1:{int(port)}", f"daemon:{name}"
    return None, "none"


# ── 渲染 / 解析 ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def render(endpoint: str, source: str, cfg: dict[str, Any], *,
           now: str | None = None) -> str:
    """渲染契约文件全文（幂等：同输入同输出，便于比对避免无谓重写）。"""
    providers = ",".join(cfg.get("providers") or [])
    fallback = cfg.get("fallback_daemon") or "-"
    lines = [
        f"# proxyctl-agent-env v{SCHEMA_VERSION} generated={now or _now_iso()}"
        f" source={source} endpoint={endpoint} providers={providers}"
        f" fallback={fallback}",
        "# 由 `proxyctl env --write` 生成；start/stop/restart/fix 会自动刷新，勿手改。",
        "# 用法：在 shell rc 里 `. 本文件`（`proxyctl env --install` 会自动加）。",
    ]
    for provider in cfg.get("providers") or []:
        lines.append(f'export {provider_var(provider)}="{endpoint}"')
    return "\n".join(lines) + "\n"


def _parse_header(line: str) -> dict[str, Any] | None:
    m = _HEADER_RE.match(line)
    if not m:
        return None
    meta: dict[str, Any] = {}
    for token in m.group("kv").split():
        if "=" in token:
            k, v = token.split("=", 1)
            meta[k] = v
    meta["schema_version"] = int(m.group("ver"))
    return meta


def load() -> dict[str, Any] | None:
    """读契约文件 → state dict；文件不存在 / 头部无法解析 → None（无声）。"""
    p = env_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None

    meta: dict[str, Any] | None = None
    variables: dict[str, str] = {}
    for line in lines:
        if meta is None:
            parsed = _parse_header(line)
            if parsed is not None:
                meta = parsed
                continue
        m = _EXPORT_RE.match(line.strip())
        if m:
            variables[m.group("name")] = m.group("value")
    if meta is None:
        return None

    providers = [p for p in (meta.get("providers") or "").split(",") if p]
    fallback = meta.get("fallback")
    return {
        "schema_version": meta.get("schema_version"),
        "path": p,
        "generated_at": meta.get("generated"),
        "source": meta.get("source"),
        "endpoint": meta.get("endpoint"),
        "providers": providers,
        "fallback_daemon": None if fallback in (None, "-") else fallback,
        "vars": variables,
    }


# ── 写入 / 清除 ─────────────────────────────────────────────────────────────

def _write_atomic(path: str, text: str) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def clear() -> bool:
    """删除契约文件。返回是否真的删了。"""
    p = env_path()
    try:
        os.remove(p)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def sync(config: dict[str, Any]) -> dict[str, Any]:
    """按当前出口刷新（或清除）契约文件。

    Returns:
        {"action": "written" | "unchanged" | "removed" | "none",
         "path", "endpoint", "source", "vars", "changed"}
    """
    cfg = resolve(config)
    p = env_path()

    if not cfg["enabled"] or not cfg["providers"]:
        removed = clear()
        return {"action": "removed" if removed else "none", "path": p,
                "endpoint": None, "source": "none", "vars": {}, "changed": removed}

    endpoint, source = target(config, cfg)
    if not endpoint:
        removed = clear()
        return {"action": "removed" if removed else "none", "path": p,
                "endpoint": None, "source": "none", "vars": {}, "changed": removed}

    body = render(endpoint, source, cfg)
    # 头部含 generated= 时间戳 → 比对时忽略它，避免每次 start 都算"变了"
    old = ""
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            old = ""
    if _same_payload(old, body):
        return {"action": "unchanged", "path": p, "endpoint": endpoint,
                "source": source, "vars": _vars_for(endpoint, cfg), "changed": False}

    _write_atomic(p, body)
    return {"action": "written", "path": p, "endpoint": endpoint,
            "source": source, "vars": _vars_for(endpoint, cfg), "changed": True}


def _vars_for(endpoint: str, cfg: dict[str, Any]) -> dict[str, str]:
    return {provider_var(p): endpoint for p in cfg.get("providers") or []}


def _same_payload(old: str, new: str) -> bool:
    return _strip_header(old) == _strip_header(new)


def _strip_header(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if _HEADER_RE.match(line):
            # 去掉 generated=<iso> 与 source=... （source 由出口决定，保留）
            parts = [t for t in line.split() if not t.startswith("generated=")]
            out.append(" ".join(parts))
        else:
            out.append(line)
    return out


# ── shell rc 安装 ───────────────────────────────────────────────────────────

def _source_line() -> str:
    return f'[ -f "{env_path()}" ] && . "{env_path()}"'


def shell_rc_installed(cfg: dict[str, Any]) -> bool:
    rc = shell_rc_path(cfg)
    try:
        with open(rc, encoding="utf-8") as f:
            return SHELL_BEGIN in f.read()
    except OSError:
        return False


def install_shell(cfg: dict[str, Any]) -> dict[str, Any]:
    """把 source 块写进 shell rc（幂等）。返回 {rc, action}。"""
    rc = shell_rc_path(cfg)
    block = f"{SHELL_BEGIN}\n{_source_line()}\n{SHELL_END}\n"
    old = ""
    if os.path.isfile(rc):
        try:
            with open(rc, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            old = ""
    new = _replace_block(old, block)
    if new == old:
        return {"rc": rc, "action": "unchanged"}
    _write_atomic(rc, new)
    return {"rc": rc, "action": "updated" if old else "created"}


def uninstall_shell(cfg: dict[str, Any]) -> dict[str, Any]:
    """摘掉 shell rc 里的 source 块（幂等）。返回 {rc, action}。"""
    rc = shell_rc_path(cfg)
    if not os.path.isfile(rc):
        return {"rc": rc, "action": "missing"}
    try:
        with open(rc, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        return {"rc": rc, "action": "missing"}
    new = _replace_block(old, "")
    if new == old:
        return {"rc": rc, "action": "unchanged"}
    _write_atomic(rc, new)
    return {"rc": rc, "action": "removed"}


def _replace_block(text: str, block: str) -> str:
    """替换（或删除）marker 之间的块，保留文件其余内容与结尾换行风格。"""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == SHELL_BEGIN:
            skipping = True
            continue
        if skipping:
            if line.strip() == SHELL_END:
                skipping = False
            continue
        kept.append(line)
    body = "".join(kept).rstrip("\n")
    if not block:
        return body + "\n" if body else ""
    return (body + "\n" if body else "") + block


# ── 状态（status / doctor 输入）──────────────────────────────────────────────

def status(config: dict[str, Any]) -> dict[str, Any]:
    """契约文件当前状态 vs 期望状态，供 status 面板与 doctor 规则使用。"""
    cfg = resolve(config)
    state = load()
    expected_endpoint, expected_source = target(config, cfg)

    present = state is not None
    endpoint = state.get("endpoint") if state else None
    port = None
    if endpoint:
        try:
            from urllib.parse import urlparse
            port = urlparse(endpoint).port
        except Exception:
            port = None
    reachable = bool(port) and _port_open(int(port)) if port else False

    return {
        "enabled": cfg["enabled"],
        "path": env_path(),
        "providers": cfg["providers"],
        "present": present,
        "vars": (state or {}).get("vars") or {},
        "endpoint": endpoint,
        "source": (state or {}).get("source"),
        "generated_at": (state or {}).get("generated_at"),
        "reachable": reachable,
        "stale": bool(present and port and not reachable),
        "expected_endpoint": expected_endpoint,
        "expected_source": expected_source,
        "in_sync": bool(present and endpoint == expected_endpoint),
        "shell_rc": shell_rc_path(cfg),
        "shell_rc_installed": shell_rc_installed(cfg),
    }
