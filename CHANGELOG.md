# Changelog

本项目使用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.4.0] — 2026-05-18

> T5（plan ↔ exec 一致性）正式版。0.4.0a1 的功能完整 + 补 `--dry-run` 行为
> 在 README / man page 的用户/agent 文档。零 schema 变更，公开 CLI 行为完全
> 兼容 0.3.x；私有 `_plan_*` helper 签名变更（仅影响仓库外直接 import 的代码）。

### Added — Documentation (vs. 0.4.0a1)

- **README.md**：扩展 `--dry-run` 段。给出 `proxyctl dns-unlock --dry-run --json |
  jq` 的可复读 argv 示例，列 9 种 PlanStep.action 枚举，指向 contract test 文件。
- **man/proxyctl.1**：扩展 `--dry-run` 段。明确 "自 0.4.0 plan.target 全部真实化"，
  列 action 枚举，提及 CI contract test。

### 0.4.0a1 → 0.4.0 之间无功能变化

a1 的所有改动（plan.target 真实化 / 5 个共享 helper / 11 个 contract test /
agent-guide Plan action types 段 / 5 人 review P1 follow-up）全部保留。
本版仅补文档使正式 release 信息对称。

## [0.4.0a1] — 2026-05-17

> Plan ↔ Exec 一致性（T5）：8 个写命令的 `_plan_*` 与 `cmd_*` 共享单一 argv 事实
> 来源，dry-run plan.target 全部真实化（无 `<...>` 占位符），agent 可原样复读
> 当 shell 命令。CI 层引入 contract test 套件永防漂移。**首个 0.4.0 pre-release，
> PEP 440 alpha；schema/envelope 不变，0.3.x 消费者无感升级**。

### Added — Agent-facing

- **plan.target 真实化**：8 个写命令的 dry-run `data.plan[].target`
  从占位符（`<plist_dst>`、`<svc>`、`system/<dns-lock.label>` 等）替换为真实
  绝对路径 / 完整 argv 字符串。agent 可直接 `target.split()` 当 argv 跑。
- **新 plan action 类型 `system_op`**：用于迭代型系统操作（如 networksetup
  遍历所有网络服务），target 为描述性字符串。`fix` / `mode` 用之。
- **`agent-guide` 加 Plan action types 段**：枚举 `subprocess` / `system_op` /
  `fs_write` / `fs_copy` / `fs_remove` / `edit_yaml` / `scan_log` / `http_put`
  7 种 action 及 agent 用法。

### Changed — 结构性

- **`_plan_*` 与 `cmd_*` 共享 helper**：新增 5 个 `_<cmd>_subprocess_argvs` /
  `_resolve_daemon_paths` 共享函数（`cli.py`），plan 派生与实际执行从同一份
  argv 生成，0.3.2 那种"plan 是手写、cmd 是另一份代码"的漂移结构性消除。
- **`_plan_mode` 不再含 launchctl kickstart 步骤**：与 cmd_mode 实际行为对齐
  （cmd_mode 只改 config，由用户手动 restart 引擎生效）。**agent 原本复读
  kickstart 会误操作 → 此版本修复**。
- **⚠️ Breaking (private helpers only)**: `_plan_daemon` / `_plan_audit_apply` /
  `_plan_dns_lock` / `_plan_dns_unlock` 函数签名扩展。新接收
  plist_src/plist_dst/full_label / backend / config 等参数让 target 可派生为
  真实路径。下划线前缀的私有 API 无稳定性承诺；公开 CLI（命令名 / envelope /
  exit codes / schema）行为完全不变，0.3.x 终端用户可无感升级。仅影响仓库外
  直接 `from proxyctl.cli import _plan_*` 的代码——T5 文档已声明这次绑定。

### Added — CI 防漂移

