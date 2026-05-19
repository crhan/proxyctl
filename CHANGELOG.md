# Changelog

本项目使用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.5.0] — 2026-05-19

> doctor 长期只是"5 项布尔健康分"，但 proxyctl 周边已经积累了一堆**值得提示**
> 的状态信号——订阅快到期、autostart plist 指向的 mihomo 与 PATH 里的不是一个、
> GeoIP 数据库 30 天没更新、Clash API secret 太短。这些都不算 "broken"，
> 但 agent 跑 doctor 时应该一并暴露出来，不必让用户/agent 自己挖。
>
> 本版加入 `data.suggestions[]` 维度（与 5 项布尔 score 完全解耦），
> 首发 **21 条规则**覆盖订阅 / autostart / 安全 / 引擎 / 数据五类。
> 永不影响 exit code，老 agent 透明忽略（字段追加 + additionalProperties=true）。

### 关键诉求来源（哥的原话）

> "doctor 能不能做的更多，比如订阅更新？还有什么功能之类的可以做一个引导提示"
> "自动启动依赖的是什么有没有管理比如 macos 用 launchdaemon，
>  然后 launch daemon 里面使用的 mihomo 是什么路径什么版本是不是也应该有检测"

第二条是关键：之前调试 TUIC 时栽过的坑—— PATH 装了 v1.20 但 plist 还指向 v1.15
旧 binary，`mihomo -v` 看到新版实际服务跑老版。0.4.7 加了 engine_version 但
只看 PATH binary，本版补齐 autostart binary 对比。

### Added — Suggestion Schema v1

每条 suggestion 字段（写进 `tests/unit/test_json_schemas.py::DOCTOR_V2`）：

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

设计立场：
- **severity 三档**：info / advisory / warn —— 没有 error / critical
  （错误走 `envelope.hints[]`，suggestion 永远是"该做的事"不是"出了事"）
- **actor 四档**：user / agent / cron / engine —— agent 据此决策"自己干 vs 问用户"
- **evidence 是结构化 dict**——agent 不必 regex title 拼凑事实
- **inspect_command ≠ fix_command**：拆开避免歧义
- **fingerprint = sha1(id)[:12]**：跨次 doctor 调用稳定去重的唯一字段，
  抖动字段（百分比、剩余天数）不进 hash
- **first_seen**：从 `~/.cache/proxyctl/suggestions_state.json` 读，
  CLI 维护，agent 不必碰；持续问题不重置时间戳
- **排序契约**：`severity desc, id asc`（写进 AGENTS.md，agent 可稳定 diff）

### Added — 首批 21 条规则

**订阅类（7）** —— `src/proxyctl/subscription.py::to_suggestions`：
- `subscription.expired` warn — `expire_days_left < 0`
- `subscription.expiring_soon` advisory — `0 ≤ days ≤ 7`
- `subscription.traffic_exhausted` warn — `used_pct ≥ 100`
- `subscription.traffic_warn` warn — `90 ≤ used_pct < 100`
- `subscription.traffic_high` advisory — `70 ≤ used_pct < 90`（UX review 指出 80% 太晚，改成 70/90 两级）
- `subscription.last_fetch_error` warn — `fetch_ok=false`
- `subscription.stale` info — `now - updated_at > 24h`
- `subscription.missing` info — `engine_up` 但快照不存在（仅 `--hint-missing` 触发）

**Autostart 类（8）** —— 哥主动加的维度，`src/proxyctl/autostart.py`：
- `autostart.unit_missing` warn — plist / unit 文件不存在（短路其他规则）
- `autostart.binary_missing` warn — plist 引用的 binary 不存在
- `autostart.binary_mismatch` advisory — plist binary ≠ `which mihomo`
- `autostart.version_mismatch` advisory — plist binary `-v` ≠ PATH binary `-v`
- `autostart.config_dir_mismatch` warn — plist `-d` ≠ `backend.config_dir`
- `autostart.placeholder_unrendered` warn — plist 含 `yourname` 字面量未替换
- `autostart.disabled` info — plist 存在但未 bootstrap / unit 未 enable
- `autostart.flapping` warn — launchctl LastExitStatus≠0 / systemctl is-failed

**安全配置类（3）** —— `src/proxyctl/suggest_rules.py`：
- `controller.empty_secret` warn — Clash API secret == ""
- `controller.weak_secret` advisory — secret 长度 < 16
- `controller.public_bind` warn — external-controller bind 0.0.0.0 / 局域网 IP

