"""测试共享 fixture & 环境隔离。

核心原则：
- 不允许任何测试触达真实文件系统中用户的 ~/.config/proxyctl 配置
- 不允许任何测试发起真实子进程到 curl / dig / launchctl / systemctl
- 不允许任何测试打开网络套接字（除 localhost loopback 在显式 monkeypatch 的 fixture 下）

所有跨模块的环境兜底都集中在这里，避免散落。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 HOME 重定向到 tmp_path，避免污染真实用户配置。

    proxyctl 大量使用 ``os.path.expanduser("~")`` 在模块加载期就拼出路径常量
    （audit.HOME / cli.HOME 等），所以这里同时 monkeypatch HOME 环境变量，
    用例侧可通过返回的 ``home`` Path 写测试用 fixture 文件。
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))   # Windows 兼容（CI 上不一定跑）
    return home


@pytest.fixture(autouse=True)
def _reset_proxyctl_globals():
    """每个测试前后重置 proxyctl 的进程级全局态。

    包括：
      - _io 的 _JSON_MODE / _T0_NS / _REQUEST_ID
      - explain 的 _GLOBAL_FLAGS（被 cli.main → set_global_flags 共享）
      - cli.GLOBAL_FLAGS 本身
    这些状态在跨测试泄漏时会导致诡异 fail（例如 completion 突然吐 JSON envelope）。
    """
    # 测试前：reset 一次（避免上一个测试遗留）
    try:
        from proxyctl import _io as _pc_io
        _pc_io._JSON_MODE = False  # type: ignore[attr-defined]
        _pc_io._T0_NS = None       # type: ignore[attr-defined]
        _pc_io._REQUEST_ID = None  # type: ignore[attr-defined]
        _pc_io._FORCE_NO_COLOR = False  # type: ignore[attr-defined]
    except ImportError:
        pass
    try:
        from proxyctl import explain as _pc_explain
        _pc_explain.set_global_flags(
            {"json": False, "no_color": False, "quiet": False,
             "dry_run": False, "plain": False})
    except ImportError:
        pass
    try:
        from proxyctl import cli as _pc_cli
        _pc_cli.GLOBAL_FLAGS.update(
            {"json": False, "no_color": False, "quiet": False,
             "dry_run": False, "plain": False})
    except ImportError:
        pass
    yield
    # 测试后再 reset 一次（保险）
    try:
        from proxyctl import _io as _pc_io
        _pc_io._JSON_MODE = False  # type: ignore[attr-defined]
        _pc_io._T0_NS = None       # type: ignore[attr-defined]
        _pc_io._REQUEST_ID = None  # type: ignore[attr-defined]
    except ImportError:
        pass
    try:
        from proxyctl import explain as _pc_explain
        _pc_explain.set_global_flags(
            {"json": False, "no_color": False, "quiet": False,
             "dry_run": False, "plain": False})
    except ImportError:
        pass


@pytest.fixture
def fake_subprocess(monkeypatch: pytest.MonkeyPatch):
    """提供可编程的 subprocess.run mock。

    用法::

        def test_xxx(fake_subprocess):
            fake_subprocess.set_result(["curl", "..."], stdout="ok", returncode=0)
            ...

    匹配优先级：完全匹配 > 命令头匹配 > 默认。
    未配置匹配规则则抛 AssertionError，避免静默漏测。
    """
    import subprocess as _sp

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[list] = []
            self._exact: dict[tuple, _sp.CompletedProcess] = {}
            self._prefix: list[tuple[tuple, _sp.CompletedProcess]] = []
            self._default: _sp.CompletedProcess | None = None

        def set_result(self, cmd, *, stdout: str = "", stderr: str = "",
                       returncode: int = 0) -> None:
            cp = _sp.CompletedProcess(cmd, returncode, stdout, stderr)
            self._exact[tuple(cmd)] = cp

        def set_prefix_result(self, prefix, *, stdout: str = "", stderr: str = "",
                              returncode: int = 0) -> None:
            cp = _sp.CompletedProcess(prefix, returncode, stdout, stderr)
            self._prefix.append((tuple(prefix), cp))

        def set_default(self, *, stdout: str = "", stderr: str = "",
                        returncode: int = 0) -> None:
            self._default = _sp.CompletedProcess([], returncode, stdout, stderr)

        def _resolve(self, cmd) -> _sp.CompletedProcess:
            key = tuple(cmd) if isinstance(cmd, list) else (cmd,)
            if key in self._exact:
                return self._exact[key]
            for prefix, cp in self._prefix:
                if key[: len(prefix)] == prefix:
                    return cp
            if self._default is not None:
                return self._default
            raise AssertionError(f"unexpected subprocess call: {cmd!r}")

    rec = _Recorder()

    def fake_run(cmd, *args, **kwargs):
        rec.calls.append(cmd)
        return rec._resolve(cmd)

    monkeypatch.setattr(_sp, "run", fake_run)
    return rec


@pytest.fixture
def repo_root() -> Path:
    """工程根目录，便于读 example/示例文件。"""
    return Path(__file__).resolve().parent.parent


# 让 src/ layout 包可被直接 import（pytest 不读 site-packages 内的 editable install
# 时这层保险）。
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
