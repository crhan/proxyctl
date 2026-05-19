# Changelog

本项目使用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.5.2] — 2026-05-19

> 修 v0.4.7 引入的 `get_engine_version()` 解析对真实 mihomo binary 输出
> 不兼容（哥 2026-05-19 升 0.5.1 跑 `status` 时一眼看到 `mihomo vv1.19.10`
> + `(Sat, go1.24.3)`，双 v 前缀 + build_date 截断成星期几）。
> 零 schema 变更（`data.engine_version.version` 字段类型不变，仅值规范化
> 为始终不带 `v` 前缀）；agent 消费方无感升级。

### Fixed — `get_engine_version()` 兼容两种 mihomo -v 输出

`src/proxyctl/cli.py::get_engine_version` 原 regex 假设 mihomo 输出唯一格式
`Mihomo Meta 1.19.25 darwin arm64 with go1.26.3 2026-05-16T14:37:07Z`，但
实际 mihomo 在不同构建里存在两种输出：

| 格式 | 样本 | 旧解析结果 |
|---|---|---|
| ISO 风（CHANGELOG 0.4.7 范本）| `Mihomo Meta 1.19.25 ... 2026-05-16T14:37:07Z` | ✓ 正确 |
| **v 前缀 + Unix `date` 风** | `Mihomo Meta v1.19.10 linux amd64 with go1.24.3 Sat May 31 07:51:30 UTC 2025` | ✗ version="v1.19.10" / build_date="Sat" |

后果（v0.4.7 ~ v0.5.1）：
- **`status` / `doctor` 行**：`engine=mihomo vv1.19.10`（渲染层 `f"v{ver}"`
  + 数据已含 v → 双 v）+ `(Sat, go1.24.3)`（build_date 被截断成星期几）
- **`data.engine_version` JSON**：`version="v1.19.10"`，agent 用字符串比较
  做版本判定时容易写出与 ISO-风 binary 不一致的分支

修复策略（双重防御）：

1. **数据层规范化**：解析时 `lstrip("v")` → `version` 永远不带 `v` 前缀，
   渲染层统一拼 `v{version}`（与 macOS Homebrew / Linux 各种安装源对齐）。
2. **regex 兼容**：build_date 段 `(\S+)` → `(.+)$`，吞到行尾，容纳 Unix
   `date` 的多 token 格式。
3. **ISO 判定收紧**：原来 `if "T" in date_raw` 会被 `UTC` 字符串中的 T
   误判 → 截成 `'Sat May 31 07:51:30 U'`；改为正则强校验
   `^\d{4}-\d{2}-\d{2}T` 头才裁剪。

### Added — 测试覆盖两种 mihomo -v 格式（parametrize）

`tests/unit/test_cli_helpers.py::test_get_engine_version_handles_both_mihomo_formats`
新增 2 个 case：
- ISO 风 `2026-05-16T14:37:07Z` → `build_date == "2026-05-16"`
- v 前缀 + Unix date `Sat May 31 07:51:30 UTC 2025` →
  `version == "1.19.10"`（不带 v）+ `build_date == "Sat May 31 07:51:30 UTC 2025"`
  完整保留

并加反向断言 `not info["version"].startswith("v")` —— 未来若再有人引入
带 v 数据，CI 立即抓住，不必再次靠用户跑 `status` 肉眼发现。

### 验证

- `pytest`：**687 passed**（+1 parametrize 新增 case 中含 2 条样本）。
- 实地跑 `proxyctl status` 在哥本机：
  - 旧：`引擎  mihomo vv1.19.10 (Sat, go1.24.3) · proxy`
  - 新：`引擎  mihomo v1.19.10 (Sat May 31 07:51:30 UTC 2025, go1.24.3) · proxy`

### 红线

- 不破坏 ISO-风构建（旧测试 `1.19.25 ... 2026-05-16T14:37:07Z` 仍全绿）
- 不改 schema（`data.engine_version` 字段集 / 类型不变）
- 不改 0.4.7 lru_cache 行为（同进程 4 项常量缓存仍成立）

## [0.5.1] — 2026-05-19

### Fixed

- `controller.empty_secret` / `controller.weak_secret` 不再在 bind
  `127.0.0.1` / `::1` 时触发。Attack surface 是单一前置条件：本机回环
  时 secret 强度无意义，规则跳过。

  | bind | secret | 行为 |
  |---|---|---|
  | `127.0.0.1` / `::1` | 任意（含空） | 不报 |
  | `0.0.0.0` / LAN IP | ≥ 16 字符 | 仅 `public_bind` warn |
  | `0.0.0.0` / LAN IP | 非空 < 16 字符 | `public_bind` warn + `weak_secret` advisory |
  | `0.0.0.0` / LAN IP | 空 | `public_bind` warn + `empty_secret` warn |

- `controller.empty_secret` / `weak_secret` 的 `title` 段明确标注 bind
  地址，避免误读。
- `proxyctl explain suggestion:controller.*` 三个 topic 的修复路径
  把"改回 127.0.0.1 bind"列为首选。

## [0.5.0] — 2026-05-19

### Added — Doctor 引导建议（`data.suggestions[]`）

`proxyctl doctor` 在 5 项布尔健康分之外，新增结构化建议列表，覆盖**值得做但没做**
的事——订阅快到期、自动启动指向旧二进制、GeoIP 过期、API 配置不安全、代理组挂掉。
与 score 解耦，永不影响 exit code。

```text
proxyctl doctor  (5/5)  engine=mihomo v1.19.25 mode=proxy port=7890
  ✓  engine_up        ...
  ✓  port_listen      ...
  ...

suggestions (2/3):
  [!] proxy_group.mostly_dead   代理组 proxy-tuic 中 21/21 节点不可达 (100%)
  [*] subscription.expiring_soon  订阅 5 天内到期 · n2ray.dev
  ...and 1 more (use --json)
```