**引擎/数据/分组类（3）**：
- `engine.outdated` info/warn — 读 `~/.cache/proxyctl/known_versions.json` 契约文件
- `data.geo_stale` info — geoip.dat / geosite.dat mtime > 30 天
- `proxy_group.mostly_dead` warn — 调 mihomo `/proxies` API（本地 HTTP，
  timeout 0.5s，0 外网）。单组 ≥ 70% 节点 delay==0 即报；多组同时挂分别
  输出独立 suggestion（fingerprint 含 group_name，agent 可分别跟踪）

### Added — Fingerprint 升级（支持 evidence 关键字段）

`FINGERPRINT_EVIDENCE_KEYS` 表：每条规则可显式声明哪些 evidence 字段
进 fingerprint hash。proxy_group.mostly_dead 用 group_name —— 多组同时
挂得到独立指纹，agent 可分别跟踪。抖动字段（百分比、剩余天数）禁止
列入，否则跨次去重失效。`_compute_fingerprint()` 同时接受 dict（推荐）
和 id 字符串（向后兼容 v0.5.0-rc 旧调用方式）。

### Added — Doctor UX 三件套

- **`--suggest-only`**：跳过 5 项 score 探测（curl connectivity 累计 2-5s）,
  仅跑 suggestion 引擎。实测 0.18s。data 中 5 项布尔 + score/healthy
  全置 null，agent 据 `data.doctor_mode` 字段识别。DOCTOR_V2 schema
  对应放宽（boolean → ["boolean","null"]），不破坏 required 列表。
- **`--since <version>`**：屏蔽 `since > <version>` 的规则。例：
  `proxyctl doctor --since 0.4.7` 让老 CI 平滑迁移到 0.5.0 时不爆红。
  inclusive 语义：since=0.5.0 保留 since=0.5.0 规则。
- **`~/.config/proxyctl/suggestions.ignore`**：用户级屏蔽文件。
  一行一个 id 或 fingerprint，# 开头注释。env var
  `PROXYCTL_SUGGEST_IGNORE_PATH` 覆盖（测试用）。agent 可显式
  `ignore_set=` 参数传规则绕过/叠加。

### Added — `proxyctl autostart` 写命令

新子命令组：
- `proxyctl autostart` / `inspect` —— 只读，展示 plist/unit 现状
- `proxyctl autostart sync` —— **写命令**：plist/unit 中 binary +
  config_dir 同步到 PATH 当前值。当 doctor 报 autostart.binary_mismatch /
  version_mismatch / config_dir_mismatch 时，agent 或用户一键修复，
  不必手动编辑 plist + bootout + bootstrap。

macOS：plistlib.load → in-place 改 ProgramArguments → plistlib.dumps
（保留 KeepAlive / RunAtLoad / EnvironmentVariables 等用户已有定制）。
Linux：正则替换 ExecStart= 整行；ExecStart 缺失时**拒绝**执行
（unit 被改得面目全非时不冒险覆盖）。

完整 plan/exec 合约：
- macOS：launchctl bootout → fs_write_atomic plist → launchctl bootstrap
- Linux：systemctl stop → fs_write_atomic unit → daemon-reload →
  systemctl start

`--dry-run` 输出 PlanStep[] 含 sudo 标记。`side_effects` 标
`process / system / config-write`，`exit_codes` 含 8 (LOCKED) /
10 (DEPENDENCY_MISSING，PATH 中找不到 binary 时)。

相应 explain topic 的 `edit` 段加入 "proxyctl autostart sync" 一键修复指引。

### Added — 8 个 doctor 集成点

`src/proxyctl/explain.py::cmd_doctor`：
- 新增 `--no-suggest` flag，关闭整个建议引擎（恢复 v0.4.x 极简体验）
- `data.suggestions[]` 字段输出全部（含 info 级，`--json` 必现）
- 人类输出新增 suggestions 块：仅显 warn + advisory，上限 3 条，
  超出折叠为 `...and N more (use --json)`
- `--quiet` 完全跳过 suggestion 块（修复历史 quiet 不抑制 print 的潜在 bug）
- 符号：`[!] warn` / `[*] advisory` / `[i] info`（纯 ASCII）
- 调用所有 inspect_* 都独立 try-except：建议引擎绝不能阻塞 doctor 主流程

### Added — 21 个 explain topics

