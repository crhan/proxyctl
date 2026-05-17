"""proxyctl._io — 共享 I/O 基元（schema v2）

提供：
  - 退出码语义常量（OK / USAGE / ENGINE_DOWN / ... / TIMEOUT / DEPENDENCY_MISSING）
  - 颜色/TTY 检测：should_color()、set_no_color()、maybe_disable_module_colors()
  - JSON envelope v2：envelope()、emit_json()
  - NDJSON 表格输出：emit_tsv()
  - 错误退出 fail()
  - 并发锁 with_lock(name) + LockedError
  - 信号处理 install_signal_handlers()
  - 全局状态：set_json_mode / is_json_mode / set_invocation_t0 / new_request_id
  - PROXYCTL_AGENT 一键模式探测 agent_mode_active()
  - flag 解析工具 extract_flags()

设计要点：
  现有模块各自持有模块级 RED/GREEN/... 常量并在 200+ 处 f-string 中使用。
  本模块用 monkey-patch 把这些常量改写为空字符串以关色
  （NO_COLOR / 非 TTY / --no-color / --json / PROXYCTL_AGENT），避免逐行改 f-string。

  envelope schema v2（与 v1 相比）：
    + meta: { ts, elapsed_ms, proxyctl_version, request_id }
    + hints: list[str]（取代单 hint: str）
    + warnings: list[str]
    - hint 字段移除（无兼容）
  字段含义见 proxyctl explain agent-protocol 或 AGENTS.md。
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, NoReturn

# ── 退出码 ────────────────────────────────────────────────────────────────
OK = 0
GENERIC = 1            # 旧路径兼容
USAGE = 2              # 未识别子命令、参数错
NOT_FOUND = 3          # 资源不存在（如 daemon 未声明）
PERMISSION = 4         # sudo / 写文件失败
ENGINE_DOWN = 5        # 引擎未运行
CONFIG_ERR = 6         # 配置文件语法/字段错
NETWORK_ERR = 7        # 上游 API / 网络错
LOCKED = 8             # 并发锁未拿到
TIMEOUT = 9            # 命令超时（如 bench / curl 长跑）
DEPENDENCY_MISSING = 10  # mihomo / sing-box 二进制缺失等

EXIT_CODE_HELP: dict[int, str] = {
    OK:                  "成功",
    GENERIC:             "通用失败（旧路径兼容）",
    USAGE:               "用法错（未识别命令/参数错）",
    NOT_FOUND:           "未找到资源（如 daemon 未声明）",
    PERMISSION:          "权限不足（sudo / 写文件失败）",
    ENGINE_DOWN:         "引擎未运行",
    CONFIG_ERR:          "配置文件语法 / 字段错",
    NETWORK_ERR:         "上游 API / 网络错",
    LOCKED:              "并发锁未拿到（另一个 proxyctl 写操作正在跑）",
    TIMEOUT:             "命令超时",
    DEPENDENCY_MISSING:  "依赖缺失（mihomo / sing-box 二进制等）",
}


# ── 颜色 / TTY 检测 ───────────────────────────────────────────────────────
_FORCE_NO_COLOR = False

_COLOR_NAMES = ("RED", "GREEN", "YELLOW", "CYAN", "BOLD", "DIM", "NC")
_COLOR_MODULES_KNOWN = (
    "proxyctl.cli", "proxyctl.status", "proxyctl.check",
    "proxyctl.trace", "proxyctl.audit", "proxyctl.explain",
)


def should_color(stream=None) -> bool:
    """决策是否输出 ANSI 颜色，按 clig.dev 与 https://no-color.org/。"""
    if _FORCE_NO_COLOR:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("PROXYCTL_NO_COLOR"):
        return False
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def set_no_color(value: bool) -> None:
    """强制开关。True 时把已加载的颜色常量全部抹掉。"""
    global _FORCE_NO_COLOR
    _FORCE_NO_COLOR = value
    if value:
        for modname in _COLOR_MODULES_KNOWN:
            _patch_module_colors(modname)


def maybe_disable_module_colors(modname: str) -> None:
    """子模块加载时调用：当前若处于关色态，则把该模块的颜色常量抹空。"""
    if _FORCE_NO_COLOR:
        _patch_module_colors(modname)


def _patch_module_colors(modname: str) -> None:
    mod = sys.modules.get(modname)
    if mod is None:
        return
    for name in _COLOR_NAMES:
        if hasattr(mod, name):
            setattr(mod, name, "")


def auto_color_init() -> None:
    """入口调用一次：根据当前环境自动决定是否关色。"""
    if not should_color():
        set_no_color(True)


# ── 全局调用状态（json 模式 / 计时 / request_id）──────────────────────────
_JSON_MODE: bool = False
_T0_NS: int | None = None
_REQUEST_ID: str | None = None