**21 条规则**：

- **订阅（7）**：`subscription.expired` / `expiring_soon` /
  `traffic_high|warn|exhausted`（70 / 90 / 100% 三档）/ `last_fetch_error` /
  `stale` / `missing`
- **自动启动（8）**：`autostart.unit_missing` / `binary_missing` /
  `binary_mismatch` / `version_mismatch` / `config_dir_mismatch` /
  `placeholder_unrendered` / `disabled` / `flapping`
- **安全（3）**：`controller.empty_secret` / `weak_secret` / `public_bind`
- **引擎/数据（2）**：`engine.outdated`（读 `~/.cache/proxyctl/known_versions.json`
  契约文件）/ `data.geo_stale`（`geoip.dat` / `geosite.dat` mtime > 30 天）
- **代理组（1）**：`proxy_group.mostly_dead`（mihomo `/proxies` API，单组
  ≥ 70% 节点延迟为 0 即报；多组同时挂分别输出独立 suggestion，fingerprint
  含 `group_name`）

### Added — Suggestion schema v1

每条 suggestion 字段：

```json
{
  "id":              "autostart.version_mismatch",
  "severity":        "info | advisory | warn",
  "actor":           "user | agent | cron | engine",
  "title":           "autostart 引擎版本 v1.15.0 ≠ PATH v1.18.10",
  "evidence":        { "autostart_version": "1.15.0", "path_version": "1.18.10" },
  "inspect_command": "proxyctl status --json | jq .data.engine",
  "fix_command":     null,
  "auto_fixable":    false,
  "doc":             "suggestion:autostart.version_mismatch",
  "fingerprint":     "abc123def456",
  "first_seen":      "2026-05-19T10:23:00Z",
  "since":           "0.5.0"
}
```

- **严重度 3 档**：`info` / `advisory` / `warn`（错误走 `envelope.hints[]`）
- **Actor 4 档**：`user` / `agent` / `cron` / `engine`，agent 据此决定
  "自己修 vs 问用户"
- **`evidence` 结构化**：agent 不必 regex `title` 拼凑事实
- **`inspect_command` ≠ `fix_command`** 拆开
- **`fingerprint`** 跨次调用稳定（默认 `sha1(id)[:12]`；规则可在
  `FINGERPRINT_EVIDENCE_KEYS` 显式声明 evidence 关键字段，
  如 `proxy_group.mostly_dead` 用 `group_name` 做多实例去重）
- **`first_seen`** 持续问题不重置（持久化到
  `~/.cache/proxyctl/suggestions_state.json`）
- **排序**：固定 `severity desc, id asc`

### Added — Doctor 新 flag

- `--suggest-only` — 跳过 5 项 score 探测，仅跑建议引擎。实测 0.18s
  （默认含 curl 探测 2-5s）。`data` 中 5 项布尔 + `score` / `healthy`
  置 `null`，agent 据 `data.doctor_mode`（`"full" | "suggest_only"`）识别。
- `--since <version>` — 屏蔽 `since > <version>` 的规则。
  `proxyctl doctor --since 0.4.7` 让老 CI 平滑迁移到 0.5.0 不爆红。
- `--no-suggest` — 关闭建议引擎，恢复 v0.4.x 极简体验。
- `--quiet` — 跳过 suggestion 人类输出块（`--json` 仍输出）。

### Added — `proxyctl autostart [inspect|sync]`

新写命令组。`sync` 把 plist / systemd unit 中的 binary 路径 + config 目录
同步到当前 PATH 值，一键修复 `autostart.binary_mismatch` /
`version_mismatch` / `config_dir_mismatch`：

```bash
proxyctl autostart                # 展示当前状态
proxyctl autostart inspect --json # 结构化输出
proxyctl autostart sync --dry-run # 预览 PlanStep[]
proxyctl autostart sync           # 写入（macOS 需 sudo）
```

- **macOS**：`plistlib` 原地修改，保留 `KeepAlive` / `RunAtLoad` /
  `EnvironmentVariables` 等用户已有字段
- **Linux**：正则替换 `ExecStart=` 整行；unit 中无 `ExecStart=` 时**拒绝
  执行**（防止覆盖被改造的 unit）
- `side_effects`：`sync` 时 `[process, system, config-write]`，
  `inspect` 时 `[]`
- `exit_codes`：含 `8` (LOCKED) / `10` (DEPENDENCY_MISSING：PATH 找不到
  binary)

### Added — 用户级屏蔽文件 `~/.config/proxyctl/suggestions.ignore`

一行一个 `id` 或 `fingerprint`，`#` 开头注释。

```text
# 我知道，本机能接受
controller.weak_secret
# 屏蔽某个具体死组（按 fingerprint）
bb60eec32908
```

Env var `PROXYCTL_SUGGEST_IGNORE_PATH` 覆盖。Agent 可通过 `ignore_set=`
参数传规则绕过或叠加。

### Added — `proxyctl explain suggestion:<id>`

21 条规则各自有 explain topic，含触发条件、修复路径、`verify` 命令。
CI 强校验完备性（`tests/unit/test_suggest_explain_completeness.py`）：
每个 id 必须有对应 topic；无孤儿 topic。

### Added — `supported_features` 探测点

`proxyctl --version --json`：

```json
"supported_features": {
  "doctor_suggestions":         true,
  "doctor_suggestions_v1":      true,
  "autostart_inspect":          true,
  "autostart_sync_cmd":         true,
  "doctor_suggest_only_mode":   true,
  "doctor_since_filter":        true,
  "suggestions_ignore_file":    true,
  "proxy_group_dead_check":     true
}
```

### Changed — Subscription hint 单一事实源