每条 suggestion id 都注册了 `proxyctl explain suggestion:<id>`：
- 用户/agent 看到 doctor 输出后能一键跳到详情
- CI 强校验 (`tests/unit/test_suggest_explain_completeness.py`)：
  - 每个规则 id 必须有对应 explain topic
  - 无孤儿 topic（规则砍了 explain 也得砍）

### Added — supported_features 探测点

`proxyctl --version --json`：
```json
"supported_features": {
  ...
  "doctor_suggestions":         true,   // 0.5.0
  "doctor_suggestions_v1":      true,   // schema v1（未来 bump 用新 key）
  "autostart_inspect":          true,   // 0.5.0
  "autostart_sync_cmd":         true,   // 0.5.0
  "doctor_suggest_only_mode":   true,   // 0.5.0 --suggest-only
  "doctor_since_filter":        true,   // 0.5.0 --since <ver>
  "suggestions_ignore_file":    true,   // 0.5.0 用户屏蔽文件
  "proxy_group_dead_check":     true    // 0.5.0 proxy_group.mostly_dead
}
```

agent 据此决定要不要解析 `data.suggestions[]` / 用新 flag。

### Refactor — 单一事实源（消除 status/doctor 文案漂移）

`subscription.summarize_hints()` 重构：内部委托给新的 `to_suggestions()`，
派生 `envelope.hints[]` 字符串。status 和 doctor 共用一套规则推导逻辑，
未来改阈值只动一处。

### 红线（doctor 即使扩展也不碰的）

1. doctor 不自动修复——`suggestion.fix_command` 是字符串，从不主动执行
2. doctor 不拉网络——所有"过期"判定来自本地 `subscription.json` / `known_versions.json`
3. suggestion 不影响 exit code——21 条 warn 全亮也是 exit 0
4. 规则失败静默降级——任何 inspect 异常 → 那一组规则跳过，不影响其他维度

### 验证

- `pytest`：**683 passed**（含 39+ 新 doctor/suggest/autostart 测试）
- 实地跑 `proxyctl doctor` 在哥本机一次性发现：
  Clash API secret 长度 14 < 16（advisory）+
  geoip.dat / geosite.dat 57 天没更新（info）+
  proxy_group.mostly_dead 某组节点全挂（warn）
- 实地跑 `proxyctl doctor --suggest-only` 0.18s 出 suggestions（vs full 模式
  含 curl 2-5s）
- 实地跑 `proxyctl autostart sync --dry-run` 哥本机 in-sync → no-op 路径触发

### 设计文档参考

- `src/proxyctl/suggest.py` 模块 docstring：Schema v1 完整契约
- `src/proxyctl/autostart.py` 模块 docstring：两层 inspect 设计
- `proxyctl explain suggestion:<id>`：每条规则的触发逻辑 + 修复路径

## [0.4.7] — 2026-05-19

> proxyctl 是引擎生命周期管理 CLI，但**之前任何位置都不显示引擎版本号**——
> debug TUIC / DNS 等引擎层问题时无法快速定位"是否引擎版本 regression"。
> 本版填补此缺陷：`status` / `doctor` 人类与 JSON 输出全部带 mihomo 版本。
> 零行为变更、零 schema 变更（envelope.data 新增字段不破坏 v2）。

### Added — `get_engine_version()` helper

`src/proxyctl/cli.py` 新增：

```python
@functools.lru_cache(maxsize=4)
def get_engine_version(backend_name: str) -> dict | None:
    """运行 `<backend> -v` 解析引擎版本信息。

    Returns dict 或 None（binary 找不到 / -v 失败 / 解析失败）：
        {
          "binary":     "/opt/homebrew/bin/mihomo",
          "version":    "1.19.25",
          "platform":   "darwin arm64",
          "go_version": "1.26.3",
          "build_date": "2026-05-16",
          "raw":        "Mihomo Meta 1.19.25 darwin arm64 with go1.26.3 ...",
        }
    """
```

设计要点：
- `shutil.which(name)` 找 binary，跑 `<binary> -v`，3s timeout
- 解析 mihomo `Mihomo Meta <ver> <platform> with go<ver> <ISO-date>` 格式
- 解析失败时仍返回 dict（含 raw），`version=None` —— agent 可拿原文兜底
- `@functools.lru_cache(maxsize=4)` 同进程内常量缓存，避免 status 每次都跑 subprocess

### Added — `status` 显示引擎版本

人类模式首行：

```
引擎  mihomo v1.19.25 (2026-05-16, go1.26.3) · proxy
```

`status --json` envelope.data.engine 新增 `version` 子字段（完整 dict）。

### Added — `doctor` 显示引擎版本