- **`tests/integration/test_plan_exec_contract.py`** 11 个 contract test：
  - 5 个白盒：真跑 `cmd_dns_unlock` / `cmd_daemon start|stop|restart` /
    `cmd_dns_lock`（reload + first install）/ `cmd_engine`，mock subprocess
    后断言 `actual_argvs ⊆ helper(_<cmd>_subprocess_argvs)`。
  - 4 个静态：`_plan_*` subprocess.target 与 helper 输出严格相等 / 8 个 plan
    target 无 `<...>` 占位符 / `_plan_audit_apply` 用 `audit.MH_LOG/SB_LOG` /
    `_plan_mode` 不含 subprocess action。
  - **故意漂移注入**：把 `_plan_engine` 一个 step.target 改为 wrong 字符串 →
    `test_contract_plan_subprocess_argvs_align_with_helper` 立即 fail，错误
    信息含 actual / expected 完整 argv 对比，定位精准。

### Test stats

- 总测试数 466 → 477（+11：contract test 套件）。

## [0.3.3] — 2026-05-17

> 4 项 agent 体感改进 + 1 组端到端回归测试。零 breaking、零 schema 变化，
> 0.3.x 消费者可无感升级。

### Added — Agent-facing
- **`doctor --json` 增 `healthy: bool` 字段**（agent 不必再算 `score == max`）。
  `supported_features.doctor_healthy_field = true`。
- **`agent-guide --section <name>` / `--list-sections`** — agent 按需取小块
  markdown，避免每次拉 ~200 行全文。H2 标题改为 `English — 中文` 双语
  （ASCII slug 稳定供 agent 引用，中文供人类阅读）。模糊匹配 + did-you-mean。
  `supported_features.agent_guide_sections = true`。
- **shell 补全脚本补齐 0.3.x 全部新 flag** — bash/zsh/fish 补全现在覆盖：
  `--dry-run`（写命令位置）、`--plain`（audit/check 位置）、
  `commands --schema`、`agent-guide --section/--list-sections`、
  `help <cmd>` 顶层子命令、`log --tail/--no-follow`、`env --unset`。

### Changed — 结构性
- **`explain.set_global_flags` 同步设 `_io._JSON_MODE`** — 解决子模块直接
  调用（绕过 `cli.main`）时 `_io.fail` 拿不到正确 JSON 模式的问题。
  本来只影响测试代码，但同样适用于第三方调用方。

### Added — 端到端回归测试（防 0.3.2 类 bug 再发）
- **`tests/integration/test_regression_0_3_2.py`** 新增 8 个集成测试：
  - `test_cmd_dns_unlock_macos_no_nameerror` — Bug #3 回归（IS_MACOS 分支
    NameError），mock `IS_MACOS=True` + `run()` 走完整 happy path。
  - `test_plan_mode_no_system_double_prefix` / `_plan_engine_*` —
    Bug #4 回归（plan target 双前缀）。
  - `test_all_plan_funcs_no_system_double_prefix` — **通用契约**：所有
    `_plan_*` 函数 target 都不含 `system/system/`，新增写命令也会被这个
    测试抓住漂移。
  - `test_cmd_trace_json_envelope_ok_is_true_when_no_remote_ip` /
    `_when_remote_ip_present` — Bug #5 回归（envelope.ok 与
    `remote_ip` 字符串解耦；connectivity.ok 仍是 informational）。
  - `test_dry_run_plan_target_no_placeholder_for_resolved_backend` —
    mode/engine/fix 的 plan target 应是真实路径，不是占位符（为 0.4.0 T5
    plan/exec 真绑定打底）。
  - `test_cli_version_matches_pyproject` — 0.3.1 引入的"VERSION 单一事实
    来源"契约（再加一道防线）。

### Test stats
- 总测试数 430 → 466（+36：T1 +1 / T2 +13 / T3 +14 / T4 +8）。

## [0.3.2] — 2026-05-17

> v0.3.0 引入 `--plain` / `--dry-run` 时的 4 处遗留 bug。无新增能力、无 schema 变更，
> 0.3.1 的消费者可无感升级。