`subscription.summarize_hints()` 内部委托给 `to_suggestions()`，
派生 `envelope.hints[]` 字符串。`status` 与 `doctor` 共用同一套规则，
未来改阈值只动一处。

### Compatibility

- Envelope `schema_version` 不变；`data.suggestions[]` 是新增字段
- `DOCTOR_V2` schema：5 项布尔 + `score` / `max` / `healthy` 改为
  `["<type>", "null"]`（兼容 `--suggest-only` 模式），新增
  `doctor_mode` enum 字段
- `additionalProperties=true` 保证仅读老字段的 agent 透明忽略新字段
- 红线：doctor 不自动修复 / 不拉外网 / 不影响 exit code

### 文档

- `proxyctl agent-guide` 加 Suggestion 协议段
- `AGENTS.md` "Doctor suggestions" 完整 schema + decision matrix + jq cookbook
- 各 `src/proxyctl/*.py` 模块 docstring 含设计立场与契约

## [0.4.7] — 2026-05-19

### Added

- `status` / `doctor` 显示 mihomo 引擎版本：
  - `status` 人类首行：`引擎  mihomo v1.19.25 (2026-05-16, go1.26.3) · proxy`
  - `doctor` 人类首行：`proxyctl doctor  (5/5)  engine=mihomo v1.19.25 ...`
  - `status --json` `data.engine` 加 `version` 子字段（完整 dict）
  - `doctor --json` `data` 加 `engine_version` 字段
- `get_engine_version(backend_name)` helper：跑 `<binary> -v` 解析为
  `{binary, version, platform, go_version, build_date, raw}`，
  `@functools.lru_cache(maxsize=4)` 同进程缓存。解析失败仍返回含 `raw`
  的 dict（`version=None`），agent 可拿原文兜底。
- `supported_features.engine_version: true`。

### Compatibility

- Envelope `schema_version` 不变（`data` 内新增字段不破坏 v2）；
  老 agent 透明忽略新字段

## [0.4.6] — 2026-05-19

### Fixed

- `ARCHITECTURE.md` 9 处死引用：三层架构图 / 后端抽象示例 / 目录结构 /
  「添加新后端」「添加新命令」开发指南 / 测试段全部从早已不存在的
  `bin/proxyctl` + `lib/engine/` 改为 `src/proxyctl/cli.py` +
  `src/proxyctl/*.py`。
- `explain engine` topic 立场对齐 README：mihomo（首发，端到端验证）/
  sing-box（预留，未端到端验证 —— 类 / 路径 / audit / trace 解析已实现，
  但完整启停闭环未跑过生产）。
- `commands --json` 中 `mode` 命令 `exit_codes` 从虚假声明的
  `[0, 1, 2, 4, 6, 8]` 改为实际可能的 `[0, 1, 2]`。
- `README.md` 版本示例号同步当前最新。

### Compatibility

- 零行为变更、零 schema 变更

## [0.4.5] — 2026-05-19

### Changed

- `proxyctl explain subscription` topic 重写为两段：
  - 不更新订阅（由用户脚本或引擎 proxy-providers 负责）
  - 显示订阅状态（读 `~/.config/proxyctl/subscription.json` 契约文件）
- `proxyctl agent-guide` 顶部一句话明确"不更新订阅"的同时
  指向新的 Subscription Status 段。
- `commands --json` 中 `status` 命令 summary 更新，examples
  增加 `proxyctl status --json | jq .data.subscription`。
- `AGENTS.md`：`does not edit user rules, nodes, or **fetch** subscriptions`
  （v0.4.4+ 起读契约文件展示订阅状态）。

### Added

- `agent-guide --list-sections` 新增 `subscription-status` 段，
  覆盖 agent 消费 `data.subscription` / `envelope.hints[]` /
  风险阈值 / capability 探测。

## [0.4.4] — 2026-05-18

### Added — `status` 显示订阅状态

`status` 末尾新增 `SUBSCRIPTION` 段（仅当契约文件存在时打印）：

```
SUBSCRIPTION
  ✓ expire 2026-08-18 (91d left) · traffic 0.15G/500.00G (0.03%) · n2ray.dev
     updated 2m ago
```

`status --json` envelope 新增：

- `data.subscription`：完整快照（schema_version / fetch_ok / expire_at /
  expire_days_left / traffic_*_bytes / traffic_used_pct / info_nodes 等）
- `hints[]`：风险摘要（过期 ≤ 7 天 / 流量 ≥ 80% / fetch 失败时分级填入）

### Added — `src/proxyctl/subscription.py` 模块

公开 API：`load()` / `severity()` / `summarize_hints()` / `format_line()` /
`fmt_bytes()` / `updated_at_human()`。**proxyctl 自身不发起任何网络请求**——
只读 `~/.config/proxyctl/subscription.json` 契约文件。文件不存在或损坏时
`load()` 返回 `None`，不破坏 status 主流程。

Env var `PROXYCTL_SUBSCRIPTION_PATH` 覆盖默认契约文件路径（测试用）。

### Added — `supported_features.status_subscription: true`

### Schema — `~/.config/proxyctl/subscription.json` (v1)

字段全部可选（缺失即 `None`），由用户的订阅刷新脚本写入：

