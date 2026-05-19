"""proxyctl.autostart — 自动启动 unit 解析与诊断（v0.5.0+）。

哥的需求："plist 里跑的 mihomo 路径和版本是不是也应该检测"——
长期被忽略的 doctor 盲区。一个常见诡异场景：
    PATH 装了 mihomo v1.20，但 plist 还指向 v1.15 的旧 binary
    → 用户跑 `mihomo -v` 看到新版，实际服务跑老版
    → 调试 TUIC/QUIC race 时怎么也对不上号

本模块两层 API：
    inspect_static(backend)   纯文件读取，无 subprocess，<10ms
    inspect_runtime(...)      额外跑 launchctl print / systemctl / binary -v

doctor 调用：先 static 推 6 条规则，再 runtime 补 2 条（disabled / flapping）。

接入点：proxyctl.suggest.build_suggestions(autostart=..., path_engine=...)。
"""

from __future__ import annotations

import os
import platform as _platform
import plistlib
import re
import shutil
import subprocess
from typing import Any

PLACEHOLDER_RE = re.compile(r"yourname|your[_-]name|/Users/yourname", re.IGNORECASE)


def _expand(path: str) -> str:
    """展开 ~ 和 systemd %h 等占位符。"""
    if not path:
        return path
    # systemd %h → $HOME
    if "%h" in path:
        path = path.replace("%h", os.path.expanduser("~"))
    return os.path.expanduser(path)


def _parse_plist(plist_path: str) -> dict[str, Any]:
    """解析 macOS LaunchDaemon plist，返回结构化字段。

    失败时返回 {"errors": [...]}，调用方决定怎么降级。
    """
    out: dict[str, Any] = {"errors": []}
    try:
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException) as e:
        out["errors"].append(f"plist 解析失败: {e}")
        return out
    args = data.get("ProgramArguments") or []
    if isinstance(args, list) and args:
        out["binary"] = args[0]
        # 找 -d 参数后的目录（mihomo 用 `-d <dir>` 指定 config 目录）
        for i, a in enumerate(args):
            if a == "-d" and i + 1 < len(args):
                out["config_dir"] = _expand(args[i + 1])
                break
            # sing-box 用 `-c <file>` 指定 config 文件
            if a == "-c" and i + 1 < len(args):
                out["config_dir"] = os.path.dirname(_expand(args[i + 1]))
                break
    out["label"] = data.get("Label")
    return out