### Fixed
- **`audit --plain` 主路径 TypeError** —
  `audit.py:442` 的 `_audit_emit(as_json, _sys, _real_stdout, collector)`
  漏传 `as_plain` 参数（函数签名是 5 个位置参数，line 347 / 359 的早期返回
  路径都对，只有这条主路径漏改）。导致任何走到"扫描到候选域名"路径的
  `proxyctl audit [N] --plain` 直接抛 `TypeError: _audit_emit() missing 1
  required positional argument: 'collector'` 退出 1。修复：line 442 补全
  `as_plain`。
- **`check --plain` connectivity 字段全错** —
  `check.py:947-948` 用 `c.get('target')` 与 `c.get('http_code')` 拼 detail，
  但 collector 里 connectivity 字段实际是 `name / url / mode / ok / message`
  （v0.2.2 起就这样），结果 TSV 一律输出
  `None=X;None=X;None=X`，agent 解析等于拿不到任何信息。改为
  `name=ok` / `name=X` 真实字段渲染。
- **`cmd_dns_unlock` 在 macOS 下 NameError** — `cli.py:1228` 的
  `cmd_dns_unlock` 只定义 `dns_lock_label`，line 1239/1240 直接用
  `dns_lock_plist`（未定义）→ macOS 用户执行 `proxyctl dns-unlock` 时
  bootout 之后必触发 `NameError`，plist 文件永远删不掉、提示行永远不打印。
  Linux 走 `IS_MACOS` 早返回不触发。补一行
  `dns_lock_plist = f"/Library/LaunchDaemons/{dns_lock_label}.plist"`，
  与 `cmd_dns_lock` (line 1184) 复用同一推导。
- **`_plan_mode` / `_plan_engine` 输出 `system/system/...` 双前缀** —
  `Backend.label` 已是 `system/com.mihomo.tun`（line 122 / 152），
  `_plan_mode`(1727) 和 `_plan_engine`(1744) 又拼 `f"system/{backend.label}"`
  → dry-run plan 的 `target` 字段输出
  `launchctl kickstart -k system/system/com.mihomo.tun`。仅影响
  `--dry-run` 展示，不影响真实执行（执行路径走 launchctl API），但 agent
  解析 plan.target 复读会得到错误命令。改为直接用 `backend.label`。
- **`trace --json` envelope.ok 语义错位** —
  `cmd_trace` 把 `_section_connectivity` 返回值 `(lines, remote_ip)` 误解包
  为 `(lines, conn_ok)`，把字符串 `remote_ip` 当布尔灌进 envelope 顶层
  `ok` 与 `collector.connectivity.ok`，导致 HTTP 5xx / 重定向解析不到 IP
  时 ok 跟随波动，agent 用 envelope.ok 判定 trace 命令是否跑成功会被误导。
  改为：envelope 顶层 `ok` 固定为 True（trace 是诊断工具，命令本身跑完
  即成功），新增 `data.stages.connectivity.remote_ip` 字段暴露原始 IP。

### Added — 测试
- `test_audit_plain_main_path_emits_tsv` — `audit --plain` 主路径 arity 回归。
- `test_check_plain_connectivity_uses_real_keys` — `check --plain` connectivity
  字段名回归（断言不含 `None`）。
- 总测试数 428 → 430。

## [0.3.1] — 2026-05-17

### Fixed
- **用户插件 ANSI 字面量泄漏到管道** — 当用户插件
  （如 `~/.config/proxyctl/plugins/sb_private.py`）自己定义 `RED/GREEN/...`
  常量时，`set_no_color(True)` 后才被加载的插件代码继续吐 `\033[...]m`
  字面量到 status / check 等命令的非 TTY 输出。修复：`core/plugin.py` 的
  `load_builtin` / `load_user` 在 import 完每个插件模块后立刻调一次
  `maybe_disable_module_colors`，使关色态对插件模块生效。