| 字段 | 类型 | 说明 |
|---|---|---|
| schema_version | int | 当前 1 |
| updated_at | str | ISO 8601 |
| fetch_ok | bool | 最近一次拉取是否成功 |
| fetch_http_status | int | HTTP 状态码（0=连接级失败）|
| fetch_channel | str\|null | "proxy-7890" / "direct" 等 |
| fetch_error | str\|null | 失败原因 |
| url_host | str | 订阅 URL hostname |
| expire_at | str\|null | ISO 8601 套餐到期 |
| expire_days_left | int\|null | 剩余天数（负=已过期）|
| traffic_upload_bytes | int\|null | 已上传字节 |
| traffic_download_bytes | int\|null | 已下载字节 |
| traffic_used_bytes | int\|null | 总已用 |
| traffic_total_bytes | int\|null | 套餐总流量 |
| traffic_used_pct | float\|null | 使用百分比 |
| info_nodes | list[str] | 机场塞节点列表的元信息行 |
| node_count | int | 主节点数 |
| relay_node_count | int | relay 节点数（可选）|
| http_relay_sub_ok | bool | relay 订阅成功（可选）|

仓库内 `update-subscription.sh` 是契约的参考实现。

### Compatibility

- Envelope `schema_version` 不变（`data` 内新增字段不破坏 v2）

## [0.4.3] — 2026-05-18

### Added

- `check --json` 失败时 `envelope.hints[]` 自动聚合真凶摘要，
  agent 不必再挖 `data.stages.*.ok` 自己定位：
  - `missing ports: proxy:7890 api:9090` — 端口失败
  - `engine not running` — 引擎未运行
  - `connectivity failed: discord,github` — 连通性失败项名列表
  - `split routing inactive (proxy == direct egress)` — 出口未走代理
  - ``DNS unhealthy — try `proxyctl fix` `` — DNS 异常

  全部通过时 `hints=[]`（与 0.3.x 行为一致）。

### Fixed

- macOS CI runner 上 `test_log_tail_*` 持续失败：测试 monkeypatch 错的类
  （`engine/mihomo.py` 而非 `cli.py` 中实际使用的 `MihomoBackend`），
  本地有真日志兜底所以本地 PASS，CI 无日志即挂。改 patch 目标到正确类，
  并新增防回归测试（patch 到不存在的目录路径，确保 patch 真生效）。

### Compatibility

- 零 schema 变更，0.4.2 消费者可无感升级

## [0.4.2] — 2026-05-18

### Fixed

- **`--dry-run` 在 5 个写命令上完全失效**：`start / stop / restart /
  restart-clean / recover` 不识别 `--dry-run` 而真执行。补 `_plan_*`
  helper + dispatcher 接 `_maybe_dry_run` + `COMMANDS_META.supports_dry_run:
  true` + shell 补全 `_DRY_RUN_CMDS` 同步：
  - `_plan_start` — `launchctl bootstrap` / `systemctl --user start` +
    DNS 注入 + dns-lock + 系统代理激活
  - `_plan_stop` — dns-lock 停 + DNS 还原 + 系统代理关 +
    `launchctl bootout` / `systemctl --user stop`
  - `_plan_restart` / `_plan_restart_clean`（多一步 `fs_remove cache_file`）
  - `_plan_recover` — 3 个 Clash API endpoint（`http_put` action）
- 新 helper `_service_start_argvs` / `_service_stop_argvs` /
  `_service_restart_argvs` / `_recover_curl_endpoints` 作为 plan ↔ exec
  单一事实来源。

### Added

- `proxyctl version` 子命令作为 `--version` flag 的等价别名。
  `proxyctl version --json` envelope `cmd: "version"` /
  `data.version` + `supported_features`。
- `supported_features.lifecycle_dry_run: true` /
  `supported_features.version_subcommand: true`。
- CI contract test 扩展：lifecycle 5 个命令 plan ⇔ exec 一致性 + 跨平台
  （macOS / Linux）+ 静态等价断言 + `restart-clean` 多 `fs_remove` 断言。
- VERSION 三源一致性 guard（`pyproject.toml` vs `proxyctl.__version__`
  vs `cli.VERSION` vs `envelope.meta.proxyctl_version` vs
  `cmd_version_print --json data.version`）。
- `--json` 错误路径不泄漏 traceback 的 5 个 USAGE 系列测试。
- `agent-guide --list-sections` 输出的每一个 slug 都 parametrize
  断言能取回非空 markdown。

### Compatibility

- 零 schema 变更；0.4.1 公开 CLI 完全兼容

## [0.4.1] — 2026-05-18

### Fixed

- `cmd_discovery` 在 Linux 平台 hardcode `engine_up=False`：Linux 用户
  裸 `proxyctl` 永远显示 `✗ engine=mihomo`、JSON discovery envelope
  `data.engine.running` 永远 false（即使 systemd 服务在跑）。改为走
  `service_running(backend)`（平台分支 launchctl / `systemctl --user is-active`）。

## [0.4.0] — 2026-05-18

正式版（0.4.0a1 → 0.4.0 无功能变化）。

### Documentation

- `README.md` 扩展 `--dry-run` 段：示例
  `proxyctl dns-unlock --dry-run --json | jq` 可复读 argv，列 9 种
  PlanStep.action 枚举，指向 contract test 文件。
- `man/proxyctl.1` 扩展 `--dry-run` 段：明确 "自 0.4.0 plan.target 全部
  真实化"，列 action 枚举，提及 CI contract test。

## [0.4.0a1] — 2026-05-17

### Added — Plan ↔ Exec 一致性

- 8 个写命令的 `data.plan[].target` 全部真实化：从占位符（`<plist_dst>` /
  `<svc>` / `system/<dns-lock.label>` 等）替换为完整 argv 字符串。
  agent 可 `target.split()` 直接当 shell 命令复读。
- 新 PlanStep `action` 类型 `system_op`：用于 networksetup 迭代等系统
  操作，`target` 为描述性字符串（`fix` / `mode` 用）。
- `agent-guide` 新增 "Plan action types" 段，枚举 8 种 action 及用法。
- `_<cmd>_subprocess_argvs` / `_resolve_daemon_paths` 5 个 plan ↔ exec
  共享 helper，杜绝"plan 手写、cmd 另一份代码"漂移。
