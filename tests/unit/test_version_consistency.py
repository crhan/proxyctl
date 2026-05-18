"""VERSION 三源一致性 guard。

0.3.1 commit 提到过 cli.VERSION 与 pyproject.toml 漂移；
0.3.2 commit message 自承"VERSION 漂移"事故；
0.4.0a1 review 把 __init__.__version__ 从 stale "0.3.2" 改为
importlib.metadata 动态读。

本测试是兜底回归保险：以 pyproject.toml 为唯一事实来源，
断言以下四个 surface 全部对齐：

  1. pyproject.toml [project] version
  2. proxyctl.__version__ (importlib.metadata 动态读)
  3. cli.VERSION
  4. envelope.meta.proxyctl_version
  5. cmd_version_print 输出的 data.version

任一漂移即 fail。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _pyproject_version() -> str:
    import tomllib
    pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    return tomllib.load(open(pyproject, "rb"))["project"]["version"]


def test_init_version_matches_pyproject():
    import proxyctl
    assert proxyctl.__version__ == _pyproject_version(), \
        f"proxyctl.__version__={proxyctl.__version__!r} != pyproject={_pyproject_version()!r}"


def test_cli_version_matches_pyproject():
    from proxyctl import cli
    assert cli.VERSION == _pyproject_version(), \
        f"cli.VERSION={cli.VERSION!r} != pyproject={_pyproject_version()!r}"


def test_envelope_meta_version_matches_pyproject():
    """任一 envelope 的 meta.proxyctl_version 必须与 pyproject 一致。"""
    from proxyctl import _io
    env = _io.envelope("test", data={"x": 1})
    assert env["meta"]["proxyctl_version"] == _pyproject_version(), \
        f"envelope.meta.proxyctl_version 与 pyproject 漂移"


def test_cmd_version_print_json_matches_pyproject(monkeypatch, capsys):
    """`proxyctl version --json` 的 data.version 必须与 pyproject 一致。"""
    from proxyctl import cli, explain, _io
    cli.GLOBAL_FLAGS["json"] = True
    explain.set_global_flags({"json": True, "no_color": True, "quiet": False,
                              "dry_run": False, "plain": False})
    _io.set_json_mode(True)
    with pytest.raises(SystemExit):
        cli.cmd_version_print()
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["version"] == _pyproject_version()
    assert env["meta"]["proxyctl_version"] == _pyproject_version()
    # 一致性双重保险
    assert env["data"]["version"] == env["meta"]["proxyctl_version"]


def test_version_subcommand_aliases_version_flag(monkeypatch, capsys):
    """0.4.2 新增 `proxyctl version` 子命令应与 `--version` 行为一致。"""
    from proxyctl import cli, explain, _io
    cli.GLOBAL_FLAGS["json"] = True
    explain.set_global_flags({"json": True, "no_color": True, "quiet": False,
                              "dry_run": False, "plain": False})
    _io.set_json_mode(True)
    ctx = {"backend": None, "config": {}, "registry": None,
           "api_base": "", "api_secret": "", "args": []}
    with pytest.raises(SystemExit):
        cli._h_version(ctx)
    env = json.loads(capsys.readouterr().out)
    assert env["cmd"] == "version"
    assert env["data"]["version"] == _pyproject_version()