def _parse_systemd_unit(unit_path: str) -> dict[str, Any]:
    """解析 systemd user unit，提取 ExecStart 中的 binary 和 -d 后的目录。

    我们的模板形如：
        ExecStart=/bin/sh -c 'exec %h/.local/bin/mihomo -d %h/.config/mihomo >> ...'
    用正则提取 exec 后的真实命令。
    """
    out: dict[str, Any] = {"errors": []}
    try:
        with open(unit_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        out["errors"].append(f"unit 读取失败: {e}")
        return out

    # 提取 ExecStart= 整行
    m = re.search(r"^ExecStart=(.+)$", text, re.M)
    if not m:
        out["errors"].append("ExecStart 字段缺失")
        return out
    exec_line = m.group(1).strip()

    # 处理 /bin/sh -c 'exec <real-cmd>' 包装
    sh_m = re.search(r"['\"]exec\s+(.+?)['\"]?$", exec_line)
    real_cmd = sh_m.group(1) if sh_m else exec_line
    # 去掉重定向部分
    real_cmd = re.split(r"\s+>>?\s+", real_cmd)[0].strip()

    tokens = real_cmd.split()
    if not tokens:
        out["errors"].append("ExecStart 解析后为空")
        return out
    out["binary"] = _expand(tokens[0])
    for i, t in enumerate(tokens):
        if t == "-d" and i + 1 < len(tokens):
            out["config_dir"] = _expand(tokens[i + 1])
            break
        if t == "-c" and i + 1 < len(tokens):
            out["config_dir"] = os.path.dirname(_expand(tokens[i + 1]))
            break
    return out


def inspect_static(backend, *, platform: str | None = None) -> dict[str, Any]:
    """读取 autostart unit 的纯文件状态——无 subprocess，<10ms。

    Args:
        backend: cli.Backend 实例（用 backend.plist / backend.unit）
        platform: "darwin" | "linux"，默认自动检测；测试可注入

    Returns:
        dict 见模块 docstring。errors 字段为非空时调用方按需降级，
        但所有规则函数都允许在 inspect 失败时跳过（不抛异常）。
    """
    plat = platform or _platform.system().lower()
    if plat.startswith("darwin"):
        plat = "darwin"
    elif plat.startswith("linux"):
        plat = "linux"

    out: dict[str, Any] = {
        "platform": plat,
        "unit_path": None,
        "unit_exists": False,
        "binary": None,
        "binary_exists": False,
        "config_dir": None,
        "placeholder_unrendered": False,
        "raw_snippet": "",
        "errors": [],
    }

    if plat == "darwin":
        unit_path = backend.plist
    elif plat == "linux":
        # 模板部署位置：~/.config/systemd/user/<unit>
        unit_path = os.path.join(
            os.path.expanduser("~"), ".config", "systemd", "user", backend.unit)
    else:
        out["errors"].append(f"unsupported platform: {plat}")
        return out

    out["unit_path"] = unit_path
    if not os.path.isfile(unit_path):
        return out  # unit_exists=False，调用方据此推 unit_missing 规则

    out["unit_exists"] = True

    # 读 raw 内容用于 placeholder 检测和 debug
    try:
        with open(unit_path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        out["raw_snippet"] = raw[:500]
        if PLACEHOLDER_RE.search(raw):
            out["placeholder_unrendered"] = True
    except OSError as e:
        out["errors"].append(f"unit 读取失败: {e}")

    # 解析结构化字段
    if plat == "darwin":
        parsed = _parse_plist(unit_path)
    else:
        parsed = _parse_systemd_unit(unit_path)

    out["errors"].extend(parsed.get("errors", []))
    binary = parsed.get("binary")
    if binary:
        out["binary"] = binary
        out["binary_exists"] = os.path.isfile(binary)
    if parsed.get("config_dir"):
        out["config_dir"] = parsed["config_dir"]
    return out


def inspect_runtime(static_result: dict[str, Any], backend, *,
                    platform: str | None = None,
                    timeout: float = 1.5) -> dict[str, Any]:
    """在 inspect_static 基础上跑命令拿 enabled / flapping / autostart_version。

    所有 subprocess 都 wrap try-except，失败时字段填 None 不报错。
    timeout 1.5s 总预算（autostart 4 个规则不应拖垮 doctor <500ms 承诺）。

    Args:
        static_result: inspect_static 返回值
        backend: cli.Backend
        platform: 同 inspect_static
        timeout: 单个子命令超时秒
    """
    plat = static_result.get("platform") or platform or _platform.system().lower()
    out = dict(static_result)
    out["enabled"] = None
    out["last_exit_status"] = None
    out["is_failed"] = None
    out["autostart_version"] = None

    if not static_result.get("unit_exists"):
        return out

    # autostart binary -v（拿 autostart 实际跑的版本）
    binary = static_result.get("binary")
    if binary and static_result.get("binary_exists"):
        try:
            r = subprocess.run([binary, "-v"], capture_output=True, text=True,
                               timeout=timeout)
            if r.returncode == 0 and r.stdout:
                raw = r.stdout.strip().split("\n", 1)[0]
                out["autostart_version_raw"] = raw
                # 复用 cli.get_engine_version 的解析逻辑（mihomo 输出格式）
                m = re.match(
                    r"^\S+\s+\S+\s+(\S+)\s+(\S+\s+\S+)\s+with\s+go(\S+)\s+(\S+)",
                    raw)
                if m:
                    out["autostart_version"] = m.group(1)
        except (subprocess.TimeoutExpired, OSError):
            pass

    if plat == "darwin":
        # launchctl print system/<label> —— 不带 sudo 也能拿到部分信息
        try:
            r = subprocess.run(["launchctl", "print", backend.label],
                               capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0:
                out["enabled"] = True
                m = re.search(r"last exit code\s*=\s*(-?\d+)", r.stdout)
                if m:
                    out["last_exit_status"] = int(m.group(1))
            else:
                out["enabled"] = False
        except (subprocess.TimeoutExpired, OSError):
            pass
    elif plat == "linux":
        try:
            r = subprocess.run(["systemctl", "--user", "is-enabled", backend.unit],
                               capture_output=True, text=True, timeout=timeout)
            out["enabled"] = (r.returncode == 0
                              and r.stdout.strip() == "enabled")
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            r = subprocess.run(["systemctl", "--user", "is-failed", backend.unit],
                               capture_output=True, text=True, timeout=timeout)
            # is-failed 返回 0 表示"是失败状态"，1 表示"非失败"
            out["is_failed"] = (r.returncode == 0
                                and r.stdout.strip() == "failed")
        except (subprocess.TimeoutExpired, OSError):
            pass

    return out


def compute_sync(inspect_result: dict[str, Any], *,
                  target_binary: str,
                  target_config_dir: str) -> dict[str, Any]:
    """计算 sync 操作的"差异 + 写入内容"。纯函数，无副作用。

    Returns:
        {
          "needs_update": bool,          # 是否需要写
          "changes": list[str],          # 人类可读的变更描述
          "new_content_bytes": bytes,    # macOS plist 的新字节内容（plistlib.dumps）
                                           Linux 用 new_content_text
          "new_content_text": str,       # Linux unit 的新文本（保留其他段）
          "platform": str,
          "unit_path": str,
          "errors": list[str],
        }
    """
    out: dict[str, Any] = {
        "needs_update": False,
        "changes": [],
        "new_content_bytes": None,
        "new_content_text": None,
        "platform": inspect_result.get("platform", ""),
        "unit_path": inspect_result.get("unit_path", ""),
        "errors": [],
    }
    if not inspect_result.get("unit_exists"):
        out["errors"].append("unit 文件不存在，无法 sync（先 install）")
        return out

    current_binary = inspect_result.get("binary")
    current_cfg = inspect_result.get("config_dir")
    plat = inspect_result["platform"]

    if current_binary != target_binary:
        out["changes"].append(f"binary: {current_binary} → {target_binary}")
    if current_cfg and os.path.normpath(current_cfg) != os.path.normpath(target_config_dir):
        out["changes"].append(f"config_dir: {current_cfg} → {target_config_dir}")
    if not out["changes"]:
        return out  # no-op

    out["needs_update"] = True
    unit_path = inspect_result["unit_path"]

    if plat == "darwin":
        try:
            with open(unit_path, "rb") as f:
                data = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException) as e:
            out["errors"].append(f"plist 解析失败: {e}")
            out["needs_update"] = False
            return out
        # in-place 改 ProgramArguments：保留 binary 之后的所有参数，
        # 仅替换 binary 和 -d/-c 后面的目录
        args = list(data.get("ProgramArguments") or [])
        if args:
            args[0] = target_binary
            for i, a in enumerate(args):
                if a == "-d" and i + 1 < len(args):
                    args[i + 1] = target_config_dir
                    break
                if a == "-c" and i + 1 < len(args):
                    # sing-box 用 -c 指 config 文件——保留文件名，仅换目录
                    fname = os.path.basename(args[i + 1])
                    args[i + 1] = os.path.join(target_config_dir, fname)
                    break
            data["ProgramArguments"] = args
        out["new_content_bytes"] = plistlib.dumps(data)
    elif plat == "linux":
        try:
            with open(unit_path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            out["errors"].append(f"unit 读取失败: {e}")
            out["needs_update"] = False
            return out
        # 保守做法：仅替换 ExecStart= 整行，保留其他段
        # 我们的模板形如：
        #   ExecStart=/bin/sh -c 'exec %h/.local/bin/mihomo -d %h/.config/mihomo >> ...'
        # 用户可能改了 sh 包装或日志重定向；为安全起见，**重新生成完整 ExecStart**
        # 用模板：exec <binary> -d <config_dir> >> <config_dir>/<name>.log 2>> <name>.err
        engine_name = os.path.basename(target_binary).replace("-", "_")
        new_exec = (
            f"ExecStart=/bin/sh -c 'exec {target_binary} -d {target_config_dir} "
            f">> {target_config_dir}/{engine_name}.log "
            f"2>> {target_config_dir}/{engine_name}.err'"
        )
        new_text = re.sub(r"^ExecStart=.*$", new_exec, text, flags=re.M)
        if new_text == text:
            # 没匹配到 ExecStart=：可能 unit 被改得面目全非，不动
            out["errors"].append("unit 中未找到 ExecStart= 行，sync 拒绝执行")
            out["needs_update"] = False
            return out
        out["new_content_text"] = new_text
    return out


def to_suggestions(inspect_result: dict[str, Any] | None, *,
                   path_binary: str | None = None,
                   path_version: str | None = None,
                   expected_config_dir: str | None = None,
                   ) -> list[dict[str, Any]]:
    """8 条 autostart 规则推导。

    分层短路：unit 不存在时只报 unit_missing；其他规则跳过。
    所有 evidence 都填实际值，agent 直接消费不必 regex title。

    Args:
        inspect_result: inspect_static / inspect_runtime 返回；None 表示跳过整组
        path_binary: shutil.which(backend_name) 结果
        path_version: PATH binary 的版本（来自 cli.get_engine_version）
        expected_config_dir: backend 期望的 config_dir
            （= dirname(backend.config_file)）
    """
    if not inspect_result:
        return []

    out: list[dict[str, Any]] = []
    unit_path = inspect_result.get("unit_path") or "?"
    plat = inspect_result.get("platform") or "?"

    # 1. unit_missing —— 优先级最高，触发后短路其他规则
    if not inspect_result.get("unit_exists"):
        out.append({
            "id": "autostart.unit_missing",
            "severity": "warn",
            "actor": "user",
            "title": "自动启动 unit 未安装",
            "evidence": {"unit_path": unit_path, "platform": plat},
            "inspect_command": (f"ls -la {unit_path}" if unit_path != "?" else None),
            "fix_command": (
                "sudo cp ~/.config/proxyctl/launchdaemons/*.plist /Library/LaunchDaemons/"
                if plat == "darwin" else
                "systemctl --user enable --now mihomo.service"),
            "doc": "suggestion:autostart.unit_missing",
            "since": "0.5.0",
        })
        return out  # 短路

    # 2. placeholder_unrendered —— 模板未渲染
    if inspect_result.get("placeholder_unrendered"):
        out.append({
            "id": "autostart.placeholder_unrendered",
            "severity": "warn",
            "actor": "user",
            "title": "autostart 模板未替换占位符（yourname）",
            "evidence": {"unit_path": unit_path},
            "inspect_command": f"grep -n yourname {unit_path}",
            "doc": "suggestion:autostart.placeholder_unrendered",
            "since": "0.5.0",
        })

    # 3. binary_missing
    binary = inspect_result.get("binary")
    if binary and not inspect_result.get("binary_exists"):
        out.append({
            "id": "autostart.binary_missing",
            "severity": "warn",
            "actor": "user",
            "title": f"autostart 引用的引擎二进制不存在：{binary}",
            "evidence": {"binary": binary, "unit_path": unit_path},
            "inspect_command": f"ls -la {binary}",
            "doc": "suggestion:autostart.binary_missing",
            "since": "0.5.0",
        })

    # 4. binary_mismatch —— autostart binary 与 PATH binary 不同路径
    if binary and path_binary and binary != path_binary:
        out.append({
            "id": "autostart.binary_mismatch",
            "severity": "advisory",
            "actor": "user",
            "title": "autostart 与 PATH 引用不同的引擎二进制",
            "evidence": {
                "autostart_binary": binary,
                "path_binary": path_binary,
            },
            "inspect_command": f"diff <({binary} -v) <({path_binary} -v)",
            "doc": "suggestion:autostart.binary_mismatch",
            "since": "0.5.0",
        })

    # 5. version_mismatch —— 优先于 binary_mismatch 之外的独立信号
    autostart_ver = inspect_result.get("autostart_version")
    if autostart_ver and path_version and autostart_ver != path_version:
        out.append({
            "id": "autostart.version_mismatch",
            "severity": "advisory",
            "actor": "user",
            "title": (f"autostart 引擎版本 v{autostart_ver} "
                      f"≠ PATH v{path_version}"),
            "evidence": {
                "autostart_version": autostart_ver,
                "path_version": path_version,
                "autostart_binary": binary,
                "path_binary": path_binary,
            },
            "inspect_command": "proxyctl status --json | jq .data.engine",
            "doc": "suggestion:autostart.version_mismatch",
            "since": "0.5.0",
        })

    # 6. config_dir_mismatch —— autostart 跑的配置目录 ≠ proxyctl 看到的
    autostart_cfg = inspect_result.get("config_dir")
    if (autostart_cfg and expected_config_dir
            and os.path.normpath(autostart_cfg)
            != os.path.normpath(expected_config_dir)):
        out.append({
            "id": "autostart.config_dir_mismatch",
            "severity": "warn",
            "actor": "user",
            "title": "autostart 配置目录与 proxyctl 不一致",
            "evidence": {
                "autostart_config_dir": autostart_cfg,
                "expected_config_dir": expected_config_dir,
            },
            "inspect_command": (f"diff -r {autostart_cfg} {expected_config_dir} | head"),
            "doc": "suggestion:autostart.config_dir_mismatch",
            "since": "0.5.0",
        })

    # 7. disabled —— unit 在但未 bootstrap / enable
    enabled = inspect_result.get("enabled")
    if enabled is False:
        out.append({
            "id": "autostart.disabled",
            "severity": "info",
            "actor": "user",
            "title": "自动启动未启用",
            "evidence": {"unit_path": unit_path, "platform": plat},
            "fix_command": ("sudo launchctl bootstrap system " + unit_path
                            if plat == "darwin" else
                            "systemctl --user enable mihomo.service"),
            "doc": "suggestion:autostart.disabled",
            "since": "0.5.0",
        })

    # 8. flapping —— 服务最近异常退出
    flapping = False
    flap_evidence: dict[str, Any] = {}
    if plat == "darwin":
        les = inspect_result.get("last_exit_status")
        if les is not None and les != 0:
            flapping = True
            flap_evidence = {"last_exit_status": les}
    elif plat == "linux":
        if inspect_result.get("is_failed") is True:
            flapping = True
            flap_evidence = {"systemd_state": "failed"}
    if flapping:
        out.append({
            "id": "autostart.flapping",
            "severity": "warn",
            "actor": "user",
            "title": "autostart 服务最近异常退出",
            "evidence": flap_evidence,
            "inspect_command": (f"launchctl print {unit_path}" if plat == "darwin"
                                else "journalctl --user -u mihomo.service -n 50"),
            "doc": "suggestion:autostart.flapping",
            "since": "0.5.0",
        })

    return out