- `tests/integration/test_plan_exec_contract.py` 11 个 contract test：
  5 个白盒（真跑 cmd_* mock subprocess 后断言 `actual ⊆ plan`）+
  4 个静态断言 + 故意漂移注入测试。

### Changed

- `_plan_mode` 不再含 `launchctl kickstart` 步骤（与 `cmd_mode` 实际
  行为对齐——只改 config，由用户手动 restart 生效）。
  **agent 复读 0.3.x 旧 plan 中的 kickstart 会误操作；本版修复**。

### Breaking — Private helpers only

- `_plan_daemon` / `_plan_audit_apply` / `_plan_dns_lock` /
  `_plan_dns_unlock` 函数签名扩展接收 `plist_src` / `plist_dst` /
  `full_label` / `backend` / `config` 等参数。**公开 CLI 行为完全不变**——
  仅影响仓库外直接 `from proxyctl.cli import _plan_*` 的代码
  （下划线前缀无稳定性承诺）。

## [0.3.3] — 2026-05-17

### Added

- `doctor --json` 新增 `data.healthy: bool` 字段（agent 不必再算
  `score == max`）。`supported_features.doctor_healthy_field: true`。
- `agent-guide --section <name>` / `--list-sections` —— agent 按需取小块
  markdown，避免每次拉 ~200 行全文。H2 标题改为 `English — 中文` 双语
  （ASCII slug 稳定供 agent 引用，中文供人类阅读）。模糊匹配 + did-you-mean。
  `supported_features.agent_guide_sections: true`。
- bash / zsh / fish 补全脚本覆盖 0.3.x 全部新 flag：
  `--dry-run`（写命令位置）/ `--plain`（audit/check）/
  `commands --schema` / `agent-guide --section/--list-sections` /
  顶层 `help <cmd>` / `log --tail/--no-follow` / `env --unset`。

### Changed

- `explain.set_global_flags` 同步设 `_io._JSON_MODE`：子模块直接调用
  （绕过 `cli.main`）时 `_io.fail` 拿到正确 JSON 模式。

### Compatibility

- 零 breaking、零 schema 变更，0.3.x 消费者无感升级

## [0.3.2] — 2026-05-17

### Fixed

- **`audit --plain` 主路径 `TypeError`**：函数签名变更时遗漏一处
  `_audit_emit()` 调用，导致扫描到候选域名后直接 `TypeError` exit 1。
- **`check --plain` connectivity 字段全错**：字段名漂移（应为
  `name / url / mode / ok / message`），TSV 一律输出 `None=X;None=X;None=X`。
- **`cmd_dns_unlock` 在 macOS 触发 `NameError`**：plist 路径变量未定义，
  bootout 之后 plist 文件永远删不掉。
- **`_plan_mode` / `_plan_engine` 输出 `system/system/...` 双前缀**：
  仅影响 `--dry-run` 展示，不影响真实执行；但 agent 解析 plan.target
  复读会得到错误命令。
- **`trace --json` envelope.ok 语义错位**：误把字符串 `remote_ip` 当
  布尔灌进 envelope `ok`。envelope 顶层 `ok` 固定为 `True`（trace 是
  诊断工具，跑完即成功）；新增 `data.stages.connectivity.remote_ip`
  暴露原始 IP。

### Compatibility

- 0.3.1 消费者可无感升级

## [0.3.1] — 2026-05-17

### Fixed

- 用户插件 ANSI 字面量泄漏到管道：当用户插件（如
  `~/.config/proxyctl/plugins/sb_private.py`）自己定义 `RED/GREEN/...`
  常量时，`set_no_color(True)` 后才加载的插件代码继续吐 ANSI 字面量
  到非 TTY 输出。`core/plugin.py` 在每个插件 import 完后立刻调一次
  `maybe_disable_module_colors`。
- `cli.VERSION` 硬编码字符串导致 `--version` 与 `pyproject.toml` 脱节。
  改为 `importlib.metadata.version("proxyctl")` 单一事实来源。
- `src/proxyctl/__init__.py` 的 `__version__` 同步修复。

## [0.3.0] — 2026-05-17

完整迁移见 [MIGRATION-0.3.md](MIGRATION-0.3.md)，agent 协议见
[AGENTS.md](AGENTS.md) 与 `proxyctl agent-guide`。

### Breaking

- **JSON envelope `schema_version`：1 → 2**：
  - 移除字段 `hint: string|null`，引入 `hints: string[]`（可多条）
  - 引入 `warnings: string[]`（非致命警告，与 hints 区分）
  - 引入 `meta` 对象：`ts` / `elapsed_ms` / `proxyctl_version` / `request_id`
  - **无兼容窗口**：消费者必须一次性升级到 v2
- **`commands --json` 中 `side_effects` 由 string 改为 enum list**：
  `["process", "system", "config-write", "cache", "network-io"]`。
  条件性副作用拆到新字段 `conditional_side_effects: { trigger: list[enum] }`。
- **`audit <not-a-number>` 不再静默 fallback 到 1**，改为 USAGE(2) + did-you-mean。
- **explain topic 的 `next` 字段重命名为 `next_commands`**。
- **无参 `proxyctl`（JSON / `PROXYCTL_AGENT` 模式）输出 discovery envelope**，
  不再吐完整 status 内容。人类模式不变（stderr banner + stdout status）。

### Added — Agent 能力

- `proxyctl --version --json` —— envelope 含 `supported_features` 能力探测表。
- `--dry-run`（全局 flag）—— 适用 7 个写命令（mode / engine / fix /
  audit apply / config set / daemon / dns-lock / dns-unlock），输出
  `data.plan = [PlanStep, ...]`。PlanStep 字段：
  `step / action / target / reversible / requires_sudo / side_effects / summary`。
