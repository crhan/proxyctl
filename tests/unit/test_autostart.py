"""测试 proxyctl.autostart — autostart unit 解析与 8 条规则推导（v0.5.0+）。

重点：plist / systemd unit 解析的正确性 + 8 条规则的触发/短路逻辑。
不实测 launchctl/systemctl 子进程（mock 即可）。
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from proxyctl import autostart


# ────────────────────────────────────────────────────────────────────────────
# 测试夹具
# ────────────────────────────────────────────────────────────────────────────

class _FakeBackend:
    """最小 backend stub，只暴露 autostart.inspect_* 用到的字段。"""

    def __init__(self, plist_path: str, unit_name: str = "mihomo.service",
                 label: str = "system/com.mihomo.tun"):
        self.plist = plist_path
        self.unit = unit_name
        self.label = label


def _write_plist(path: Path, *, binary: str = "/opt/homebrew/bin/mihomo",
                  config_dir: str = "/Users/alice/.config/mihomo",
                  label: str = "com.mihomo.tun") -> Path:
    data = {
        "Label": label,
        "ProgramArguments": [binary, "-d", config_dir],
        "RunAtLoad": True,
        "KeepAlive": True,
    }
    path.write_bytes(plistlib.dumps(data))
    return path


def _write_systemd_unit(path: Path, *,
                         binary: str = "%h/.local/bin/mihomo",
                         config_dir: str = "%h/.config/mihomo") -> Path:
    content = (
        "[Unit]\n"
        "Description=Mihomo\n\n"
        "[Service]\n"
        f"ExecStart=/bin/sh -c 'exec {binary} -d {config_dir} >> "
        f"{config_dir}/mihomo.log 2>> {config_dir}/mihomo.err'\n"
        "Restart=on-failure\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


# ────────────────────────────────────────────────────────────────────────────
# inspect_static — plist 解析
# ────────────────────────────────────────────────────────────────────────────

def test_inspect_static_darwin_happy(tmp_path):
    plist = _write_plist(tmp_path / "com.mihomo.tun.plist",
                          binary="/opt/homebrew/bin/mihomo",
                          config_dir="/Users/alice/.config/mihomo")
    backend = _FakeBackend(str(plist))
    out = autostart.inspect_static(backend, platform="darwin")
    assert out["platform"] == "darwin"
    assert out["unit_exists"] is True
    assert out["binary"] == "/opt/homebrew/bin/mihomo"
    assert out["config_dir"] == "/Users/alice/.config/mihomo"
    assert out["placeholder_unrendered"] is False


def test_inspect_static_darwin_unit_missing(tmp_path):
    backend = _FakeBackend(str(tmp_path / "no-such.plist"))
    out = autostart.inspect_static(backend, platform="darwin")
    assert out["unit_exists"] is False
    assert out["binary"] is None


def test_inspect_static_detects_placeholder(tmp_path):
    plist = _write_plist(tmp_path / "com.mihomo.tun.plist",
                          binary="/Users/yourname/.local/bin/mihomo",
                          config_dir="/Users/yourname/.config/mihomo")
    backend = _FakeBackend(str(plist))
    out = autostart.inspect_static(backend, platform="darwin")
    assert out["placeholder_unrendered"] is True


def test_inspect_static_binary_missing_flag(tmp_path):
    plist = _write_plist(tmp_path / "com.mihomo.tun.plist",
                          binary="/nonexistent/bin/mihomo")
    backend = _FakeBackend(str(plist))
    out = autostart.inspect_static(backend, platform="darwin")
    assert out["binary"] == "/nonexistent/bin/mihomo"
    assert out["binary_exists"] is False


def test_inspect_static_corrupt_plist(tmp_path):
    p = tmp_path / "bad.plist"
    p.write_bytes(b"not a plist {{{")
    backend = _FakeBackend(str(p))
    out = autostart.inspect_static(backend, platform="darwin")
    assert out["unit_exists"] is True
    assert out["errors"]  # 有错误但不 crash


# ────────────────────────────────────────────────────────────────────────────
# inspect_static — systemd unit 解析
# ────────────────────────────────────────────────────────────────────────────

def test_inspect_static_linux_happy(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    sysd_dir = fake_home / ".config" / "systemd" / "user"
    sysd_dir.mkdir(parents=True)
    unit_path = _write_systemd_unit(sysd_dir / "mihomo.service")
    monkeypatch.setenv("HOME", str(fake_home))
    backend = _FakeBackend(plist_path="/unused", unit_name="mihomo.service")
    out = autostart.inspect_static(backend, platform="linux")
    assert out["platform"] == "linux"
    assert out["unit_path"] == str(unit_path)
    assert out["unit_exists"] is True
    assert out["binary"] == str(fake_home / ".local" / "bin" / "mihomo")
    assert out["config_dir"] == str(fake_home / ".config" / "mihomo")


def test_inspect_static_linux_unit_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    backend = _FakeBackend(plist_path="/unused", unit_name="mihomo.service")
    out = autostart.inspect_static(backend, platform="linux")
    assert out["unit_exists"] is False


# ────────────────────────────────────────────────────────────────────────────
# to_suggestions — 8 条规则
# ────────────────────────────────────────────────────────────────────────────

def _ok_inspect(**overrides) -> dict:
    """假设 inspect 通过的健康基线。"""
    base = {
        "platform": "darwin",
        "unit_path": "/Library/LaunchDaemons/com.mihomo.tun.plist",
        "unit_exists": True,
        "binary": "/opt/homebrew/bin/mihomo",
        "binary_exists": True,
        "config_dir": "/Users/alice/.config/mihomo",
        "placeholder_unrendered": False,
        "raw_snippet": "",
        "errors": [],
        "enabled": True,
        "last_exit_status": 0,
        "is_failed": False,
        "autostart_version": "1.18.10",
    }
    base.update(overrides)
    return base


def _ids(suggestions) -> list[str]:
    return [s["id"] for s in suggestions]


def test_rule_unit_missing_short_circuits():
    """unit 不存在时只报 unit_missing，其他规则不输出。"""
    out = autostart.to_suggestions(_ok_inspect(unit_exists=False, binary=None))
    assert _ids(out) == ["autostart.unit_missing"]


def test_rule_binary_missing():
    out = autostart.to_suggestions(_ok_inspect(binary_exists=False))
    s = [x for x in out if x["id"] == "autostart.binary_missing"][0]
    assert s["severity"] == "warn"
    assert "/opt/homebrew/bin/mihomo" in s["evidence"]["binary"]


def test_rule_binary_mismatch():
    out = autostart.to_suggestions(
        _ok_inspect(binary="/opt/homebrew/bin/mihomo"),
        path_binary="/Users/alice/.local/bin/mihomo")
    s = [x for x in out if x["id"] == "autostart.binary_mismatch"][0]
    assert s["severity"] == "advisory"
    assert s["evidence"]["autostart_binary"] == "/opt/homebrew/bin/mihomo"
    assert s["evidence"]["path_binary"] == "/Users/alice/.local/bin/mihomo"


def test_rule_version_mismatch():
    out = autostart.to_suggestions(
        _ok_inspect(autostart_version="1.15.0"),
        path_version="1.18.10")
    s = [x for x in out if x["id"] == "autostart.version_mismatch"][0]
    assert s["severity"] == "advisory"
    assert s["evidence"]["autostart_version"] == "1.15.0"
    assert s["evidence"]["path_version"] == "1.18.10"


def test_rule_version_match_no_suggestion():
    out = autostart.to_suggestions(
        _ok_inspect(autostart_version="1.18.10"),
        path_version="1.18.10")
    assert "autostart.version_mismatch" not in _ids(out)


def test_rule_config_dir_mismatch():
    out = autostart.to_suggestions(
        _ok_inspect(config_dir="/Users/alice/.config/mihomo"),
        expected_config_dir="/Users/alice/proxy-configs/mihomo")
    s = [x for x in out if x["id"] == "autostart.config_dir_mismatch"][0]
    assert s["severity"] == "warn"


def test_rule_config_dir_match_normalized():
    """路径 normalize 后相同（含尾斜杠）不应触发。"""
    out = autostart.to_suggestions(
        _ok_inspect(config_dir="/Users/alice/.config/mihomo/"),
        expected_config_dir="/Users/alice/.config/mihomo")
    assert "autostart.config_dir_mismatch" not in _ids(out)


def test_rule_placeholder_unrendered():
    out = autostart.to_suggestions(_ok_inspect(placeholder_unrendered=True))
    s = [x for x in out if x["id"] == "autostart.placeholder_unrendered"][0]
    assert s["severity"] == "warn"


def test_rule_disabled():
    out = autostart.to_suggestions(_ok_inspect(enabled=False))
    s = [x for x in out if x["id"] == "autostart.disabled"][0]
    assert s["severity"] == "info"
    assert s["fix_command"] is not None


def test_rule_flapping_darwin():
    out = autostart.to_suggestions(_ok_inspect(last_exit_status=-9))
    s = [x for x in out if x["id"] == "autostart.flapping"][0]
    assert s["severity"] == "warn"
    assert s["evidence"]["last_exit_status"] == -9


def test_rule_flapping_linux():
    out = autostart.to_suggestions(
        _ok_inspect(platform="linux", is_failed=True, last_exit_status=None))
    s = [x for x in out if x["id"] == "autostart.flapping"][0]
    assert s["evidence"]["systemd_state"] == "failed"


def test_rule_combined_multiple_fire():
    """同时多个问题：binary_mismatch + version_mismatch + disabled + flapping。"""
    out = autostart.to_suggestions(
        _ok_inspect(binary="/opt/homebrew/bin/mihomo",
                    autostart_version="1.15.0",
                    enabled=False, last_exit_status=1),
        path_binary="/Users/alice/.local/bin/mihomo",
        path_version="1.18.10")
    ids = _ids(out)
    assert "autostart.binary_mismatch" in ids
    assert "autostart.version_mismatch" in ids
    assert "autostart.disabled" in ids
    assert "autostart.flapping" in ids


def test_rule_healthy_no_suggestions():
    out = autostart.to_suggestions(
        _ok_inspect(),
        path_binary="/opt/homebrew/bin/mihomo",
        path_version="1.18.10",
        expected_config_dir="/Users/alice/.config/mihomo")
    assert out == []


def test_to_suggestions_none_input():
    assert autostart.to_suggestions(None) == []


def test_to_suggestions_empty_inspect():
    """inspect 为 dict 但什么都没探测到不应 crash。"""
    out = autostart.to_suggestions({"unit_exists": False, "platform": "darwin"})
    assert _ids(out) == ["autostart.unit_missing"]


# ────────────────────────────────────────────────────────────────────────────
# compute_sync — 写命令的 diff 计算（纯函数）
# ────────────────────────────────────────────────────────────────────────────

def test_compute_sync_no_change_returns_no_op(tmp_path):
    plist = _write_plist(tmp_path / "p.plist",
                          binary="/opt/homebrew/bin/mihomo",
                          config_dir="/Users/alice/.config/mihomo")
    backend = _FakeBackend(str(plist))
    static = autostart.inspect_static(backend, platform="darwin")
    diff = autostart.compute_sync(static,
                                   target_binary="/opt/homebrew/bin/mihomo",
                                   target_config_dir="/Users/alice/.config/mihomo")
    assert diff["needs_update"] is False
    assert diff["changes"] == []


def test_compute_sync_binary_change_produces_new_plist(tmp_path):
    plist = _write_plist(tmp_path / "p.plist",
                          binary="/opt/homebrew/bin/mihomo",
                          config_dir="/Users/alice/.config/mihomo")
    backend = _FakeBackend(str(plist))
    static = autostart.inspect_static(backend, platform="darwin")
    diff = autostart.compute_sync(static,
                                   target_binary="/Users/alice/.local/bin/mihomo",
                                   target_config_dir="/Users/alice/.config/mihomo")
    assert diff["needs_update"] is True
    assert any("binary:" in c for c in diff["changes"])
    # 解析新 plist 应见到新 binary 且保留其他字段（KeepAlive 等）
    new_data = plistlib.loads(diff["new_content_bytes"])
    assert new_data["ProgramArguments"][0] == "/Users/alice/.local/bin/mihomo"
    assert new_data["KeepAlive"] is True   # 保留


def test_compute_sync_config_dir_change(tmp_path):
    plist = _write_plist(tmp_path / "p.plist",
                          binary="/opt/homebrew/bin/mihomo",
                          config_dir="/Users/alice/.config/mihomo")
    backend = _FakeBackend(str(plist))
    static = autostart.inspect_static(backend, platform="darwin")
    diff = autostart.compute_sync(static,
                                   target_binary="/opt/homebrew/bin/mihomo",
                                   target_config_dir="/Users/alice/new-cfg/mihomo")
    new_data = plistlib.loads(diff["new_content_bytes"])
    # ProgramArguments 中 -d 后面的目录应被更新
    args = new_data["ProgramArguments"]
    d_idx = args.index("-d")
    assert args[d_idx + 1] == "/Users/alice/new-cfg/mihomo"


def test_compute_sync_unit_missing_errors(tmp_path):
    backend = _FakeBackend(str(tmp_path / "nope.plist"))
    static = autostart.inspect_static(backend, platform="darwin")
    diff = autostart.compute_sync(static,
                                   target_binary="/bin/mihomo",
                                   target_config_dir="/cfg")
    assert diff["needs_update"] is False
    assert any("unit 文件不存在" in e for e in diff["errors"])


def test_compute_sync_linux_unit(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    sysd = fake_home / ".config" / "systemd" / "user"
    sysd.mkdir(parents=True)
    unit = _write_systemd_unit(sysd / "mihomo.service",
                                binary="%h/.local/bin/mihomo",
                                config_dir="%h/.config/mihomo")
    monkeypatch.setenv("HOME", str(fake_home))
    backend = _FakeBackend(plist_path="/unused", unit_name="mihomo.service")
    static = autostart.inspect_static(backend, platform="linux")
    diff = autostart.compute_sync(
        static,
        target_binary=str(fake_home / ".local/bin/mihomo-v2"),
        target_config_dir=str(fake_home / ".config" / "mihomo"))
    assert diff["needs_update"] is True
    assert diff["new_content_text"]
    # 新内容应该含目标 binary 且仍是 systemd unit 格式
    assert "mihomo-v2" in diff["new_content_text"]
    assert "[Unit]" in diff["new_content_text"]
    assert "[Service]" in diff["new_content_text"]


def test_compute_sync_linux_unit_without_execstart_rejects(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    sysd = fake_home / ".config" / "systemd" / "user"
    sysd.mkdir(parents=True)
    bad = sysd / "mihomo.service"
    bad.write_text(  # 故意没 ExecStart
        "[Unit]\nDescription=mihomo\n\n[Service]\nType=simple\n",
        encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    backend = _FakeBackend(plist_path="/unused", unit_name="mihomo.service")
    static = autostart.inspect_static(backend, platform="linux")
    diff = autostart.compute_sync(
        static, target_binary="/bin/mihomo",
        target_config_dir="/cfg")
    # 应拒绝（unit 被改得面目全非），不冒险覆盖
    assert diff["needs_update"] is False
    assert any("ExecStart=" in e for e in diff["errors"])
