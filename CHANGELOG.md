# Changelog

本项目使用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.3] — 2026-05-15

### Fixed
- `cmd_start` / `cmd_stop` / `cmd_restart` / `cmd_fix` 接入 `RouteHook` 调度
  （`_apply_route_hooks`）。`RouteHook` 数据类自插件机制引入起就已定义，但
  CLI 一直未调用，导致用户插件（如本机 Tailscale 100.64/10 精细路由覆盖）
  无法在启动 / 重启 / fix 流程中真正生效。

### Notes
- proxyctl 本体不含任何站点或网段特征，具体子网清单（典型场景：
  公司内网在 Tailscale `100.64.0.0/10` 段内的服务器）由用户插件 / 用户
  watchdog hook（`$CONFIG_DIR/dns-watchdog.local`）提供。

## [0.1.2] — 2026-05-14

### Added
- `.python-version` 固定开发期 Python 为 3.13（与 CI 最新矩阵一致）。
- README 加 Changelog 章节，提供 CHANGELOG 入口。
- `CHANGELOG.md`（本文件，按 Keep a Changelog 格式回填历史）。

### Changed
- `pyproject.toml` `[project.urls]` 增加 `Changelog` 和 `Repository`，
  PyPI 项目页将显示对应链接。

## [0.1.1] — 2026-05-14

### Changed
- README 安装章节重写：首推 `uv tool install proxyctl` / `pipx install proxyctl`，
  `git clone` 仅在需要 launchd plist 或开发时使用。
- README 顶部加 PyPI / CI / Python versions / License 四个 badge。

### Added
- Dependabot 配置：每周一 09:00（Asia/Shanghai）扫一次 GitHub Actions 版本，
  官方 `actions/*` 合并到单个 PR 减少噪声。

## [0.1.0] — 2026-05-14

首次发布到 PyPI。

### Added
- **状态面板** `proxyctl status` — 引擎进程 / 端口 / TUN 接口 / DNS / 系统代理 /
  网络环境，并发采集顺序打印。
- **健康检查** `proxyctl check` — 四阶段：基础状态 → 代理组节点延迟 → 连通性测试 →
  出口 IP 验证。
- **链路诊断** `proxyctl trace <domain>` — DNS 解析（fakeip/realip 区分）→
  规则匹配预测 → 连通性测试 → 实际连接验证。
- **配置审计** `proxyctl audit [days] / audit apply` — 扫描日志，找出"走代理但实际
  是国内 IP"的域名，可自动写回 mihomo / sing-box 双 config 直连规则。
- **节点测速** `proxyctl bench [groups...]` — 通过 Clash API 并发触发延迟测试，
  进度条实时输出。
- **服务管理** `start / stop / restart / restart-clean / fix / recover` —
  macOS launchctl + Linux systemctl user 两套底层。
- **模式切换** `proxyctl mode tun|proxy` — TUN（全局接管）与代理模式互转。
- **DNS 守护** `proxyctl dns-lock [--reload] / dns-unlock` — 内嵌 plist 模板，
  对抗 DHCP / VPN / scutil 三层 DNS 覆盖。
- **守护进程通用接口** `proxyctl daemon <name> <subcmd>` 与
  `proxyctl engine mihomo|singbox` 切换。
- **后端抽象层** `engine/{base,mihomo,singbox}.py` — 同一份 CLI 同时支持
  Mihomo（首发）和 Sing-box（预留）。
- **插件机制** `core/plugin.py` — 内置插件目录 + `~/.config/proxyctl/plugins/*.py`
  用户插件目录，hook 类型涵盖检查项、出口探测、status 段、DNS / route 钩子、
  watchdog 层、audit 跳过项等。
- **内置插件**：`connectivity-basic`（google/github/baidu）+ `corp-network`
  （企业 DNS / 内网目标，仅在 `corp_dns` 配置时启用）。
- **测试体系**：pytest + coverage，254 用例，49.4% 总覆盖率，
  关键模块（builtin_plugins / engine / core/plugin）≥ 88%。
- **GitHub Actions CI**：matrix `ubuntu/macos` × Python `3.10/3.11/3.12/3.13`
  共 8 个测试矩阵格，加 `build` job 用 `twine check --strict` 验证 metadata。
- **GitHub Actions Release**：tag `v*.*.*` 触发，OIDC trusted publishing
  发到 PyPI（无 API token），自动创建 GitHub Release 并附 sdist + wheel。

### Fixed
- `cli.main()` 处理 `--help/-h/help` 后未 return，导致继续落入默认 else 分支
  二次调用 `cmd_help()`，help 输出重复两次。

[Unreleased]: https://github.com/crhan/proxyctl/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/crhan/proxyctl/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/crhan/proxyctl/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/crhan/proxyctl/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/crhan/proxyctl/releases/tag/v0.1.0