- **`src/proxyctl/__init__.py` 的 `__version__` 与 pyproject 脱节**
  （0.2.2 vs 0.3.0）— 此后由发版流程统一同步。
- **`cli.VERSION` 硬编码 `"0.3.0"`** 导致 `proxyctl --version` 跟 pyproject 脱节。
  改为通过 `importlib.metadata.version("proxyctl")` 单一事实来源读取。

### Added — 测试
- `test_load_user_strips_plugin_ansi_when_no_color` /
  `test_load_user_keeps_plugin_ansi_when_color_on` — 插件加载色彩策略回归。
- 总测试数 426 → 428。

## [0.3.0] — 2026-05-17

> Agent-friendliness + clig.dev 全面合规。这是一个有意 breaking 的版本——
> 完整迁移见 [MIGRATION-0.3.md](MIGRATION-0.3.md)，agent 协议见 AGENTS.md
> 与 `proxyctl agent-guide`。

### Breaking
- **JSON envelope schema_version：1 → 2**。新结构：
  - 移除字段 `hint: string|null`，引入 `hints: string[]`（可多条）。
  - 引入 `warnings: string[]`（非致命警告，与 hints 区分）。
  - 引入 `meta` 对象：`ts` / `elapsed_ms` / `proxyctl_version` / `request_id`。
  - **无兼容窗口**：消费者必须一次性升级到 v2。
- **`commands --json` 中 `side_effects` 由 string 改为 enum list**：
  `["process", "system", "config-write", "cache", "network-io"]`。
  条件性副作用拆到新字段 `conditional_side_effects: { trigger: list[enum] }`。
- **`audit <not-a-number>` 不再静默 fallback 到 1**，改为 USAGE(2) + did-you-mean。
- **explain topic 的 `next` 字段重命名为 `next_commands`**（schema 字段名稳定）。
- **无参 `proxyctl`（JSON / PROXYCTL_AGENT 模式）输出 discovery envelope**，
  不再吐完整 status 内容。人类模式不变（stderr banner + stdout status）。

### Agent-facing — 新能力
- **`proxyctl --version --json`** — 输出 envelope，含 `supported_features`
  能力探测表（envelope_v2 / dry_run / plain / commands_schema / ... 等 18 项）。
- **`--dry-run`**（全局 flag）— 适用 7 个写命令（mode / engine / fix /
  audit apply / config set / daemon / dns-lock / dns-unlock），输出
  `data.plan = [PlanStep, ...]`。PlanStep 字段：
  `step / action / target / reversible / requires_sudo / side_effects / summary`。
- **`--plain`**（全局 flag）— `audit` / `check` 命令输出 TSV
  （无 ANSI / 无 box / 无 emoji）。与 `--json` 互斥。
- **`proxyctl commands --schema`** — 输出 `commands --json` 的 JSON Schema
  (Draft 2020-12)，agent 可用以 validate 解析的结构。
- **`proxyctl help <command>`** — 顶层 `help` 子命令支持 `help <cmd>`
  等价 `<cmd> --help`，统一从 COMMANDS_META 派生。
- **`log --json` NDJSON 规范化** — 首行 meta header
  `{schema_version, cmd, stream, path}`，后续每行事件 `{source, line}`。
- **`doctor --json` 扩展 informational 字段** — 新增
  `engine / mode / port / config_path / engine_config_path /
   lock_held / lock_path`（不计分，但 agent 一次调用就拿到上下文）。
- **新 explain topics**：`subscription`（订阅边界）、`agent-protocol`
  （envelope/退出码/决策树 cheat sheet）、`locks`（锁文件位置 + 手动释放）、
  `flags`（全局 flag 速查）。
- **LOCKED(8) 错误透出锁路径** — `hints` 列出具体锁文件、`lsof` 排查命令、
  手动 `rm` 步骤；`doc: locks` 指向新 explain topic。
