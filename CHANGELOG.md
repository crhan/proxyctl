# Changelog

本项目使用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.2.0] — 2026-05-17

### Added — Agent 接入一等公民
- **新命令 `proxyctl agent-guide`** — 输出 ≤200 行 markdown，含能力边界 /
  概念地图 / 退出码语义 / JSON envelope 规范 / 故障决策树 / non-interactive
  承诺 / footgun。Agent 第一条该调用的命令。
- **新命令 `proxyctl explain [<topic>]`** — 无参输出"想改 X 去哪？"速查表
  （rules / nodes / config / dns / ports / ...）；带 topic 输出卡片
  `SUMMARY / FILE / EDIT / VERIFY / NEXT`。13 个 topic，内容由当前 backend
  动态计算路径，不硬编码。
- **新命令 `proxyctl commands [--json]`** — 列出所有命令的元数据：
  `group / side_effects / needs_sudo / interactive / supports_json /
  exit_codes / examples`。Agent 决策必备。
- **新命令 `proxyctl config path | get <key>`** — 让 Agent 无需 grep
  就能定位/查询自身配置；支持 dot 路径（`corp_dns.server`）。
  `set` 留作 v0.3。
- **新命令 `proxyctl doctor [--json]`** — 极简 5 项布尔健康打分
  （engine_up / port_listen / dns_ok / system_proxy_ok / connectivity_ok）
  + score + hint。比 `status` 精简、比 `check` 快（<2 秒）。

### Added — clig.dev 合规
- 统一 JSON envelope（schema v1）：
  `{schema_version, cmd, ok, data, error, code, hint, doc}`，失败时也输出完整
  envelope 到 stdout。`status / doctor / explain / agent-guide / commands /
  config / log` 全部支持 `--json`。
- **分语义退出码**：
  - `0` OK · `1` GENERIC（旧路径） · `2` USAGE · `3` NOT_FOUND ·
  - `4` PERMISSION · `5` ENGINE_DOWN · `6` CONFIG_ERR ·
  - `7` NETWORK_ERR · `8` LOCKED（写操作并发锁未拿到）
- **颜色 / TTY 智能化**：非 TTY、`NO_COLOR`、`TERM=dumb`、`--no-color` flag、
  `PROXYCTL_NO_COLOR=1`、`--json` 模式时一律关 ANSI。
- **stdout / stderr 严格分流**：JSON / 数据 → stdout；错误、警告、提示、
  进度 → stderr。错误信息一律带 `hint` + `doc`（指向 explain topic）。
- **写操作并发锁**：`mode / engine / fix / dns-lock / dns-unlock /
  daemon start|stop|restart / audit apply` 用 `fcntl.flock` 在
  `~/.config/proxyctl/.lock.*` 加锁；拿不到锁立即 exit 8 + 结构化错误。
- **拼写建议**：未识别子命令时 `difflib` 给"是否想要 X？"建议，exit 2。
- **PROXYCTL_AGENT=1 一键模式**：等价 `--json` + `--no-color` + 非交互。
- **`proxyctl log` 多模式**：保留默认 `tail -f`；新增 `--tail N` /
  `--no-follow` / `--json`（JSON Lines）。`--json --no-follow` 不再死循环。
- **SIGPIPE 安全**：`proxyctl commands --json | head` 不再抛
  BrokenPipeError；Ctrl-C 退出码 130。
- 全局 flag 位置无关：`--json` / `--no-color` / `--quiet` / `-q` 可在任意位置。

### Changed
- `--help` 顶部加 `AI Agent? → proxyctl agent-guide` 与
  `想改 ... 去哪？ → proxyctl explain` 入口。
- 裸 `proxyctl` 仍 = `status`（兼容），但 stderr 加一行 Agent 入口提示。
- `cmd_recover` 引擎未运行 → `ENGINE_DOWN(5)`（旧 `1`）。
- `trace` 缺参 → `USAGE(2)`（旧 `1`）。
- `daemon <X>` 未声明 → `NOT_FOUND(3)`（旧 `1`），并列出已声明列表。
- `proxyctl log` 文件不存在 → `NOT_FOUND(3)`。