人类模式打分行：

```
proxyctl doctor  (5/5)  engine=mihomo v1.19.25 mode=proxy port=7890
```

`doctor --json` envelope.data 新增 `engine_version` 字段（完整 dict）。

### Added — `supported_features.engine_version = true`

agent 探测 0.4.7+ 可直接消费 `engine_version`，无需 fallback。

### Added — 测试覆盖 6 个

`tests/unit/test_cli_helpers.py` 新增：
- happy parse（标准 mihomo -v 输出）
- binary not found → None
- 非 0 退出码 → None
- 解析失败但保留 raw（version=None）
- LRU cache 命中（删 binary 后第二次仍拿到结果）
- 必要字段集合烟雾测

### Backstory

今天 debug TUIC 全死时，最初没意识到 mihomo 版本（1.19.24）才升级到了 4 月底。
直到我自己跑 `mihomo -v` 才发现版本，绕了几圈才把"升级 mihomo 看看"
列入诊断方向。`status` 本来就该一眼看到——本来是 1 句话的诊断变成了几轮探索。

### Known: TUIC 当前仍 21/21 死

mihomo 1.19.24 → 1.19.25 升级**未解决** TUIC dial 不发包问题。HTTP 节点 21/21
全活，实际使用无影响（mihomo Fallback 自动路由 proxy → proxy-http）。
深入修 TUIC 留 backlog（可能要试 `udp-relay-mode: native` / `reduce-rtt: false`
/ 或者 mihomo TUIC client 实现 bug 上游 issue）。

### Test stats

总测试数 547 → **553**（+6 engine version helper）。

## [0.4.6] — 2026-05-19

> 全仓库矛盾审计 + 修复。0.4.5 修了订阅描述的双立场矛盾后，"举一反三"
> 三个并发 agent 通查全仓，又找到 4 处类似的「文档说不做 / 实际做了」
> 或「metadata 撒谎」的矛盾。零行为变更、零 schema 变更。

### Fixed — ARCHITECTURE.md 9 处死引用（P0）

整篇文档基于早已不存在的 `bin/proxyctl` + `lib/engine/` 目录结构写作，
但项目早就重构为 PEP 517 / `src/proxyctl/` 布局。9 处死引用一次清理：

- 三层架构图：`CLI 层 (bin/)` → `CLI 层 (src/proxyctl/cli.py)`；
  `工具层 (lib/)` → `工具层 (src/proxyctl/*.py)`
- 后端抽象代码示例：`# lib/engine/base.py` → `# src/proxyctl/engine/base.py`
- 目录结构图：整段重写，含 `src/proxyctl/` 全部子模块（_io / subscription /
  explain / completion / builtin_plugins / core / engine）+ tests / systemd /
  launchdaemons / man / config.yaml.example / pyproject.toml / uv.lock 等
- 「添加新后端 / 添加新命令」开发指南：路径全更新（含 dispatcher 表
  `_DISPATCH` 注册 + COMMANDS_META metadata 字段说明 + supported_features
  flag 流程）
- 测试段：`python3 bin/proxyctl status` → `uv run proxyctl status`

### Fixed — `explain engine` sing-box 立场对齐（P0，复刻 0.4.5 模式）

`explain.py:122` `_t_engine` topic 仍说"支持 mihomo / sing-box"——与
README v0.4.4 / config.yaml.example 已经明确的"sing-box 预留 / 未端到端
验证"打架，agent 看到这条立场会以为 sing-box 完整可用。

```diff
- "支持 mihomo / sing-box；..."
+ "支持 mihomo（首发，端到端验证）/ sing-box（预留，未端到端验证 —
+  类 / 路径 / audit / trace 解析已实现，但完整启停闭环未跑过生产）；..."
```

### Fixed — `mode` 命令 exit_codes metadata 撒谎（P1）

`explain.py:1000` 的 COMMANDS_META 里 `mode` 命令声称 `exit_codes:
[0, 1, 2, 4, 6, 8]`，但 `cmd_mode` 实际只可能返回 `[0, 1, 2]`（仅
`_io.fail(code=_io.USAGE)` 一处 fail 路径）。`4 CONFIG_ERR / 6 PERMISSION
/ 8 LOCKED` 都没有任何代码路径产生。agent 用 metadata 做错误分类会按
不可能的 code 路由。修正为 `[0, 1, 2]`。

### Fixed — README 版本示例号过期（P1，流程问题）