- `--plain`（全局 flag）—— `audit` / `check` 输出 TSV（无 ANSI / 无 box /
  无 emoji）。与 `--json` 互斥。
- `proxyctl commands --schema` —— 输出 `commands --json` 的 JSON Schema
  (Draft 2020-12)，agent 可用以 validate。
- `proxyctl help <cmd>` —— 顶层 `help` 子命令等价 `<cmd> --help`，
  统一从 COMMANDS_META 派生。
- `log --json` NDJSON 规范化：首行 meta header
  `{schema_version, cmd, stream, path}`，后续每行事件 `{source, line}`。
- `doctor --json` 扩展 informational 字段：`engine / mode / port /
  config_path / engine_config_path / lock_held / lock_path`（不计分）。
- 新 explain topics：`subscription`（订阅边界）、`agent-protocol`
  （envelope/退出码/决策树 cheat sheet）、`locks`（锁文件位置 + 手动释放）、
  `flags`（全局 flag 速查）。
- LOCKED(8) 错误透出锁路径、`lsof` 排查命令、手动 `rm` 步骤；
  `doc: locks` 指向新 explain topic。
- 子选项级 did-you-mean：`mode tunn` / `engine mihomoo` / `daemon name stat` /
  `completion zsh-foo` 失败时给最接近的有效值建议。

### Added — 新退出码

- `9  TIMEOUT` —— 命令超时（bench / curl 等长跑路径）。
- `10 DEPENDENCY_MISSING` —— 依赖二进制 / 脚本缺失（mihomo / dns-watchdog 等）。

### Added — 仓库元数据

- `AGENTS.md` —— 仓库视角的 agent 协作约定。
- `LLMS.md` —— stub 指向 AGENTS.md 与 `proxyctl agent-guide`。
- `MIGRATION-0.3.md` —— 0.2.x → 0.3.0 破坏点清单与迁移建议。

### Changed

- 顶层 `proxyctl --help` 完全元数据驱动：从 COMMANDS_META 派生命令分组
  与 badges；删除硬编码 box-drawing；新增 "AGENT 接入" 区块、全局 flag 段、
  环境变量段。`proxyctl --help` ≡ `proxyctl help`。
- 统一错误退出走 `_io.fail()`：cli / check / audit / trace / explain 中
  ~25 处 `sys.exit(1/2)` 收口到 `_io.fail(..., hint=, doc=, code=)`，
  所有失败路径都带 hint + doc + 分语义退出码。
- `_exec_with_lock` 抛 `LockedError(path)` 替代 `BlockingIOError`，错误
  消息含具体锁路径与 `lsof` / `rm` 步骤。
- 全 flag 位置无关：`--json` / `--plain` / `--dry-run` / `--no-color` /
  `--quiet` 在任意位置都被识别。

### Removed

- 旧 envelope schema v1（无双写兼容）
- `commands --json` 中 `side_effects: string` 形态
- `--help` 中硬编码 box-drawing 文本

## [0.2.2] — 2026-05-17

### Added

- `check --json` 的 groups stage 完整结构化：
  `[{name, type, now, tested_ago, members: [{name, delay_ms, is_now}]}]`，
  替代原来的 `"skipped_in_json_v1"` 占位。
- `proxyctl completion [bash|zsh|fish]` —— 生成 shell 补全脚本，
  从 COMMANDS_META + TOPICS 动态派生。一键
  `eval "$(proxyctl completion zsh)"` 生效。支持 `--json` 输出
  （envelope 含 `script` 字段）。
- `man/proxyctl.1` —— groff man page，含全局 flag / 命令表 /
  JSON envelope / 退出码 / 环境变量 / 文件位置 / 例子。`install.sh`
  自动安装到 `~/.local/share/man/man1/`；`uninstall.sh` 同步清理。
  sdist 也带上 man page。

### Changed

- `cli.main()` 重构为声明式 `DISPATCH` 路由表 + 24 个 `_h_*` handler
  函数（每个 ≤ 10 行）。完整向后兼容（位置参数 / 别名 / 锁 / 拼写建议
  路径不变）。`tests/unit/test_dispatch_coverage.py` 断言 `DISPATCH`
  表与 `COMMANDS_META` 完整对齐，防止新增命令时漏注册。

## [0.2.1] — 2026-05-17

### Added — JSON envelope 覆盖更多命令

- `proxyctl <cmd> --help` / `-h` —— 子命令独立帮助，从 COMMANDS_META
  派生（summary / 用法 / examples / exit_codes / sudo / side_effects /
  json 支持）。`--json` 模式输出 envelope 含该命令的完整元数据。
- `proxyctl check --json` —— 4 阶段事实进 collector：
  `engine / mode / stages.{basic, groups, connectivity, outbound_ip,
  split_routing}`；引擎未运行直接 ENGINE_DOWN(5) + envelope。
- `proxyctl trace <domain> --json` —— 4 阶段
  `input / parsed / mode / stages.{dns, rules, connectivity, connections}`。
- `proxyctl audit [days] --json` / `audit apply --json` —— collector 含
  `scanned / candidates / proxy_ok / unknown / new_suffixes / applied`。
- `proxyctl bench [groups...] --json` —— NDJSON 流式：每节点完成立即输出
  `{node, rtt_ms, ok, error}` 一行；末尾再输出 envelope summary 含
  `total / ok_count / fail_count / avg_rtt_ms / min/max / results[]`。
- `proxyctl config set <dot.key> <value>` —— 原子写（tmp + rename）+
  `.bak` 备份 + 写后 YAML 校验 + 失败回滚；值类型自动推断
  （int / float / bool / null / JSON list/dict / str）。dot-key 支持
  （`corp_dns.server`）。受 `with_lock("config")` 保护。

### Added — 测试契约