def set_json_mode(value: bool) -> None:
    """cli.main() 入口调用，让子模块通过 is_json_mode() 取值，解循环导入。"""
    global _JSON_MODE
    _JSON_MODE = bool(value)


def is_json_mode() -> bool:
    return _JSON_MODE


def set_invocation_t0(ns: int | None = None) -> None:
    """cli.main() 入口调用记 t0，envelope() 自动算 elapsed_ms。"""
    global _T0_NS
    _T0_NS = ns if ns is not None else time.monotonic_ns()


def _elapsed_ms_now() -> int | None:
    if _T0_NS is None:
        return None
    return int((time.monotonic_ns() - _T0_NS) / 1_000_000)


def new_request_id() -> str:
    """生成并锁定本次调用的 request_id。多次调用返回同一个值。"""
    global _REQUEST_ID
    if _REQUEST_ID is None:
        _REQUEST_ID = uuid.uuid4().hex
    return _REQUEST_ID


def _proxyctl_version() -> str:
    try:
        from importlib.metadata import version
        return version("proxyctl")
    except Exception:
        return "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


# ── JSON envelope v2 ──────────────────────────────────────────────────────
SCHEMA_VERSION = 2


def envelope(cmd: str, *, ok: bool = True, data: Any = None,
             error: str | None = None, code: int = OK,
             hint: str | None = None,
             hints: list[str] | None = None,
             warnings: list[str] | None = None,
             doc: str | None = None,
             elapsed_ms: int | None = None) -> dict:
    """构造标准 envelope v2。

    `hint` 参数（单条字符串）会自动包装为 `hints: [hint]`，方便迁移老调用方；
    若 `hints` 显式给出则忽略 `hint`。

    `elapsed_ms` 不传时自动从 `set_invocation_t0()` 计算（用于 fail 路径）。
    """
    if hints is None:
        hints_list: list[str] = [hint] if hint else []
    else:
        hints_list = list(hints)
    warnings_list: list[str] = list(warnings) if warnings else []
    return {
        "schema_version": SCHEMA_VERSION,
        "cmd": cmd,
        "ok": ok,
        "data": data,
        "error": error,
        "code": code,
        "hints": hints_list,
        "warnings": warnings_list,
        "doc": doc,
        "meta": {
            "ts": _now_iso(),
            "elapsed_ms": elapsed_ms if elapsed_ms is not None else _elapsed_ms_now(),
            "proxyctl_version": _proxyctl_version(),
            "request_id": new_request_id(),
        },
    }


def emit_json(env: dict) -> None:
    """写 envelope 到 stdout（永远不带颜色）。"""
    print(json.dumps(env, ensure_ascii=False, indent=2))


# ── NDJSON / TSV 表格输出 ─────────────────────────────────────────────────
def emit_tsv(rows: list[dict], cols: list[str]) -> None:
    """写 TSV 到 stdout：首行表头，后续每行一条 row（tab 分隔）。

    单元格中的 tab/换行被替换为空格，确保列稳定。
    `--plain` 输出格式的核心实现，与 `--json` 互斥。
    """
    def _cell(v: Any) -> str:
        s = "" if v is None else str(v)
        return s.replace("\t", " ").replace("\n", " ")
    print("\t".join(cols))
    for r in rows:
        print("\t".join(_cell(r.get(c)) for c in cols))


# ── 错误退出 ──────────────────────────────────────────────────────────────
def fail(msg: str, *, hint: str | None = None,
         hints: list[str] | None = None,
         warnings: list[str] | None = None,
         doc: str | None = None,
         data: Any = None,
         code: int = GENERIC, cmd: str | None = None,
         as_json: bool | None = None) -> NoReturn:
    """统一错误退出。

    - JSON 模式：stdout 输出完整 envelope（ok:false），stderr 输出一行简短摘要
    - 人类模式：仅 stderr 输出彩色 ERROR / hints / doc

    `doc` 是 explain topic 名（不含 'proxyctl explain' 前缀）。
    `data` 用于"失败但有 partial result"的场景（如 check 已收集部分 stage）。
    `as_json` 不传时读全局 `is_json_mode()` —— 子模块无需手工传。
    """
    if as_json is None:
        as_json = is_json_mode()
    if as_json:
        emit_json(envelope(cmd or "", ok=False, data=data, error=msg,
                           code=code, hint=hint, hints=hints,
                           warnings=warnings, doc=doc))

    red = "\033[0;31m" if should_color(sys.stderr) else ""
    yellow = "\033[0;33m" if should_color(sys.stderr) else ""
    nc = "\033[0m" if should_color(sys.stderr) else ""
    print(f"{red}ERROR{nc}: {msg}", file=sys.stderr)

    eff_hints = list(hints) if hints else ([hint] if hint else [])
    for h in eff_hints:
        print(f"  {yellow}hint{nc}: {h}", file=sys.stderr)
    for w in (warnings or []):
        print(f"  {yellow}warn{nc}: {w}", file=sys.stderr)
    if doc:
        print(f"  {yellow}doc{nc}:  proxyctl explain {doc}", file=sys.stderr)
    sys.exit(code)