`README.md:167` 还是 `# → proxyctl v0.4.3`，实际跟随发版号应是当前最新。
本版更新到 `v0.4.6`。每次发版后需要 grep README 里的版本号 example，
**记入 release-process backlog**。

### Audit 结果总结

3 个并发 agent 扫了 6 个方向，验证后真实矛盾：4 条（P0 × 2 + P1 × 2）。
误判撤销：2 条（agent_guide_sections 数错 / hints 措辞瑕疵不是问题）。
P2 留 backlog：`recover` exit_codes NOT_FOUND(3) 缺漏、`daemon`
needs_sudo 不区分子命令、Backend 双胞胎类。

### Meta — 元模式总结（非代码变更）

矛盾的结构性根因：同一立场散落在 ≥3 处（README / agent-guide / explain
topic / COMMANDS_META / 代码 docstring），任意一处变更其他处不会自动
同步。**3 轮（0.4.4 sub 漏 docs / 0.4.5 docs 补 / 0.4.6 engine + arch
再补）后**仍未结构性解决，下次如再栽，考虑：(a) 集中字符串常量到
single source；(b) 加 lint 测试 grep 关键短语在文档间一致。本版不做。

## [0.4.5] — 2026-05-19

> v0.4.4 收尾补丁：补齐 agent 自描述链路对订阅显示能力的发现性。
> 0.4.4 把能力做了、`supported_features.status_subscription` 探针埋了，
> 但 `agent-guide` / `explain subscription` / `commands --json` 里仍是
> 旧措辞「proxyctl 不管订阅」，造成 agent 拿到 capability flag 也无从
> 路由到使用文档。本版统一双重立场：**不更新订阅 + 显示订阅状态**。
> 零行为变更、零 schema 变更。

### Changed — `explain subscription` topic 重写

旧版只讲「proxyctl 不更新订阅」立场。新版分两段：
- (1) **不更新订阅** —— 由用户脚本或引擎自身 proxy-providers 负责（旧立场不变）
- (2) **显示订阅状态** —— 通过 `~/.config/proxyctl/subscription.json` 契约文件
  读取（v0.4.4+），关键字段 + 写入方约定全列出，最后给 agent 消费命令
  （`proxyctl status --json | jq .data.subscription`）

### Added — `agent-guide` 新增 `Subscription Status` 段

`proxyctl agent-guide --list-sections` 现在返回 **16 个段**（v0.4.4 是 15）：
新增 `subscription-status`，覆盖：
- agent 怎么消费 `data.subscription` 与 `envelope.hints[]`
- 风险阈值表（warn ≤ 7d expire / ≥ 80% traffic；critical = 已过期 / 100% / fetch fail）
- 谁来写契约文件（用户脚本，参考 `update-subscription.sh`）
- capability 探测（`supported_features.status_subscription`）

### Changed — `agent-guide` 措辞统一

- 顶部一句话：原「不改订阅」→「不更新订阅」；并补充 v0.4.4 起 proxyctl
  显示订阅状态，引导到新段
- `Exclusions` 段订阅条目：精确化为「不更新订阅」+ 反向指向 `Subscription Status` 段
- `_t_nodes` topic 顺手统一：从「不管订阅更新」→「不发起订阅拉取；但显示订阅状态」

### Changed — `commands --json` status 命令元数据

```diff
- summary: "系统状态面板"
+ summary: "系统状态面板（含订阅状态：到期/流量/拉取健康度，v0.4.4+）"
  examples: [
    "proxyctl status",
    "proxyctl status --json",
+   "proxyctl status --json | jq .data.subscription"
  ]
```

agent 用 `commands --json` 探测能力时直接看到例子，不必再去 explain。

### Changed — `AGENTS.md` 仓库协议文档

原「does not edit user rules, nodes, or subscriptions」→「does not edit
user rules, nodes, or **fetch** subscriptions」+ 新增一段明确说 v0.4.4+
读契约文件显示订阅状态。

### Test stats

- 总测试数 546 → **547**（+1：`test_agent_guide_sections.py` 的 parametrize
  测试自动覆盖新增的 `subscription-status` section，断言 `--section
  subscription-status` 输出非空 markdown 且 `envelope.ok=True`）。
- 现有 23 个 `subscription.py` 测试全部继续通过。

## [0.4.4] — 2026-05-18

> `status` 显示订阅状态（到期日 / 剩余流量 / 拉取状态）。proxyctl 自己不
> 主动拉远端订阅 —— 通过一份 `~/.config/proxyctl/subscription.json` 契约
> 文件由用户脚本写入；本版加上读取、渲染、风险摘要到 envelope.hints 的
> 链路。零 breaking、零 schema 变更（envelope v2 不变，新增字段在 data 内）。

