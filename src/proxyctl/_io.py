"""proxyctl._io — 共享 I/O 基元

提供：
  - 退出码语义常量（OK / USAGE / ENGINE_DOWN / ... ）
  - 颜色/TTY 检测：should_color()、set_no_color()、maybe_disable_module_colors()
  - JSON envelope：envelope()、emit_json()
  - 错误退出 fail()
  - 并发锁 with_lock(name)
  - 信号处理 install_signal_handlers()
  - PROXYCTL_AGENT 一键模式探测 agent_mode_active()

设计要点：
  现有 5 个模块（cli/status/check/trace/audit）各自持有模块级 RED/GREEN/...
  常量并在 200+ 处 f-string 中使用。本模块用 monkey-patch 的方式
  在运行期把这些常量改写为空字符串以关色（NO_COLOR / 非 TTY / --no-color /
  --json / PROXYCTL_AGENT），避免逐行改 f-string。
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import time
from typing import Any, NoReturn

# ── 退出码 ────────────────────────────────────────────────────────────────
OK = 0
GENERIC = 1          # 旧路径保留，避免破坏外部脚本
USAGE = 2            # 未识别子命令、参数错
NOT_FOUND = 3        # 资源不存在（如 daemon 未声明）
PERMISSION = 4       # sudo / 写文件失败
ENGINE_DOWN = 5      # 引擎未运行
CONFIG_ERR = 6       # 配置文件语法/字段错
NETWORK_ERR = 7      # 上游 API / 网络错
LOCKED = 8           # 并发锁未拿到

EXIT_CODE_HELP: dict[int, str] = {
    OK:           "成功",
    GENERIC:      "通用失败（旧路径兼容）",
    USAGE:        "用法错（未识别命令/参数错）",
    NOT_FOUND:    "未找到资源（如 daemon 未声明）",
    PERMISSION:   "权限不足（sudo / 写文件失败）",
    ENGINE_DOWN:  "引擎未运行",
    CONFIG_ERR:   "配置文件语法 / 字段错",
    NETWORK_ERR:  "上游 API / 网络错",
    LOCKED:       "并发锁未拿到（另一个 proxyctl 写操作正在跑）",
}


# ── 颜色 / TTY 检测 ───────────────────────────────────────────────────────
_FORCE_NO_COLOR = False

_COLOR_NAMES = ("RED", "GREEN", "YELLOW", "CYAN", "BOLD", "DIM", "NC")
_COLOR_MODULES_KNOWN = (
    "proxyctl.cli", "proxyctl.status", "proxyctl.check",
    "proxyctl.trace", "proxyctl.audit",
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
    """子模块加载时调用：当前若处于关色态，则把该模块的颜色常量抹空。

    用法：每个子模块顶部加一行
        from proxyctl._io import maybe_disable_module_colors
        maybe_disable_module_colors(__name__)
    """
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


# ── JSON envelope ─────────────────────────────────────────────────────────
SCHEMA_VERSION = 1


def envelope(cmd: str, *, ok: bool = True, data: Any = None,
             error: str | None = None, code: int = OK,
             hint: str | None = None, doc: str | None = None) -> dict:
    """构造标准 envelope。失败时 ok=False / data=None / error/code/hint 填充。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "cmd": cmd,
        "ok": ok,
        "data": data,
        "error": error,
        "code": code,
        "hint": hint,
        "doc": doc,
    }


def emit_json(env: dict) -> None:
    """写 envelope 到 stdout（永远不带颜色）。"""
    print(json.dumps(env, ensure_ascii=False, indent=2))


# ── 错误退出 ──────────────────────────────────────────────────────────────
def fail(msg: str, *, hint: str | None = None, doc: str | None = None,
         code: int = GENERIC, cmd: str | None = None,
         as_json: bool = False) -> NoReturn:
    """统一错误退出。

    - JSON 模式：stdout 输出完整 envelope（ok:false），stderr 输出一行简短摘要
    - 人类模式：仅 stderr 输出彩色 ERROR / hint / doc

    `doc` 是 explain topic 名（不含 'proxyctl explain' 前缀）。
    """
    if as_json:
        emit_json(envelope(cmd or "", ok=False, error=msg,
                           code=code, hint=hint, doc=doc))
    red = "\033[0;31m" if should_color(sys.stderr) else ""
    yellow = "\033[0;33m" if should_color(sys.stderr) else ""
    nc = "\033[0m" if should_color(sys.stderr) else ""
    print(f"{red}ERROR{nc}: {msg}", file=sys.stderr)
    if hint:
        print(f"  {yellow}hint{nc}: {hint}", file=sys.stderr)
    if doc:
        print(f"  {yellow}doc{nc}:  proxyctl explain {doc}", file=sys.stderr)
    sys.exit(code)


# ── 并发锁 ────────────────────────────────────────────────────────────────
@contextlib.contextmanager
def with_lock(name: str = "default", *, timeout: float = 0.0,
              lock_dir: str | None = None):
    """文件锁（fcntl.flock），用于保护写操作。

    name: 锁文件后缀，按操作类型分锁（如 'config' / 'daemon'）。
    timeout: 0 立即失败；>0 重试间隔 0.2s 直到 deadline。

    拿不到锁则抛 BlockingIOError，调用方应捕获后用 LOCKED 退出码 fail。
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
                    raise
                time.sleep(0.2)
        yield lock_path
    finally:
        try:
            import fcntl as _fcntl
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)


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
    return os.environ.get("PROXYCTL_AGENT", "").strip().lower() in ("1", "true", "yes")