- **子选项级 did-you-mean** — `mode tunn` / `engine mihomoo` /
  `daemon name stat` / `completion zsh-foo` 失败时给最接近的有效值建议。

### Added — 新退出码
- `9  TIMEOUT` — 命令超时（bench / curl 等长跑路径）。
- `10 DEPENDENCY_MISSING` — 依赖二进制 / 脚本缺失（mihomo / dns-watchdog 等）。

### Added — 仓库元数据
- **`AGENTS.md`**（仓库根，~150 行）— 仓库视角的 agent 协作约定：
  5 秒决策树、目录布局、build/test、写代码约定、commit/PR 风格。
- **`LLMS.md`**（仓库根）— 4 行 stub 指向 AGENTS.md 与 `proxyctl agent-guide`。
- **`MIGRATION-0.3.md`**（仓库根）— 0.2.x → 0.3.0 破坏点清单与迁移建议。

### Changed
- **顶层 `proxyctl --help` 完全元数据驱动** — 从 COMMANDS_META 派生命令分组与
  badges；删除硬编码 box-drawing；新增"AGENT 接入"区块、全局 flag 段、
  环境变量段。`proxyctl --help` ≡ `proxyctl help`。
- **`proxyctl agent-guide` 重写** — 新增 4 段：Agent 第一次接入引导路径
  (6 步)、读/写/系统三分类表、envelope 字段含义表、锁文件位置 + 手动释放；
  重写 footgun（LOCKED / 订阅 / 多实例）。
- **统一错误退出走 `_io.fail()`** — cli/check/audit/trace/explain 中
  ~25 处 `sys.exit(1/2)` 收口到 `_io.fail(..., hint=, doc=, code=)`，
  保证所有失败路径都带 hint + doc + 分语义退出码。
  新增 `test_no_bare_sys_exit.py` 防退化（阈值 ≤ 13）。
- **`_exec_with_lock` 抛 `LockedError(path)`** 替代 BlockingIOError，
  错误消息含具体锁路径与 `lsof` / `rm` 步骤。
- **全 flag 位置无关** — `--json` / `--plain` / `--dry-run` / `--no-color` /
  `--quiet` 在任意位置都被识别；`cmd_log` 用新工具
  `_io.extract_flags(args, known)` 重构子命令 flag 解析。

### Added — 测试
- `test_no_bare_sys_exit.py` — sys.exit 数量阈值检查（≤ 13）。
- `test_help_output.py` — `help`/`--help`/`help <cmd>`/`<cmd> --help` 等价性。
- `test_version_json.py` — `--version --json` envelope + supported_features 验证。
- `test_side_effects_enum.py` — side_effects 枚举值集合 + 关键命令断言。
- `test_dry_run.py` — 8 个写命令 dry-run + mock subprocess 防泄漏。
- `test_plain_output.py` — emit_tsv / --plain 互斥 / topic 注册。
- `test_flag_position_invariance.py` — `cmd --flag` ≡ `--flag cmd` ≡ `cmd --json --flag`。
- `test_doctor_extended.py` — doctor 0.3.0 informational 字段验证。
- `test_commands_schema.py` — `commands --schema` JSON Schema 自洽 + 用以验
  `commands --json` 结构；新 topics 注册。
- 总测试数 360 → 426。

### Removed
- 旧 envelope schema v1 完全替换（无双写兼容）。
- `commands --json` 中 `side_effects: string` 形态。
- `--help` 中硬编码 box-drawing 文本（60 行 → 元数据驱动）。

## [0.2.2] — 2026-05-17

### Added
- **`check --json` 的 groups stage 完整结构化** —
  `[{name, type, now, tested_ago, members:[{name, delay_ms, is_now}]}]`，
  替代原来的 `"skipped_in_json_v1"` 占位。
