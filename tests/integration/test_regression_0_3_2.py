"""端到端集成测试，防 0.3.0 → 0.3.2 期间发现的 5 处 bug 类型再次出现。

0.3.2 自带 2 个 fix 测试（test_audit_plain_main_path_emits_tsv /
test_check_plain_connectivity_uses_real_keys，位于 test_plain_output.py）。
本文件覆盖剩下 3 处 + 1 个通用的"plan target 不含双前缀"契约测试。

所有测试都走 cli.main / cmd_* 真实路径（不 mock 整个函数），
保证字段名 / arity / 平台分支这类 0.3.2 类 bug 能在 CI 一次性被抓出来。
"""

from __future__ import annotations

import json
import re

import pytest

from proxyctl import cli, _io, explain


# ── Bug #3: cmd_dns_unlock 在 macOS 下 NameError 回归 ─────────────────────
def test_cmd_dns_unlock_macos_no_nameerror(monkeypatch, capsys):
    """v0.3.0 → 0.3.2 修复：line 1228 的 cmd_dns_unlock 在 IS_MACOS=True 路径下
    使用未定义的 dns_lock_plist 触发 NameError。本测试模拟 macOS 平台 +
    mock run()，验证函数能跑完整 happy path（含 plist 删除 + 末尾打印）。
    """
    monkeypatch.setattr(cli, "IS_MACOS", True)
    calls: list[list] = []

    class _R:
        def __init__(self, rc=0):
            self.returncode = rc
            self.stdout = ""
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _R(0)
    monkeypatch.setattr(cli, "run", fake_run)

    cli.cmd_dns_unlock({"dns_lock_label": "com.test.dns-lock"})

    out = capsys.readouterr().out
    # 不抛 NameError 才能走到这两行打印
    assert "已停止" in out or "未在运行" in out
    # 必须真正调用 rm <plist> （否则旧 bug 静默无副作用）
    rm_calls = [c for c in calls if c[0] == "rm"]
    assert rm_calls, "cmd_dns_unlock 应调用 rm 删除 plist"
    plist_path = rm_calls[0][-1]
    # 路径必须含 dns_lock_label，且必须在 macOS 系统目录
    assert "com.test.dns-lock" in plist_path
    assert "LaunchDaemons" in plist_path
    # 末尾打印行也必须出现（含 plist 路径）
    assert plist_path in out


# ── Bug #4: _plan_mode / _plan_engine 不输出 system/system/... 双前缀 ─────
def _all_plan_targets(plan: list[dict]) -> list[str]:
    return [s.get("target", "") for s in plan]


def test_plan_mode_no_system_double_prefix():
    """v0.3.0 → 0.3.2 修复：_plan_mode 拼 f'system/{backend.label}' 而
    backend.label 已是 'system/com.mihomo.tun' → plan.target 出现
    'system/system/...'。本测试断言任何 plan target 都没双前缀。"""
    backend = cli.MihomoBackend({"config_dir": "/tmp/test", "proxy_port": 7890})
    plan = cli._plan_mode(backend, "tun")
    for target in _all_plan_targets(plan):
        assert "system/system/" not in target, \
            f"plan target 含双前缀: {target!r}"


def test_plan_engine_no_system_double_prefix():
    backend = cli.MihomoBackend({"config_dir": "/tmp/test", "proxy_port": 7890})
    plan = cli._plan_engine(backend, "singbox")
    for target in _all_plan_targets(plan):
        assert "system/system/" not in target, \
            f"plan target 含双前缀: {target!r}"


def test_all_plan_funcs_no_system_double_prefix():
    """通用契约：所有 _plan_* 函数的 PlanStep.target 都不应含 system/system/。
    新增写命令的 _plan_* 漂移到双前缀也会被这个测试抓住。"""
    backend = cli.MihomoBackend({"config_dir": "/tmp/test", "proxy_port": 7890})
    config = {"api_base": "http://127.0.0.1:9090",
              "dns_lock_label": "com.test.dns-lock"}
    plan_calls = [
        ("mode",   cli._plan_mode,        (backend, "tun")),
        ("engine", cli._plan_engine,      (backend, "singbox")),
        ("fix",    cli._plan_fix,         (backend, config)),
        ("audit",  cli._plan_audit_apply, (7, backend, config)),
        ("config", cli._plan_config_set,  ("/tmp/c.yaml", "k", "v")),
        ("daemon", cli._plan_daemon,      ("foo", "start", "/tmp/src.plist",
                                          "/Library/LaunchDaemons/com.test.foo.plist",
                                          "system/com.test.foo")),
        ("dns-lock",   cli._plan_dns_lock,   (config,)),
        ("dns-unlock", cli._plan_dns_unlock, (config,)),
    ]
    for name, fn, args in plan_calls:
        plan = fn(*args)
        for target in _all_plan_targets(plan):
            assert "system/system/" not in target, \
                f"plan {name!r} target 含双前缀: {target!r}"