- `tests/unit/test_json_schemas.py` —— 用 `jsonschema` 锁定 v1 envelope /
  commands / doctor / explain / config get/set 的字段契约。
- `tests/unit/test_no_bare_ansi.py` —— `NO_COLOR=1` / `--json` 模式下
  stdout / stderr 不含任何 ANSI 转义。

### Changed

- 命令元数据：`check / trace / audit / bench / config` 的
  `supports_json` 字段从 `false` 改为 `true`。
- 退出码语义补全：`bench` 增加 `[3, 7]`（无可测组 / API 不可达）；
  `config` 增加 `[4]`（写文件失败）。

### Compatibility

- 人类输出 0 改动
- `bench --json` 进度条静默（NDJSON 替代）；人类模式进度条不变

## [0.2.0] — 2026-05-17

### Added — Agent 接入一等公民

- `proxyctl agent-guide` —— 输出 markdown，含能力边界 / 概念地图 /
  退出码语义 / JSON envelope 规范 / 故障决策树 / non-interactive 承诺 /
  footgun。Agent 第一条该调用的命令。
- `proxyctl explain [<topic>]` —— 无参输出"想改 X 去哪？"速查表
  （rules / nodes / config / dns / ports / ...）；带 topic 输出卡片
  `SUMMARY / FILE / EDIT / VERIFY / NEXT`。13 个 topic，内容由当前
  backend 动态计算路径，不硬编码。
- `proxyctl commands [--json]` —— 列出所有命令的元数据：
  `group / side_effects / needs_sudo / interactive / supports_json /
  exit_codes / examples`。
- `proxyctl config path | get <key>` —— 让 agent 无需 grep 就能定位/查询
  自身配置；支持 dot 路径（`corp_dns.server`）。
- `proxyctl doctor [--json]` —— 极简 5 项布尔健康打分
  （engine_up / port_listen / dns_ok / system_proxy_ok / connectivity_ok）
  + score + hint。比 `status` 精简、比 `check` 快（< 2 秒）。

### Added — clig.dev 合规

- 统一 JSON envelope（schema v1）：
  `{schema_version, cmd, ok, data, error, code, hint, doc}`。失败时也输出
  完整 envelope 到 stdout。`status / doctor / explain / agent-guide /
  commands / config / log` 全部支持 `--json`。
- 分语义退出码：`0` OK / `1` GENERIC（旧路径）/ `2` USAGE /
  `3` NOT_FOUND / `4` PERMISSION / `5` ENGINE_DOWN / `6` CONFIG_ERR /
  `7` NETWORK_ERR / `8` LOCKED（写操作并发锁未拿到）。
- 颜色 / TTY 智能化：非 TTY、`NO_COLOR`、`TERM=dumb`、`--no-color` flag、
  `PROXYCTL_NO_COLOR=1`、`--json` 模式时一律关 ANSI。
- stdout / stderr 严格分流：JSON / 数据 → stdout；错误、警告、提示、
  进度 → stderr。错误信息一律带 `hint` + `doc`（指向 explain topic）。
- 写操作并发锁：`mode / engine / fix / dns-lock / dns-unlock /
  daemon start|stop|restart / audit apply` 用 `fcntl.flock` 在
  `~/.config/proxyctl/.lock.*` 加锁；拿不到锁立即 exit 8 + 结构化错误。
- 拼写建议：未识别子命令时 `difflib` 给"是否想要 X？"建议，exit 2。
- `PROXYCTL_AGENT=1` 一键模式：等价 `--json` + `--no-color` + 非交互。
- `proxyctl log` 多模式：保留默认 `tail -f`；新增 `--tail N` /
  `--no-follow` / `--json`（JSON Lines）。`--json --no-follow` 不再死循环。
- SIGPIPE 安全：`proxyctl commands --json | head` 不再抛
  `BrokenPipeError`；Ctrl-C 退出码 130。
- 全局 flag 位置无关：`--json` / `--no-color` / `--quiet` / `-q` 可在任意位置。

### Changed

- `--help` 顶部加 `AI Agent? → proxyctl agent-guide` 与
  `想改 ... 去哪？ → proxyctl explain` 入口。
- 裸 `proxyctl` 仍 = `status`（兼容），stderr 加一行 Agent 入口提示。
- `cmd_recover` 引擎未运行 → `ENGINE_DOWN(5)`（旧 `1`）。
- `trace` 缺参 → `USAGE(2)`（旧 `1`）。
- `daemon <X>` 未声明 → `NOT_FOUND(3)`（旧 `1`），并列出已声明列表。
- `proxyctl log` 文件不存在 → `NOT_FOUND(3)`。

### Compatibility

- 22 个既有命令的调用形式 100% 保留（包括 `proxyctl claude-proxy` 别名、
  `proxyctl audit 7`、`proxyctl audit apply`、裸 `proxyctl` 等）
- 旧 `sys.exit(1)` 路径未动；只在新代码和上述边界路径上启用新退出码

## [0.1.5] — 2026-05-17

### Added

- `no_proxy_extra` 配置项（默认 `[]`）：让用户在 `proxyctl env` 输出的
  `NO_PROXY` 末尾追加个人项（公司域名、Tailscale CGNAT 段
  `100.64.0.0/10` 和 MagicDNS `.ts.net` 后缀、本地服务等）。
  接受 `list[str]` 或逗号分隔字符串两种写法。
- `config.yaml.example` 补 `proxy_port` 与 `no_proxy_extra` 注释样例。

### Compatibility

- 保留默认 `NO_PROXY` 集合，仅新增"追加"语义，向后完全兼容。

## [0.1.4] — 2026-05-17

### Added

- `proxy_port` 配置项（默认 `7890`）：让引擎对外的 HTTP/SOCKS mixed-port
  在 status / check / env / wait 等所有命令中可配置。