- **`proxyctl completion [bash|zsh|fish]`** — 生成 shell 补全脚本，
  从 COMMANDS_META + TOPICS 动态派生。一键 `eval "$(proxyctl completion zsh)"` 生效。
  支持 `--json` 输出（envelope 含 script 字段）。
- **`man/proxyctl.1`** — 完整的 groff man page，含全局 flag / 命令表 /
  JSON envelope / 退出码 / 环境变量 / 文件位置 / 例子。
  `install.sh` 自动安装到 `~/.local/share/man/man1/`；`uninstall.sh` 同步清理。
  sdist 也带上 man page。

### Changed
- **`cli.main()` 重构** — 70+ 行 if-elif 替换为声明式 `DISPATCH` 路由表
  + 24 个 `_h_*` handler 函数，每个 handler ≤10 行。
  完整向后兼容（位置参数 / 别名 / 锁 / 拼写建议路径不变）。
- **dispatch 完整性测试** — `tests/unit/test_dispatch_coverage.py` 断言
  `DISPATCH` 表与 `COMMANDS_META` 完整对齐；防止新增命令时漏注册。

### Notes
- 345 passed（v0.2.1 基础 332 + 新增 13）。
- 不做：argparse subparsers 完整重构（v0.3 backlog）—— 当前 dispatch 表
  已足够清晰，强行 argparse 化 风险/收益比不划算。

## [0.2.1] — 2026-05-17

### Added — 第二批 Agent 友好打磨
- **`proxyctl <cmd> --help` / `-h`** — 子命令独立帮助，从 COMMANDS_META 派生
  （summary / 用法 / examples / exit_codes / badges：sudo / side_effects /
  json 支持）。`--json` 模式输出 envelope 含该命令的完整元数据。
- **`proxyctl check --json`** — 把 4 阶段事实收进 collector：
  `engine / mode / stages.{basic, groups, connectivity, outbound_ip,
  split_routing}`；引擎未运行直接输出 ENGINE_DOWN(5)+ envelope。
- **`proxyctl trace <domain> --json`** — 4 阶段
  `input / parsed / mode / stages.{dns, rules, connectivity, connections}`。
- **`proxyctl audit [days] --json` / `proxyctl audit apply --json`** — collector
  含 `scanned / candidates / proxy_ok / unknown / new_suffixes / applied`。
- **`proxyctl bench [groups...] --json`** — NDJSON 流式：每节点完成立即输出
  `{node, rtt_ms, ok, error}` 一行；末尾再输出 envelope summary 含
  `total / ok_count / fail_count / avg_rtt_ms / min/max / results[]`。
- **`proxyctl config set <dot.key> <value>`** — 原子写（tmp + rename）
  + `.bak` 备份 + 写后 YAML 校验 + 失败回滚；值类型自动推断
  （int / float / bool / null / JSON list/dict / str）。dot-key 支持
  （`corp_dns.server`）。受 `with_lock("config")` 保护。

### Added — 防退化与契约测试
- **`tests/unit/test_json_schemas.py`** — 用 `jsonschema` 锁定 v1 envelope /
  commands / doctor / explain / config get/set 的字段契约（13 个测试）；
  防止意外破坏 schema v1 兼容性。
- **`tests/unit/test_no_bare_ansi.py`** — 21 个端到端测试，断言
  `NO_COLOR=1` / `--json` 模式下 stdout/stderr 均不含任何 ANSI 转义。

### Changed
- 命令元数据：`check / trace / audit / bench / config` 的
  `supports_json` 字段从 `False` 改为 `True`。
- 退出码语义补全：`bench` 增加 `[3, 7]`（无可测组 / API 不可达）；
  `config` 增加 `[4]`（写文件失败）。

### Notes
- 完整向后兼容：`check / trace / audit / bench` 的人类输出 0 改动。
- `bench --json` 进度条静默（用 NDJSON 替代）；人类模式进度条不变。
- 332 passed（v0.2.0 基础 298 + 新增 34）。

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