# ── Bug #5: cmd_trace --json envelope.ok 不应被 remote_ip 字符串影响 ──────
def test_cmd_trace_json_envelope_ok_is_true_when_no_remote_ip(monkeypatch,
                                                                fake_subprocess,
                                                                capsys):
    """v0.3.0 → 0.3.2 修复：cmd_trace 把 _section_connectivity 返回的
    (lines, remote_ip) 解包当 (lines, conn_ok)，导致 envelope 顶层 ok
    跟随 remote_ip 字符串布尔波动。本测试验证：即使 remote_ip 为空，
    envelope.ok 仍为 True；data.stages.connectivity.ok 仍是 False（informational）。"""
    from proxyctl import trace

    # mock 阶段函数（不真发 curl）
    monkeypatch.setattr(trace, "_section_dns",
                        lambda *a, **kw: (["DNS x"], ["1.2.3.4"]))
    monkeypatch.setattr(trace, "_section_rules",
                        lambda *a, **kw: ("Match", "DIRECT"))
    monkeypatch.setattr(trace, "_section_connectivity",
                        lambda *a, **kw: (["conn x"], ""))   # 空 remote_ip
    monkeypatch.setattr(trace, "_section_connections",
                        lambda *a, **kw: None)

    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit):
        trace.cmd_trace("example.com", "http://x", "s", {"proxy_port": 7890})
    env = json.loads(capsys.readouterr().out)

    # 核心断言：envelope.ok 与 remote_ip 解耦
    assert env["schema_version"] == 2
    assert env["ok"] is True, \
        "trace envelope.ok 应固定 True（命令本身跑完即成功）"
    assert env["code"] == 0

    conn = env["data"]["stages"]["connectivity"]
    assert "remote_ip" in conn
    assert conn["remote_ip"] == ""
    # connectivity.ok 是 informational：remote_ip 空 → False（不影响 envelope.ok）
    assert conn["ok"] is False


def test_cmd_trace_json_envelope_ok_is_true_when_remote_ip_present(monkeypatch,
                                                                     fake_subprocess,
                                                                     capsys):
    """补一个正向：remote_ip 有值时 envelope.ok 仍 True，connectivity.ok 也 True。"""
    from proxyctl import trace
    monkeypatch.setattr(trace, "_section_dns",
                        lambda *a, **kw: (["DNS x"], ["1.2.3.4"]))
    monkeypatch.setattr(trace, "_section_rules",
                        lambda *a, **kw: ("Match", "proxy"))
    monkeypatch.setattr(trace, "_section_connectivity",
                        lambda *a, **kw: (["conn x"], "93.184.216.34"))
    monkeypatch.setattr(trace, "_section_connections",
                        lambda *a, **kw: None)

    explain.set_global_flags({"json": True})
    with pytest.raises(SystemExit):
        trace.cmd_trace("example.com", "http://x", "s", {"proxy_port": 7890})
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is True
    conn = env["data"]["stages"]["connectivity"]
    assert conn["remote_ip"] == "93.184.216.34"
    assert conn["ok"] is True


# ── 通用契约：写命令 --dry-run + --json 输出 plan target 不能含 < > 占位符
def test_dry_run_plan_target_no_placeholder_for_resolved_backend(monkeypatch):
    """plan target 应是真实路径 / 命令，不应含 '<...>' 占位符。

    0.4.0a1 T5 把所有写命令的 _plan_* 占位符消除，contract test 接管。
    本测试用 substring 检查（任何 target 内含 `<...>` 都 fail），覆盖所有
    8 个 _plan_* 函数。
    """
    backend = cli.MihomoBackend({"config_dir": "/tmp/test", "proxy_port": 7890})
    config = {"api_base": "http://127.0.0.1:9090",
              "dns_lock_label": "com.test.dns-lock"}
    placeholder_re = re.compile(r"<[^>]+>")
    for name, fn, args in [
        ("mode",   cli._plan_mode,        (backend, "tun")),
        ("engine", cli._plan_engine,      (backend, "singbox")),
        ("fix",    cli._plan_fix,         (backend, config)),
        ("audit",  cli._plan_audit_apply, (7, backend, config)),
        ("config", cli._plan_config_set,  ("/tmp/c.yaml", "k", "v")),
        ("daemon", cli._plan_daemon,      ("foo", "start", "/tmp/src.plist",
                                          "/Library/LaunchDaemons/com.test.foo.plist",
                                          "system/com.test.foo")),
        ("dns-lock",   cli._plan_dns_lock,   (config,)),
        ("dns-unlock", cli._plan_dns_unlock, (config,)),
    ]:
        plan = fn(*args)
        for s in plan:
            t = s.get("target", "")
            assert not placeholder_re.search(t), \
                f"plan {name!r} target 含 <...> 占位符: {t!r}"


# ── pyproject / cli VERSION 单一事实来源回归（0.3.1 引入）────────────────
def test_cli_version_matches_pyproject():
    """v0.3.1 修复：cli.VERSION 不再硬编码，从 importlib.metadata 取。
    回归：cli.VERSION 必须与 pyproject 一致。"""
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    py_ver = tomllib.load(open(pyproject, "rb"))["project"]["version"]
    assert cli.VERSION == py_ver, \
        f"cli.VERSION={cli.VERSION!r} != pyproject={py_ver!r}"