### Added — `status` 命令显示订阅状态

`status` 末尾增加 `SUBSCRIPTION` 段（仅当契约文件存在时打印）：

```
SUBSCRIPTION
  ✓ expire 2026-08-18 (91d left) · traffic 0.15G/500.00G (0.03%) · n2ray.dev
     updated 2m ago
     官网:next.n2ray.dev
     已用:0.0%
     到期:2026-08-18
```

`status --json` envelope 多两块字段：
- `data.subscription`：完整快照（schema_version / fetch_ok / expire_at /
  expire_days_left / traffic_*_bytes / traffic_used_pct / info_nodes / ...）
- `hints[]`：风险摘要（过期 ≤ 7 天、流量 ≥ 80%、fetch fail 时分级填入）

### Added — `src/proxyctl/subscription.py` 新模块

公开 API：`load()` / `severity()` / `summarize_hints()` / `format_line()` /
`fmt_bytes()` / `updated_at_human()`。proxyctl 本身不拉网络、不解析订阅
URL，只读契约 JSON 文件。

环境变量 `PROXYCTL_SUBSCRIPTION_PATH` 覆盖默认路径
`~/.config/proxyctl/subscription.json`（测试 / 多账户场景用）。

文件不存在或损坏时 `load()` 返回 None，不破坏 status 主流程
（订阅状态非必需 —— 单纯本地用代理也能跑）。

### Added — `supported_features.status_subscription = true`

agent 探测 0.4.4+ 后可直接消费 `data.subscription`，无需 fallback 检测。

### Added — 测试

`tests/unit/test_subscription.py` 新增 **23 个测试**，覆盖：
- `load()` happy / missing / corrupt / non-dict 四种路径
- `severity()` 5 个状态切换（ok / warn-expire / warn-traffic / critical-*）
- `summarize_hints()` 5 类场景（含 fetch-failed short-circuit）
- `format_line()` happy / expired / fetch-failed
- `fmt_bytes()` GB / 0 / None
- `updated_at_human()` 相对时间 + bad input

### Schema — `~/.config/proxyctl/subscription.json` (v1)

契约字段（全部可选，缺失 = None）：

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

更新者（用户的订阅刷新脚本）：每次拉订阅后写此文件 —— 成功或失败都写
（fetch_ok=false 时填 fetch_error）。本仓库 `update-subscription.sh` 已
落实此契约，可作参考实现。

### Test stats

- 总测试数 523 → 546（+23 subscription 模块）。

## [0.4.3] — 2026-05-18

> CI 修复 + agent 体感增强：修 GitHub Actions 连续 4 次失败的真凶（测试
> monkeypatch 错的类，本地有真实 log 兜底所以通过、CI 没有就挂），同时把
> `check --json` 失败时的真凶聚合到 envelope.hints，agent 不再需要挖
> `stages.*.ok` 自己定位。零 breaking、零 schema 变更，0.4.2 消费者可
> 无感升级。

### Fixed — CI 连续失败（P0，必修）

`tests/unit/test_flag_position_invariance.py::test_log_tail_*` 在 macOS CI runner 上
持续挂（最近 4 个 release 全部失败），但 `Release to PyPI` 工作流照常发包。
真凶：

- **测试 monkeypatch 错的类**。代码里有两份 `MihomoBackend` 双胞胎：
  - `proxyctl.cli.MihomoBackend`（cli.py:115，`cli.main` 实际使用的那份）
  - `proxyctl.engine.mihomo.MihomoBackend`（engine 模块那份，当前未被 main 路径用）
- 测试 patch 的是 engine 那份 → patch 从未生效 → `cmd_log` 拿到真路径
  `~/.config/mihomo/mihomo.log`。本机有真 log 兜底所以本地 PASS，CI runner
  没有这个文件就挂，envelope 输出"日志文件不存在"19 行 JSON，断言期望 4 行。
- 修复：测试 patch 改为 `proxyctl.cli.MihomoBackend.log_file`。

### Added — 防回归测试

- `test_log_tail_monkeypatch_targets_correct_class_no_real_logfile_required` —
  显式 patch 到一个**不存在**的目录路径，确保 monkeypatch 真生效（如果未来
  又有人 patch 错类，本测试会立即抓住，不必依赖 CI 偶发场景才暴露）。