### Notes
- 完整向后兼容：22 个既有命令的调用形式 100% 保留（包括
  `proxyctl claude-proxy` 别名、`proxyctl audit 7`、`proxyctl audit apply`、
  裸 `proxyctl` 等）。
- 旧 `sys.exit(1)` 路径未动；只在新代码和上述列明的 4 条边界路径上启用新码。
- 颜色改造：每个文件保留 `RED/GREEN/...` 模块常量，运行期由 `_io.set_no_color`
  monkey-patch 抹空，零侵入 200+ 处 f-string。

## [0.1.5] — 2026-05-17

### Added
- `no_proxy_extra` 配置项（默认 `[]`），让用户在 `proxyctl env` 输出的
  `NO_PROXY` 末尾追加个人项（公司域名、Tailscale CGNAT 段 `100.64.0.0/10`
  和 MagicDNS `.ts.net` 后缀、本地服务等）。接受 `list[str]` 或逗号分隔
  字符串两种写法。
- `config.yaml.example` 补 `proxy_port` 与 `no_proxy_extra` 注释样例。

### Notes
- 之前 `cmd_env` 写死 `localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,
  192.168.0.0/16`，用户没法在不分叉源码的情况下加自己的 NO_PROXY 项；
  本次保留默认集合，仅新增"追加"语义，向后完全兼容。
- 推荐 shell rc 中 `proxy()` 改成 `eval "$(proxyctl env)"`，端口与
  NO_PROXY 一并跟着 `~/.config/proxyctl/config.yaml` 走，rc 自身不再
  硬编码端口或域名（保持仓库公开 / 个性化留本地的原则）。

## [0.1.4] — 2026-05-17

### Added
- `proxy_port` 配置项（默认 `7890`），让引擎对外的 HTTP/SOCKS mixed-port
  在 status/check/env/wait 等所有命令中可配置；之前的 `7890` / `9090` 硬编码
  在 `cli.py` `status.py` `check.py` 多处散落，导致同机起第二个 mihomo 实例
  （例如 Docker 已占 7890，本地用 proxyctl 起到 7892）时，所有命令仍按
  老端口读 / 测，对新实例完全 "看不见"。

### Changed
- `cmd_status` 的端口列表改为 `(config.proxy_port, "proxy")` +
  `urlparse(api_base).port`，不再写死。
- `cmd_check` 第 1/4 阶段端口检测同上；第 3/4 阶段
  （连通性 / 出口 IP）的 `socks5h://127.0.0.1:7890` 改用 `proxy_port`，
  通过 `_test_url` / `_ipgeo` / `_fetch_probe` 的新 `proxy_port` 参数透传。
- `cmd_env` / `_wait_ready` 的 7890 改用 `config.proxy_port`。
- `_gather_ports(claude_proxy_label, port_list=None)` 加可选参数，
  `None` 时回退到 `[(7890,"proxy"),(9090,"api")]` 保持旧调用兼容。

### Notes
- 默认值仍为 7890 / 9090，所有现有用户行为完全不变；只在
  `~/.config/proxyctl/config.yaml` 加 `proxy_port:` 字段后才切换。
- 代理组名（中文 / 自定义）依然由用户插件 `check_groups()` 声明，
  与 `core/plugin.py` 顶部 "core 不感知任何具体业务" 原则一致——
  本次特意没有在 core 加 "自动取所有顶层组" 的 fallback。

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

[Unreleased]: https://github.com/crhan/proxyctl/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/crhan/proxyctl/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/crhan/proxyctl/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/crhan/proxyctl/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/crhan/proxyctl/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/crhan/proxyctl/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/crhan/proxyctl/releases/tag/v0.1.0