# ── flag 抽取工具（子命令位置无关）──────────────────────────────────────
def extract_flags(args: list[str],
                  known: dict[str, str]) -> tuple[list[str], dict[str, Any]]:
    """从 args 中剥离已知 flag，返回 (positional, flags_dict)。

    known 是 flag 名（带 --）到类型的映射：
      - "bool"：出现即 True（如 ``--no-follow``）
      - "value"：消耗下一个 token（如 ``--tail 50``）

    flags_dict 的 key 是 flag 名去掉前导 ``--`` 并把 ``-`` 转为 ``_``（如
    ``--no-follow`` → ``no_follow``）。

    clig.dev 原则：flag 位置无关——任意位置出现都被识别并剥离，
    位置参数顺序保持不变。未配置的 token 全部进入 positional。
    """
    flags: dict[str, Any] = {}
    positional: list[str] = []
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a in known:
            kind = known[a]
            key = a.lstrip("-").replace("-", "_")
            if kind == "bool":
                flags[key] = True
                i += 1
            elif kind == "value":
                if i + 1 < n:
                    flags[key] = args[i + 1]
                    i += 2
                else:
                    flags[key] = None
                    i += 1
            else:
                # 未知 kind 当 bool
                flags[key] = True
                i += 1
        else:
            positional.append(a)
            i += 1
    return positional, flags


# ── 并发锁 ────────────────────────────────────────────────────────────────
class LockedError(Exception):
    """fcntl.flock 拿不到锁时抛出，带 `lock_path` 属性供错误消息使用。"""

    def __init__(self, lock_path: str, lock_name: str = "") -> None:
        super().__init__(f"locked: {lock_path}")
        self.lock_path = lock_path
        self.lock_name = lock_name


@contextlib.contextmanager
def with_lock(name: str = "default", *, timeout: float = 0.0,
              lock_dir: str | None = None):
    """文件锁（fcntl.flock），用于保护写操作。

    name: 锁文件后缀，按操作类型分锁（如 'config' / 'daemon'）。
    timeout: 0 立即失败；>0 重试间隔 0.2s 直到 deadline。

    拿不到锁则抛 ``LockedError(lock_path)``，调用方应捕获后用 LOCKED 退出码 fail。
    """
    import fcntl  # macOS/Linux only
    home = os.path.expanduser("~")
    lock_dir = lock_dir or os.path.join(home, ".config", "proxyctl")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, f".lock.{name}")
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise LockedError(lock_path, lock_name=name)
                time.sleep(0.2)
        yield lock_path
    finally:
        try:
            import fcntl as _fcntl
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)


def held_lock_names(lock_dir: str | None = None) -> list[str]:
    """探测哪些锁当前被持有（fd 检测，best-effort）。

    用法：`doctor` 的 `lock_held` 字段；不是强保证，仅用于 informational。
    """
    import fcntl
    home = os.path.expanduser("~")
    lock_dir = lock_dir or os.path.join(home, ".config", "proxyctl")
    if not os.path.isdir(lock_dir):
        return []
    held = []
    for name in os.listdir(lock_dir):
        if not name.startswith(".lock."):
            continue
        lock_name = name[len(".lock."):]
        path = os.path.join(lock_dir, name)
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # 拿到了——说明此前没人持有；释放，跳过
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                held.append(lock_name)
        finally:
            os.close(fd)
    return held


def lock_paths(lock_dir: str | None = None) -> dict[str, str]:
    """返回所有可能的锁名 → 路径映射，供 doctor data 使用。"""
    home = os.path.expanduser("~")
    base = lock_dir or os.path.join(home, ".config", "proxyctl")
    # 已知锁名（与 cli._exec_with_lock 一致）
    names = ["system", "config", "daemon"]
    return {n: os.path.join(base, f".lock.{n}") for n in names}


# ── 信号 ──────────────────────────────────────────────────────────────────
def install_signal_handlers() -> None:
    """避免 `proxyctl ... | head` 触发 BrokenPipeError；Ctrl-C 退出 130。"""
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass

    def _on_sigint(_signum, _frame):
        sys.exit(130)
    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        pass


# ── PROXYCTL_AGENT 一键模式 ────────────────────────────────────────────────
def agent_mode_active() -> bool:
    """读取 PROXYCTL_AGENT；为 1/true/yes 时返回 True。"""
    return os.environ.get("PROXYCTL_AGENT", "").strip().lower() in (
        "1", "true", "yes")