### Added — `check --json` 失败摘要聚合到 envelope.hints（P0 体感）

历史行为：`check` 任一 stage 失败时 envelope 顶层 `ok=False / code=1`，但
`hints=[] / warnings=[] / error=null` — agent 拿到 ok=False 必须自己挖
`data.stages.connectivity[].ok` / `data.stages.basic.ports.fail` 才能定位
真凶，违反 v0.3.0 引入的 envelope 设计精神（顶层字段应足够 agent 路由）。

修复：新增 `check._collect_fail_hints(collector, dns_bad, failed)` helper，
在 `emit_json` 之前根据 collector 聚合摘要：

- `missing ports: proxy:7890 api:9090` — basic.ports.fail 非空
- `engine not running` — basic.daemon_up=False
- `connectivity failed: discord,github` — connectivity 失败项名列表
- `split routing inactive (proxy == direct egress)` — split_routing.ok=False
- `DNS unhealthy — try \`proxyctl fix\`` — DNS 异常时（替换原 hint 同语义）

`check` 全部通过时 hints 为空列表（与历史 `hint=null → hints=[]` 行为一致）。

### Added — `check._collect_fail_hints` 测试覆盖

- `test_check.py` 新增 6 个测试：empty-when-pass / connectivity-failed-names /
  dns-bad-appends-fix / missing-ports-and-engine-down / split-routing-inactive /
  aggregates-multiple-categories。

### Known tech debt（不在本版处理）

- **双胞胎 `MihomoBackend` / `SingboxBackend`**：`cli.py` 和 `engine/*.py`
  各自维护一份，签名不同（`__init__` 一个吃 dict、一个吃字符串）。属历史
  遗留，合并需重审 cli 内所有 backend 使用点，单独 PR 处理。
- **connectivity 目标 `optional: true` 配置项**：让 discord 这种长期失败
  的目标不计入整体 fail。扩 config schema 属 minor bump（0.5.0）。

### Test stats

- 总测试数 516 → 523（+7：1 防回归 monkeypatch / 6 hints 聚合）。

## [0.4.2] — 2026-05-18

> 一次性收拾 0.4.1 自查发现的 dry-run 契约硬合同破裂（P0）+ 配套测试盲区
> （P1）+ 体感改进（P2）。零公开 schema 变更，行为完全向后兼容。

### Fixed — `--dry-run` 契约硬合同破裂（P0，关键）

5 个写命令的 `--dry-run` **完全失效且会真执行**，README/man page 全局承诺
"写命令支持 `--dry-run`" 在它们身上是空头支票。直接复现：

```
$ proxyctl stop --dry-run --json    # 0.4.1
mihomo stopped                       # ← 真的把 mihomo 停了
```

修复列表（每条都补 `_plan_*` helper + 在 dispatcher 接 `_maybe_dry_run` +
COMMANDS_META 标 `supports_dry_run: True` + completion `_DRY_RUN_CMDS` 同步）：

- **`start`** — `_plan_start` 列 `launchctl bootstrap` / `systemctl --user start`
  + macOS DNS 注入 + dns-lock + 系统代理激活的全套上界
- **`stop`** — `_plan_stop` 列 dns-lock 停 + DNS 还原 + 系统代理关闭 +
  `launchctl bootout` / `systemctl --user stop`
- **`restart`** — `_plan_restart(clean=False)` 列 `launchctl kickstart -k` /
  `systemctl --user restart` + DNS/代理刷新
- **`restart-clean`** — `_plan_restart(clean=True)` 比 restart 多一条
  `fs_remove backend.cache_file`
- **`recover`** — `_plan_recover` 列三个 Clash API endpoint（http_put action）

`_service_start_argvs` / `_service_stop_argvs` / `_service_restart_argvs` /
`_recover_curl_endpoints` 四个新 helper 作为 plan ↔ exec 单一事实来源，
按 `IS_MACOS` 分流。

### Added — Contract test 覆盖（P1）

- `tests/integration/test_plan_exec_contract.py` 补 9 个测试：
  - 白盒：`cmd_start` / `cmd_stop` / `cmd_restart` 在 macOS + Linux 两种平台下
    真跑（mock subprocess），断言 `actual ⊆ plan` 的 subprocess 部分
  - 白盒：`cmd_recover` 用宽松断言（curl argv 中包含 plan http_put URL 子串）
  - 静态：`_plan_start/_plan_stop/_plan_restart/_plan_recover` target 无 `<...>` 占位符
  - 静态：lifecycle plan subprocess 严格等于 `_service_*_argvs` helper 输出
    （按平台 parametrize）
  - 静态：`_plan_restart(clean=True)` 比 `_plan_restart(clean=False)` 多 1 步
    `fs_remove backend.cache_file`