### Changed

- `cmd_status` 端口列表改为 `(config.proxy_port, "proxy")` +
  `urlparse(api_base).port`，不再硬编码。
- `cmd_check` 第 1/4 阶段端口检测同上；第 3/4 阶段连通性 / 出口 IP 的
  `socks5h://127.0.0.1:7890` 改用 `proxy_port`，通过 `_test_url` /
  `_ipgeo` / `_fetch_probe` 的新 `proxy_port` 参数透传。
- `cmd_env` / `_wait_ready` 的 `7890` 改用 `config.proxy_port`。
- `_gather_ports(claude_proxy_label, port_list=None)` 加可选参数，
  `None` 时回退到 `[(7890, "proxy"), (9090, "api")]` 保持旧调用兼容。

### Compatibility

- 默认值仍为 7890 / 9090，所有现有用户行为完全不变；只在
  `~/.config/proxyctl/config.yaml` 加 `proxy_port:` 字段后才切换。

## [0.1.3] — 2026-05-15

### Fixed

- `cmd_start` / `cmd_stop` / `cmd_restart` / `cmd_fix` 接入 `RouteHook`
  调度（`_apply_route_hooks`）。`RouteHook` 数据类自插件机制引入起就
  已定义，但 CLI 一直未调用，导致用户插件（如本机 Tailscale `100.64/10`
  精细路由覆盖）无法在启动 / 重启 / fix 流程中真正生效。

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

- README 安装章节重写：首推 `uv tool install proxyctl` /
  `pipx install proxyctl`，`git clone` 仅在需要 launchd plist 或开发时使用。
- README 顶部加 PyPI / CI / Python versions / License 四个 badge。

### Added

- Dependabot 配置：每周一 09:00（Asia/Shanghai）扫一次 GitHub Actions
  版本，官方 `actions/*` 合并到单个 PR 减少噪声。

## [0.1.0] — 2026-05-14

首次发布到 PyPI。

### Added

- **状态面板** `proxyctl status` —— 引擎进程 / 端口 / TUN 接口 / DNS /
  系统代理 / 网络环境，并发采集顺序打印。
- **健康检查** `proxyctl check` —— 四阶段：基础状态 → 代理组节点延迟 →
  连通性测试 → 出口 IP 验证。
- **链路诊断** `proxyctl trace <domain>` —— DNS 解析（fakeip/realip 区分）→
  规则匹配预测 → 连通性测试 → 实际连接验证。
- **配置审计** `proxyctl audit [days] / audit apply` —— 扫描日志，找出
  "走代理但实际是国内 IP" 的域名，可自动写回 mihomo / sing-box 双 config
  直连规则。
- **节点测速** `proxyctl bench [groups...]` —— 通过 Clash API 并发触发
  延迟测试，进度条实时输出。
- **服务管理** `start / stop / restart / restart-clean / fix / recover` ——
  macOS launchctl + Linux systemctl user 两套底层。
- **模式切换** `proxyctl mode tun|proxy` —— TUN（全局接管）与代理模式互转。
- **DNS 守护** `proxyctl dns-lock [--reload] / dns-unlock` —— 内嵌 plist
  模板，对抗 DHCP / VPN / scutil 三层 DNS 覆盖。
- **守护进程接口** `proxyctl daemon <name> <subcmd>` 与
  `proxyctl engine mihomo|singbox` 切换。
- **后端抽象层** `engine/{base,mihomo,singbox}.py` —— 同一份 CLI 同时
  支持 Mihomo（首发）和 Sing-box（预留）。
- **插件机制** `core/plugin.py` —— 内置插件目录 +
  `~/.config/proxyctl/plugins/*.py` 用户插件目录，hook 类型涵盖检查项、
  出口探测、status 段、DNS / route 钩子、watchdog 层、audit 跳过项等。
- **内置插件**：`connectivity-basic`（google / github / baidu）+
  `corp-network`（企业 DNS / 内网目标，仅在 `corp_dns` 配置时启用）。
- **GitHub Actions CI**：matrix `ubuntu / macos` × Python `3.10/3.11/3.12/3.13`
  共 8 个测试矩阵格，加 `build` job 用 `twine check --strict` 验证 metadata。
- **GitHub Actions Release**：tag `v*.*.*` 触发，OIDC trusted publishing
  发到 PyPI（无 API token），自动创建 GitHub Release 并附 sdist + wheel。

### Fixed

- `cli.main()` 处理 `--help/-h/help` 后未 return，导致继续落入默认 else
  分支二次调用 `cmd_help()`，help 输出重复两次。

[Unreleased]: https://github.com/crhan/proxyctl/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/crhan/proxyctl/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/crhan/proxyctl/compare/v0.4.7...v0.5.0
[0.4.7]: https://github.com/crhan/proxyctl/compare/v0.4.6...v0.4.7
[0.4.6]: https://github.com/crhan/proxyctl/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/crhan/proxyctl/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/crhan/proxyctl/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/crhan/proxyctl/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/crhan/proxyctl/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/crhan/proxyctl/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/crhan/proxyctl/compare/v0.4.0a1...v0.4.0
[0.4.0a1]: https://github.com/crhan/proxyctl/compare/v0.3.3...v0.4.0a1
[0.3.3]: https://github.com/crhan/proxyctl/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/crhan/proxyctl/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/crhan/proxyctl/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/crhan/proxyctl/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/crhan/proxyctl/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/crhan/proxyctl/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/crhan/proxyctl/compare/v0.1.5...v0.2.0
[0.1.5]: https://github.com/crhan/proxyctl/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/crhan/proxyctl/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/crhan/proxyctl/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/crhan/proxyctl/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/crhan/proxyctl/compare/v0.1.0...v0.1.1
