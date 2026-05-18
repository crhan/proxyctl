"""Plan ↔ Exec 契约测试：杜绝 _plan_* 与 cmd_* 漂移。

策略概述
========

proxyctl 0.4.0a1 引入了 5 个共享 helper（``_<cmd>_subprocess_argvs``）作为
plan 与 cmd 的**单一事实来源**：
- ``_plan_<cmd>`` 调 helper 拿 argv list → split-join 为 PlanStep.target
- ``cmd_<cmd>`` 调同一个 helper → 拿 argv 传给 ``run(..., sudo=True)``

本套件做两件事：

1. **白盒**：真跑 ``cmd_<cmd>``，mock ``subprocess.run``（``fake_subprocess``
   fixture）+ 系统状态查询函数（launchctl_running / get_mode / 等），收集实
   际 argv list，与 helper 输出对齐。argv 比较前剥 ``sudo`` 前缀 + canonical
   ``/bin/``（``run(..., sudo=True)`` 会 prepend "sudo"）。

2. **静态**：对所有 8 个 ``_plan_*`` 函数：
   - target 字符串不得含 ``<...>`` 占位符
   - subprocess action 的 target.split() 必须与对应 helper 输出严格相等

约定
====

- ``actual_subprocess_argvs ⊆ plan_subprocess_argvs``：plan 是 dry-run 上界
  （包含 conditional cp、reload bootout 等），实际可少不可多
- 测试中显式置 ``cli.GLOBAL_FLAGS["dry_run"] = False``（conftest 已默认 False，
  此为防御性）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from proxyctl import audit, cli


# ── argv 规范化与比较 ─────────────────────────────────────────────────────────

_BIN_PREFIXES = ("/bin/", "/usr/bin/", "/usr/local/bin/", "/sbin/", "/usr/sbin/")


def _strip_argv(argv) -> list:
    """剥 sudo 前缀 + 剥常见 /bin/ 路径前缀，返回 token 列表。"""
    argv = list(argv)
    if argv and argv[0] == "sudo":
        argv = argv[1:]
    if argv:
        head = argv[0]
        for p in _BIN_PREFIXES:
            if head.startswith(p):
                head = head[len(p):]
                break
        argv = [head] + argv[1:]
    return argv


def _argv_matches(expected, actual) -> bool:
    """expected 与 actual（去 sudo / 去 /bin/ 后）token 全等。"""
    return _strip_argv(expected) == _strip_argv(actual)


def _plan_subprocess_targets(plan: list[dict]) -> list[list[str]]:
    """从 PlanStep list 抽出 action==subprocess 的 target，split 成 argv。"""
    return [s["target"].split() for s in plan if s.get("action") == "subprocess"]


def _assert_actual_subset_of_plan(actual_argvs, plan_argvs, cmd_name):
    """每个 actual argv 必须在 plan_argvs 中找到 _argv_matches 命中。"""
    for a in actual_argvs:
        assert any(_argv_matches(p, a) for p in plan_argvs), (
            f"{cmd_name}: actual subprocess {a!r} 不在 plan {plan_argvs!r} 中——"
            "plan ↔ exec 漂移")


# ── 测试公用 fixture ──────────────────────────────────────────────────────────

@pytest.fixture
def macos(monkeypatch):
    """强制 IS_MACOS=True 让 cmd_dns_lock/unlock/daemon/engine 走 macOS 分支。"""
    monkeypatch.setattr(cli, "IS_MACOS", True)


@pytest.fixture
def silence_helpers(monkeypatch):
    """把 cmd_* 调用链上不涉及 subprocess 的 helper mock 为 no-op，
    避免 contract test 因网络/文件系统状态噪音 fail。"""
    monkeypatch.setattr(cli, "_wait_ready", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "dns_activate", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "dns_deactivate", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "dns_lock_start", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "dns_lock_stop", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "proxy_activate", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "proxy_deactivate", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "launchctl_running", lambda *a, **kw: False)
    monkeypatch.setattr(cli, "get_mode", lambda *a, **kw: "tun")
    monkeypatch.setattr(cli, "wait_port", lambda *a, **kw: True)
    # cmd_start 调用 _apply_route_hooks → 走 registry；contract test 不关心。
    monkeypatch.setattr(cli, "_apply_route_hooks", lambda *a, **kw: None)


# ── 1. 白盒：cmd_dns_unlock 真跑 → fake_subprocess.calls vs helper ──────────

def test_contract_helper_dns_unlock(macos, fake_subprocess):
    config = {"dns_lock_label": "com.test.dns-lock"}
    plist = "/Library/LaunchDaemons/com.test.dns-lock.plist"
    full_label = "system/com.test.dns-lock"

    expected = cli._dns_unlock_subprocess_argvs(plist, full_label)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_dns_unlock(config)

    _assert_actual_subset_of_plan(fake_subprocess.calls, expected, "dns-unlock")
    # 同时验证反向：actual 至少跑了 helper 中的每条（dns-unlock 无 conditional）
    assert len(fake_subprocess.calls) == len(expected), \
        f"dns-unlock 应跑 {len(expected)} 步，实际跑了 {len(fake_subprocess.calls)}"


# ── 2. 白盒：cmd_daemon start/stop/restart ────────────────────────────────

def _config_with_daemon():
    return {
        "extra_daemons": {
            "foo": {
                "label": "com.test.foo",
                "plist_src": "/tmp/test-src.plist",
                "log_path": "/tmp/test.log",
            }
        }
    }


def test_contract_helper_daemon_start(macos, silence_helpers, fake_subprocess,
                                       monkeypatch, tmp_path):
    config = _config_with_daemon()
    plist_dst = "/Library/LaunchDaemons/com.test.foo.plist"
    plist_src = "/tmp/test-src.plist"
    full_label = "system/com.test.foo"

    # cmd_daemon start 在 plist_dst 不存在时 cp，存在时跳过。这里让两个都不存在
    # 触发 _io.fail（plist_src 也不存在）—— 改为 mock os.path.isfile：
    # plist_dst 不存在 → cp；plist_src 存在 → 通过检查
    def _isfile(p):
        if p == plist_dst:
            return False
        if p == plist_src:
            return True
        return False
    monkeypatch.setattr(cli.os.path, "isfile", _isfile)

    expected = cli._daemon_subprocess_argvs("start", plist_src, plist_dst, full_label)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_daemon("foo", "start", config)

    _assert_actual_subset_of_plan(fake_subprocess.calls, expected, "daemon start")
    # 含 cp + bootstrap 共 2 步
    assert len(fake_subprocess.calls) == 2


def test_contract_helper_daemon_stop(macos, silence_helpers, fake_subprocess,
                                      monkeypatch):
    config = _config_with_daemon()
    plist_src = "/tmp/test-src.plist"
    plist_dst = "/Library/LaunchDaemons/com.test.foo.plist"
    full_label = "system/com.test.foo"

    # launchctl_running=True 走 stop 实际路径
    monkeypatch.setattr(cli, "launchctl_running", lambda *a, **kw: True)
    expected = cli._daemon_subprocess_argvs("stop", plist_src, plist_dst, full_label)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_daemon("foo", "stop", config)

    _assert_actual_subset_of_plan(fake_subprocess.calls, expected, "daemon stop")
    assert len(fake_subprocess.calls) == 1


def test_contract_helper_daemon_restart(macos, silence_helpers, fake_subprocess):
    config = _config_with_daemon()
    plist_src = "/tmp/test-src.plist"
    plist_dst = "/Library/LaunchDaemons/com.test.foo.plist"
    full_label = "system/com.test.foo"

    expected = cli._daemon_subprocess_argvs("restart", plist_src, plist_dst, full_label)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_daemon("foo", "restart", config)

    _assert_actual_subset_of_plan(fake_subprocess.calls, expected, "daemon restart")
    assert len(fake_subprocess.calls) == 1


# ── 3. 白盒：cmd_dns_lock（reload 与 default 路径）───────────────────────

def test_contract_helper_dns_lock_reload(macos, silence_helpers, fake_subprocess,
                                          monkeypatch, tmp_path):
    """reload=True 且 already_registered=True → 跑 [bootout, tee, bootstrap]。"""
    config = {"dns_lock_label": "com.test.dns-lock"}
    plist = "/Library/LaunchDaemons/com.test.dns-lock.plist"
    full_label = "system/com.test.dns-lock"
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})

    # watchdog 脚本可执行检查 → 直接 True
    monkeypatch.setattr(cli.os, "access", lambda *a, **kw: True)
    # already_registered=True 触发 reload bootout
    monkeypatch.setattr(cli, "launchctl_running", lambda *a, **kw: True)

    expected = cli._dns_lock_subprocess_argvs(plist, full_label)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_dns_lock(config, backend, reload=True)

    _assert_actual_subset_of_plan(fake_subprocess.calls, expected, "dns-lock reload")
    # bootout + tee + bootstrap = 3 步
    assert len(fake_subprocess.calls) == 3


def test_contract_helper_dns_lock_first_install(macos, silence_helpers,
                                                  fake_subprocess, monkeypatch,
                                                  tmp_path):
    """首次安装（already_registered=False）→ 跳过 bootout，跑 [tee, bootstrap]。
    actual ⊆ plan（plan 是上界含 bootout）。"""
    config = {"dns_lock_label": "com.test.dns-lock"}
    plist = "/Library/LaunchDaemons/com.test.dns-lock.plist"
    full_label = "system/com.test.dns-lock"
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})

    monkeypatch.setattr(cli.os, "access", lambda *a, **kw: True)
    monkeypatch.setattr(cli, "launchctl_running", lambda *a, **kw: False)

    expected = cli._dns_lock_subprocess_argvs(plist, full_label)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_dns_lock(config, backend, reload=False)

    _assert_actual_subset_of_plan(fake_subprocess.calls, expected,
                                   "dns-lock first install")
    # 跳过 bootout：实际 2 步
    assert len(fake_subprocess.calls) == 2


# ── 4. 白盒：cmd_engine ──────────────────────────────────────────────────

def test_contract_helper_engine(macos, silence_helpers, fake_subprocess,
                                  monkeypatch, tmp_path):
    """cmd_engine 切换 mihomo → singbox 应跑 [bootout, rm, cp, bootstrap]。"""
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"config_dir": str(tmp_path), "proxy_port": 7890, "backend": "mihomo"}

    new_backend = cli.SingboxBackend(config)
    plist_src = cli.os.path.join(cli.DEFAULT_CONFIG_DIR, "launchdaemons",
                                  cli.os.path.basename(new_backend.plist))

    # 跳过预检：plist_src + new_backend.config_file 都视作存在
    monkeypatch.setattr(cli.os.path, "isfile", lambda p: True)

    expected = cli._engine_subprocess_argvs(backend, plist_src, new_backend.plist,
                                             new_backend.label)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_engine(backend, "singbox", config)

    _assert_actual_subset_of_plan(fake_subprocess.calls, expected, "engine")
    # 4 步：bootout + rm + cp + bootstrap
    assert len(fake_subprocess.calls) == 4


# ── 5. 静态：_plan_* subprocess.target == helper 输出 ────────────────────

def test_contract_plan_subprocess_argvs_align_with_helper(tmp_path):
    """对 4 个有 helper 的命令，断言 _plan_*.subprocess[i].target.split()
    与 helper[i] 严格相等。helper 改、plan 不改 → 这条测试 fail。"""
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"api_base": "http://127.0.0.1:9090",
              "dns_lock_label": "com.test.dns-lock"}

    # 1) engine
    new_cfg = {"backend": "singbox"}
    new_backend = cli.get_backend(new_cfg)
    plist_src = cli.os.path.join(cli.DEFAULT_CONFIG_DIR, "launchdaemons",
                                  cli.os.path.basename(new_backend.plist))
    plan_engine = cli._plan_engine(backend, "singbox")
    helper_engine = cli._engine_subprocess_argvs(backend, plist_src,
                                                   new_backend.plist,
                                                   new_backend.label)
    assert _plan_subprocess_targets(plan_engine) == helper_engine, \
        "engine: _plan_engine 与 _engine_subprocess_argvs 漂移"

    # 2) daemon start/stop/restart
    plist_src = "/tmp/src.plist"
    plist_dst = "/Library/LaunchDaemons/com.test.foo.plist"
    full_label = "system/com.test.foo"
    for sub in ("start", "stop", "restart"):
        plan_daemon = cli._plan_daemon("foo", sub, plist_src, plist_dst, full_label)
        helper_daemon = cli._daemon_subprocess_argvs(sub, plist_src, plist_dst,
                                                      full_label)
        assert _plan_subprocess_targets(plan_daemon) == helper_daemon, \
            f"daemon {sub}: _plan_daemon 与 _daemon_subprocess_argvs 漂移"

    # 3) dns-lock
    plan_dns_lock = cli._plan_dns_lock(config, reload=True)
    helper_dns_lock = cli._dns_lock_subprocess_argvs(
        "/Library/LaunchDaemons/com.test.dns-lock.plist",
        "system/com.test.dns-lock")
    assert _plan_subprocess_targets(plan_dns_lock) == helper_dns_lock, \
        "dns-lock: _plan_dns_lock 与 _dns_lock_subprocess_argvs 漂移"

    # 4) dns-unlock
    plan_dns_unlock = cli._plan_dns_unlock(config)
    helper_dns_unlock = cli._dns_unlock_subprocess_argvs(
        "/Library/LaunchDaemons/com.test.dns-lock.plist",
        "system/com.test.dns-lock")
    assert _plan_subprocess_targets(plan_dns_unlock) == helper_dns_unlock, \
        "dns-unlock: _plan_dns_unlock 与 _dns_unlock_subprocess_argvs 漂移"


# ── 6. 静态：所有 _plan_* target 不含 <...> 占位符 ──────────────────────

def test_contract_plan_targets_no_placeholder(tmp_path):
    """0.4.0a1 T5 强化：8 个 _plan_* 的 target 都不得含 <...> 占位符。
    （test_regression_0_3_2.py 已有同等覆盖；本副本与白盒测试同 file 便于阅读。）"""
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"api_base": "http://127.0.0.1:9090",
              "dns_lock_label": "com.test.dns-lock"}
    placeholder_re = re.compile(r"<[^>]+>")
    plan_calls = [
        ("mode",       cli._plan_mode,        (backend, "tun")),
        ("engine",     cli._plan_engine,      (backend, "singbox")),
        ("fix",        cli._plan_fix,         (backend, config)),
        ("audit",      cli._plan_audit_apply, (7, backend, config)),
        ("config",     cli._plan_config_set,  ("/tmp/c.yaml", "k", "v")),
        ("daemon",     cli._plan_daemon,      ("foo", "start", "/tmp/src.plist",
                                               "/Library/LaunchDaemons/com.test.foo.plist",
                                               "system/com.test.foo")),
        ("dns-lock",   cli._plan_dns_lock,    (config,)),
        ("dns-unlock", cli._plan_dns_unlock,  (config,)),
    ]
    for name, fn, args in plan_calls:
        plan = fn(*args)
        for s in plan:
            t = s.get("target", "")
            assert not placeholder_re.search(t), \
                f"plan {name!r} target 含占位符: {t!r}"


# ── 7. 静态：audit apply target 用 audit.MH_LOG/SB_LOG ───────────────────

def test_contract_audit_plan_paths_real(tmp_path):
    config = {"api_base": "http://127.0.0.1:9090"}
    api_url = "http://127.0.0.1:9090/configs?force=true"

    for cls, expected_log in [(cli.MihomoBackend, audit.MH_LOG),
                              (cli.SingboxBackend, audit.SB_LOG)]:
        backend = cls({"config_dir": str(tmp_path), "proxy_port": 7890})
        plan = cli._plan_audit_apply(7, backend, config)

        by_action = {s["action"]: s["target"] for s in plan}
        assert by_action["scan_log"] == expected_log, \
            f"{cls.__name__}: scan_log target 应等于 audit.{cls.__name__[:2].upper()}_LOG"
        assert backend.config_file in by_action["edit_yaml"], \
            f"{cls.__name__}: edit_yaml target 应含 backend.config_file"
        assert by_action["http_put"] == api_url


# ── 8. 静态：mode plan 不含 subprocess action ─────────────────────────────

def test_contract_mode_plan_no_subprocess(tmp_path):
    """0.4.0a1：cmd_mode 不调 launchctl —— plan 不应再含 subprocess action。"""
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    for target in ("tun", "proxy"):
        plan = cli._plan_mode(backend, target)
        sub_steps = [s for s in plan if s.get("action") == "subprocess"]
        assert sub_steps == [], \
            f"_plan_mode({target!r}) 不应含 subprocess action（cmd_mode 实际不调 launchctl）"


# ── 9. 白盒：cmd_start / cmd_stop / cmd_restart（0.4.2 新增）──────────────
#
# 0.4.2 起，lifecycle 4 命令补齐 --dry-run / plan。下面 6 个白盒测试覆盖
# macOS + Linux 两种平台，断言 actual ⊆ plan 的 subprocess 部分。

def test_contract_helper_start_macos(macos, silence_helpers, fake_subprocess,
                                       monkeypatch, tmp_path):
    """macOS：cmd_start 应跑 [cp <src> <dst>, launchctl bootstrap]
    （首次安装路径，plist 不存在触发 cp）。actual ⊆ plan。
    """
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"proxy_port": 7890, "dns_lock_label": "com.test.dns-lock"}

    # plist 不存在 → 触发 cp 分支；plist 源文件视作存在跳过 _io.fail。
    monkeypatch.setattr(cli.os.path, "isfile",
                        lambda p: p != backend.plist)

    expected = cli._service_start_argvs(backend)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_start(backend, config)

    plan = cli._plan_start(backend, config)
    plan_argvs = _plan_subprocess_targets(plan)
    _assert_actual_subset_of_plan(fake_subprocess.calls, plan_argvs, "start macOS")
    # 至少应跑 cp + bootstrap（顺序由 service_start 决定）
    assert len(fake_subprocess.calls) >= 2
    # helper 的两条 argv 必须都在 plan subprocess.target 中
    for argv in expected:
        assert any(_argv_matches(argv, p) for p in plan_argvs), \
            f"helper argv {argv!r} 不在 _plan_start 的 subprocess targets {plan_argvs!r} 中"


def test_contract_helper_start_linux(silence_helpers, fake_subprocess,
                                       monkeypatch, tmp_path):
    """Linux：cmd_start 应跑 [systemctl --user start <unit>]。"""
    monkeypatch.setattr(cli, "IS_MACOS", False)
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"proxy_port": 7890}

    expected = cli._service_start_argvs(backend)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_start(backend, config)

    plan = cli._plan_start(backend, config)
    plan_argvs = _plan_subprocess_targets(plan)
    _assert_actual_subset_of_plan(fake_subprocess.calls, plan_argvs, "start Linux")
    assert len(fake_subprocess.calls) == 1
    assert _argv_matches(expected[0], fake_subprocess.calls[0])


def test_contract_helper_stop_macos(macos, silence_helpers, fake_subprocess,
                                      tmp_path):
    """macOS：cmd_stop 应至少包含 launchctl bootout <backend.label>。
    （DNS / proxy 联动函数被 silence_helpers mock 成 no-op，实际 subprocess
    只剩 service_stop 的 launchctl bootout。）"""
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"dns_lock_label": "com.test.dns-lock"}

    expected = cli._service_stop_argvs(backend)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_stop(backend, config)

    plan = cli._plan_stop(backend, config)
    plan_argvs = _plan_subprocess_targets(plan)
    _assert_actual_subset_of_plan(fake_subprocess.calls, plan_argvs, "stop macOS")
    assert _argv_matches(expected[0], fake_subprocess.calls[-1])


def test_contract_helper_stop_linux(silence_helpers, fake_subprocess,
                                      monkeypatch, tmp_path):
    """Linux：cmd_stop 应只跑 [systemctl --user stop <unit>]。"""
    monkeypatch.setattr(cli, "IS_MACOS", False)
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {}

    expected = cli._service_stop_argvs(backend)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_stop(backend, config)

    plan = cli._plan_stop(backend, config)
    plan_argvs = _plan_subprocess_targets(plan)
    _assert_actual_subset_of_plan(fake_subprocess.calls, plan_argvs, "stop Linux")
    assert len(fake_subprocess.calls) == 1
    assert _argv_matches(expected[0], fake_subprocess.calls[0])


def test_contract_helper_restart_macos(macos, silence_helpers, fake_subprocess,
                                         tmp_path):
    """macOS：cmd_restart 应至少包含 launchctl kickstart -k <label>。"""
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"proxy_port": 7890, "dns_lock_label": "com.test.dns-lock"}

    expected = cli._service_restart_argvs(backend)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_restart(backend, config)

    plan = cli._plan_restart(backend, config, clean=False)
    plan_argvs = _plan_subprocess_targets(plan)
    _assert_actual_subset_of_plan(fake_subprocess.calls, plan_argvs, "restart macOS")
    assert any(_argv_matches(expected[0], c) for c in fake_subprocess.calls)


def test_contract_helper_restart_linux(silence_helpers, fake_subprocess,
                                         monkeypatch, tmp_path):
    """Linux：cmd_restart 应只跑 [systemctl --user restart <unit>]。"""
    monkeypatch.setattr(cli, "IS_MACOS", False)
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"proxy_port": 7890}

    expected = cli._service_restart_argvs(backend)
    fake_subprocess.set_default(returncode=0)
    cli.cmd_restart(backend, config)

    plan = cli._plan_restart(backend, config, clean=False)
    plan_argvs = _plan_subprocess_targets(plan)
    _assert_actual_subset_of_plan(fake_subprocess.calls, plan_argvs, "restart Linux")
    assert len(fake_subprocess.calls) == 1
    assert _argv_matches(expected[0], fake_subprocess.calls[0])


# ── 10. 白盒：cmd_recover —— 宽松断言（curl argv 包含 plan http_put URL）──

def test_contract_helper_recover_macos(macos, silence_helpers, fake_subprocess,
                                          monkeypatch, tmp_path):
    """recover 的 plan 列出三个 http_put endpoints（configs / fakeip / proxies）。
    cmd_recover 真跑后，fake_subprocess.calls 中应该至少存在 curl argv
    把这三个 URL 作为参数传入。这是宽松断言（http_put 不是 subprocess action，
    没有严格 argv 复读约束），但保证 plan ↔ exec 不漂移。
    """
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"api_base": "http://127.0.0.1:9090",
              "dns_lock_label": "com.test.dns-lock",
              "api_secret": "test-secret"}
    monkeypatch.setattr(cli, "launchctl_running", lambda *a, **kw: True)

    # PUT /configs?force=true 返回 HTTP 200 跑过 reload check
    fake_subprocess.set_prefix_result(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}"],
        stdout="200", returncode=0)
    # GET /proxies 返回空 proxies 列表，绕过 groups_to_test
    fake_subprocess.set_prefix_result(
        ["curl", "-s", "--noproxy", "*", "-H"],
        stdout='{"proxies": {}}', returncode=0)
    fake_subprocess.set_default(stdout="", returncode=0)

    cli.cmd_recover(backend, config)

    plan = cli._plan_recover(backend, config)
    http_targets = [s["target"] for s in plan if s.get("action") == "http_put"]
    assert len(http_targets) == 3

    # 把所有 fake_subprocess.calls 摊平成字符串，断言每个 plan URL 都出现过
    flat = " ".join(" ".join(c) for c in fake_subprocess.calls)
    for url in http_targets:
        assert url in flat, \
            f"recover plan http_put target {url!r} 应在实际 curl argv 中出现；" \
            f"实际 calls={fake_subprocess.calls!r}"


# ── 11. 静态：扩展 plan no-placeholder 覆盖 0.4.2 新加的 5 个 _plan_* ────

def test_contract_plan_no_placeholder_0_4_2(tmp_path):
    """0.4.2 新增的 _plan_start / _plan_stop / _plan_restart / _plan_recover
    target 都不得含 <...> 占位符。"""
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"api_base": "http://127.0.0.1:9090",
              "dns_lock_label": "com.test.dns-lock"}
    placeholder_re = re.compile(r"<[^>]+>")
    plan_calls = [
        ("start",         cli._plan_start,   (backend, config)),
        ("stop",          cli._plan_stop,    (backend, config)),
        ("restart",       cli._plan_restart, (backend, config), {"clean": False}),
        ("restart-clean", cli._plan_restart, (backend, config), {"clean": True}),
        ("recover",       cli._plan_recover, (backend, config)),
    ]
    for entry in plan_calls:
        name, fn, args = entry[0], entry[1], entry[2]
        kwargs = entry[3] if len(entry) > 3 else {}
        plan = fn(*args, **kwargs)
        for s in plan:
            t = s.get("target", "")
            assert not placeholder_re.search(t), \
                f"plan {name!r} target 含 <...> 占位符: {t!r}"


# ── 12. 静态：_plan_start / _plan_stop / _plan_restart 的 subprocess 部分
#       严格等于对应 _service_*_argvs helper 输出（按 IS_MACOS 平台分流）─

@pytest.mark.parametrize("is_macos", [True, False])
def test_contract_lifecycle_plan_aligns_with_helper(monkeypatch, tmp_path, is_macos):
    monkeypatch.setattr(cli, "IS_MACOS", is_macos)
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"proxy_port": 7890, "dns_lock_label": "com.test.dns-lock"}

    # service_*_argvs 是 plan 与 cmd 的单一事实来源。plan 中的第一个
    # subprocess（macOS：start 的 cp，stop 的 dns-lock bootout；
    # restart 的 launchctl kickstart）/ Linux 唯一 subprocess 即 service helper 输出。
    expected_start = cli._service_start_argvs(backend)
    expected_stop = cli._service_stop_argvs(backend)
    expected_restart = cli._service_restart_argvs(backend)

    plan_start_subs = _plan_subprocess_targets(cli._plan_start(backend, config))
    plan_stop_subs = _plan_subprocess_targets(cli._plan_stop(backend, config))
    plan_restart_subs = _plan_subprocess_targets(
        cli._plan_restart(backend, config, clean=False))

    if is_macos:
        # macOS: start plan 第一条 subprocess 应是 cp（_service_start_argvs[0]），
        # 第二条是 bootstrap（[1]）
        assert plan_start_subs[0] == expected_start[0], \
            f"plan start[0] 与 _service_start_argvs[0] 漂移：{plan_start_subs[0]!r} != {expected_start[0]!r}"
        assert plan_start_subs[1] == expected_start[1]
        # stop plan 最后一条 subprocess（service_stop 的 bootout）等于 helper
        assert plan_stop_subs[-1] == expected_stop[0], \
            f"plan stop 最后一条 subprocess 与 _service_stop_argvs 漂移"
        # restart plan 第一条 subprocess（kickstart）= helper
        assert plan_restart_subs[0] == expected_restart[0], \
            f"plan restart[0] 与 _service_restart_argvs 漂移"
    else:
        # Linux：plan 各只有一条 subprocess（systemctl）
        assert plan_start_subs == [expected_start[0]]
        assert plan_stop_subs == [expected_stop[0]]
        assert plan_restart_subs == [expected_restart[0]]


# ── 13. restart-clean 比 restart 多一条 fs_remove cache 步骤 ─────────────

def test_contract_restart_clean_adds_cache_remove(tmp_path):
    backend = cli.MihomoBackend({"config_dir": str(tmp_path), "proxy_port": 7890})
    config = {"proxy_port": 7890, "dns_lock_label": "com.test.dns-lock"}

    plain = cli._plan_restart(backend, config, clean=False)
    clean = cli._plan_restart(backend, config, clean=True)

    plain_actions = [s["action"] for s in plain]
    clean_actions = [s["action"] for s in clean]
    # clean=True 比 clean=False 多 1 步，且第一步是 fs_remove backend.cache_file
    assert len(clean) == len(plain) + 1
    assert clean[0]["action"] == "fs_remove"
    assert clean[0]["target"] == backend.cache_file
    # 其余顺序保持一致（plain 的 fs_remove watchdog + ... = clean 的 [1:]）
    assert clean_actions[1:] == plain_actions