### Added — VERSION 三源一致性 guard（P1）

- `tests/unit/test_version_consistency.py` 新增 5 个测试。以 `pyproject.toml`
  为唯一事实来源，断言：
  1. `proxyctl.__version__`（importlib.metadata 动态读）一致
  2. `cli.VERSION` 一致
  3. `_io.envelope().meta.proxyctl_version` 一致
  4. `cmd_version_print --json` 的 `data.version` 一致
  5. 新增 `version` 子命令与 `--version` flag 输出一致

### Added — agent-guide section 可用性 parametrize（P1）

- `tests/unit/test_agent_guide_sections.py` 加 parametrize 测试：
  `--list-sections` 输出的**每一个** slug，都验证 `--section <slug>` 能取回
  非空 markdown 且 `envelope.ok=True`。覆盖 15 个 section。

### Added — `--json` 错误路径不泄漏 traceback（P2）

- `tests/unit/test_error_envelope.py` 新增 5 个测试，覆盖 USAGE 系列错误：
  未识别子命令 / trace 缺参 / audit 错参 / mode 错参 / agent-guide --section 拼错。
  断言：JSON 模式下输出合法 envelope（schema_version=2 / ok=False / 含 hints），
  且 stdout/stderr **不含 Python traceback 标记**。

### Added — `proxyctl version` 子命令（P2）

新增 `version` 子命令作为 `--version` flag 的等价别名。`proxyctl version --json`
输出与 `proxyctl --version --json` 完全一致的 envelope（`cmd: "version"` /
`data.version` + `supported_features`）。agent 拿结构化版本号不再需要旁路。

### Added — `supported_features` 字段

- `lifecycle_dry_run: True` — 标记 0.4.2 起 start/stop/restart/restart-clean/recover
  全部支持 `--dry-run`
- `version_subcommand: True` — 标记 0.4.2 起有 `version` 子命令

### Test stats

- 总测试数 480 → 516（+36）。包括 9 个 lifecycle contract test、
  5 个 version 一致性、15 个 agent-guide section parametrize、5 个
  error envelope，外加现有静态测试自动覆盖新加 plan 函数。

### 复盘

dispatcher（cli.py 中 `_h_start` / `_h_stop` 等）调 `cmd_*` 时**不走** `_maybe_dry_run`
卫语句，未知 flag `--dry-run` 被 `_extract_global_flags` 剥离后**静默吃掉**，
导致 cmd_* 走正常路径真执行。0.4.0a1 引入 plan 契约时把焦点放在写操作命令的
helper 抽取上，**漏审了 lifecycle 4 个**——它们写得最早（v0.1.0 就有），
最后才需要 plan，反而被错过。

教训记录到 `_DRY_RUN_CMDS` 与 `COMMANDS_META.supports_dry_run` 必须双向同步，
且 contract test 应**自动覆盖**所有 `supports_dry_run=True` 的命令（未来工作）。

## [0.4.1] — 2026-05-18

> 修 cmd_discovery 在 Linux 平台 hardcode `engine_up=False` 的 bug。
> agent 探测 + 人类 banner 在 Linux 上现在正确反映 systemd 服务状态。

### Fixed

- **`cmd_discovery` 在非 macOS 平台正确反映引擎状态**（cli.py:1494）
  原代码：`engine_up = launchctl_running(backend.label) if IS_MACOS else False`
  Linux 用户裸 `proxyctl` 永远显示 `✗ engine=mihomo`，**即使 systemd 服务在跑**；
  JSON discovery envelope `data.engine.running` 永远 false，**agent 探测拿到错值**。
  修改为 `engine_up = service_running(backend)`，走平台分支（macOS launchctl /
  Linux systemctl --user is-active）。

### Added

- **`tests/integration/test_regression_0_4_0.py`** 新增 3 个回归测试：
  - `test_cmd_discovery_linux_uses_service_running` — JSON discovery envelope
    在 Linux + service_running=True 时 `data.engine.running` 必 True
  - `test_cmd_discovery_linux_banner_shows_check_when_running` — 人类 banner
    在 Linux + 引擎跑时显示 ✓
  - `test_cmd_discovery_linux_banner_shows_cross_when_stopped` — 反向验证

### Test stats

- 总测试数 477 → 480（+3 regression）。

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
