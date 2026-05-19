# AGENTS.md

This file is the **repository-level contract** for coding agents (Cursor /
Aider / OpenHands / Continue / Claude Code etc.) working *inside* the
proxyctl source tree.

For **runtime usage** (how to invoke `proxyctl` on a user's machine after
installation), run `proxyctl agent-guide` — it prints a runtime-dynamic
markdown with the current engine, ports, file paths, and a decision tree.

---

## 5-second decision tree

```
Question                                  → Command
"How do I use proxyctl as an agent?"        → proxyctl agent-guide
"Where do I change X (rules/nodes/...)?"    → proxyctl explain
"All commands as machine-readable JSON?"    → proxyctl commands --json
"Commands JSON Schema (field semantics)?"   → proxyctl commands --schema
"Is the system healthy right now?"          → proxyctl doctor --json
"What does this command do without running it?" → proxyctl <write-cmd> --dry-run --json
"Help on one command?"                      → proxyctl help <cmd>
"Full capability list (no execution)?"      → proxyctl --version --json
```

Set `PROXYCTL_AGENT=1` to force `--json` + no color + non-interactive
contract for the whole invocation.

---

## What proxyctl is

A macOS-first (Linux partial) CLI that manages the **lifecycle** of a local
proxy engine (mihomo / sing-box):

- start / stop / restart / restart-clean (engine + DNS + system proxy)
- status / doctor / check / trace / audit / bench (diagnose)
- mode tun|proxy / engine mihomo|singbox / config set ... (change)
- fix / recover (heal)
- dns-lock / dns-unlock / daemon (auxiliary processes)

**It does not edit user rules, nodes, or fetch subscriptions.** Those live
in the engine's own config file (`~/.config/mihomo/config.yaml` etc.) or
user scripts. proxyctl points agents to where to edit (`proxyctl explain
rules`); it does not proxy the edit itself.

**Since v0.4.4, proxyctl reads (but never writes) a subscription status
contract file** at `~/.config/proxyctl/subscription.json` and surfaces it
through `proxyctl status --json | jq .data.subscription` and the
`SUBSCRIPTION` section of `proxyctl status`. The user's subscription
refresh script writes this file; proxyctl performs zero network calls. See
`proxyctl explain subscription` for the schema.

---

## Doctor suggestions (v0.5.0+)

`proxyctl doctor` returns two **independent** signals:

1. **Score** — 5 boolean health checks (`engine_up / port_listen / dns_ok /
   system_proxy_ok / connectivity_ok`). Controls `exit code` and `data.healthy`.
2. **Suggestions** (`data.suggestions[]`) — structured "things worth doing"
   that don't constitute failures. **Never affect exit code.**

### Suggestion schema v1

```jsonc
{
  "id":              "autostart.version_mismatch",  // stable enum "<area>.<situation>"
  "severity":        "info | advisory | warn",      // no "error" — errors live in envelope.hints[]
  "actor":           "user | agent | cron | engine", // who fixes — agent decides "DIY vs ask user"
  "title":           "...",                          // one-line human text
  "evidence":        { /* structured facts */ },     // agent does NOT regex title
  "inspect_command": "proxyctl status --json | ...", // read-only diagnosis, runnable
  "fix_command":     null,                           // write-op, may need sudo
  "auto_fixable":    false,                          // whether agent may run fix_command unattended
  "doc":             "suggestion:autostart.version_mismatch",  // `proxyctl explain <doc>` always works
  "fingerprint":     "abc123def456",                 // sha1(id)[:12] — stable across calls
  "first_seen":      "2026-05-19T10:23:00Z",         // persisted in ~/.cache/proxyctl/suggestions_state.json
  "since":           "0.5.0"
}
```

### Contract guarantees for agents

- **Severity is three-tier**, no `error` / `critical`. Errors go through
  `envelope.hints[]` with `ok=false` and non-zero exit code; suggestions are
  always advisory.
- **Sort order is fixed**: `severity desc (warn > advisory > info), id asc`.
  Agents can stable-diff two `doctor --json` outputs without sorting.
- **`fingerprint` is the deduplication key.** Same id across calls returns
  the same fingerprint regardless of evidence fluctuation (e.g. traffic
  73% → 88%). Track "this problem is still unresolved" by fingerprint.
- **`first_seen` is monotonic** while the fingerprint keeps appearing. Use
  it to escalate after N hours of an unresolved suggestion.
- **`doc` is always `proxyctl explain <doc>`-routable.** CI enforces every
  suggestion id has a corresponding explain topic.
- **Adding suggestions is non-breaking.** New `severity` / `actor` enum
  values, new fields → bumps `supported_features.doctor_suggestions_v<N>`
  to a new bool key. v1 keys never disappear.
- **Schema additions to `data.suggestions[].*`** never remove fields;
  agents using only the v1 keys are forward-compatible.

### Severity vs actor — decision matrix for agents

| severity   | actor   | Suggested agent behavior                           |
|------------|---------|----------------------------------------------------|
| `warn`     | `user`  | Surface to the user, suggest the fix              |
| `warn`     | `cron`  | Surface; recommend user check the cron job        |
| `warn`     | `agent` | Take action if `auto_fixable=true`, else surface  |
| `advisory` | any     | Mention if user asked "anything to improve?"      |
| `info`     | any     | Don't volunteer; surface only if user queries     |

### Reading suggestions

```bash
# All suggestions including info-level
proxyctl doctor --json | jq '.data.suggestions[]'

# Only actionable (warn + advisory)
proxyctl doctor --json | jq '.data.suggestions[] | select(.severity != "info")'

# Specific group (autostart-related)
proxyctl doctor --json | jq '.data.suggestions[] | select(.id | startswith("autostart."))'

# Per-id detail
proxyctl explain suggestion:autostart.version_mismatch
```

### Coverage (v0.5.0 — 21 rules)

- **Subscription (7)**: expired / expiring_soon / traffic_high|warn|exhausted /
  last_fetch_error / stale / missing
- **Autostart (8)**: unit_missing / binary_missing / binary_mismatch /
  version_mismatch / config_dir_mismatch / placeholder_unrendered /
  disabled / flapping
- **Security (3)**: controller.{empty,weak}_secret / public_bind
- **Engine/Data (2)**: engine.outdated (reads
  `~/.cache/proxyctl/known_versions.json` contract file) /
  data.geo_stale (geoip.dat / geosite.dat mtime > 30d)
- **Proxy groups (1)**: proxy_group.mostly_dead — polls local mihomo
  `/proxies` API (0 external network, 0.5s timeout). Each affected group
  emits its **own** suggestion; fingerprint includes `evidence.group_name`
  so agents can track multiple dead groups independently.

### Doctor flags (v0.5.0)

| Flag                   | Use case                                            |
|------------------------|-----------------------------------------------------|
| `--json`               | Machine-readable envelope (always recommend)        |
| `--no-suggest`         | Disable suggestions entirely (user-facing)          |
| `--suggest-only`       | Skip 5-bool score probes (curl ~2-5s); ~0.18s total |
| `--since <version>`    | Hide rules `since > <version>` (CI migration aid)   |
| `--quiet`              | Skip the human suggestion block (JSON unaffected)   |

Suggestions can also be silenced per-id via
`~/.config/proxyctl/suggestions.ignore` (one id/fingerprint per line,
`#`-prefixed comments). Env override `PROXYCTL_SUGGEST_IGNORE_PATH`.

### Fixing autostart mismatches (v0.5.0+)

When doctor reports `autostart.binary_mismatch` /
`autostart.version_mismatch` / `autostart.config_dir_mismatch`, the
**`proxyctl autostart sync`** write-command syncs the plist/unit to the
current PATH binary + config_dir in one shot:

```bash
proxyctl autostart inspect              # show current state
proxyctl autostart sync --dry-run       # preview PlanStep[]
proxyctl autostart sync --dry-run --json
proxyctl autostart sync                 # actually apply (needs sudo on macOS)
```

The command preserves user customizations (KeepAlive, EnvironmentVariables,
StandardOutPath, etc.). On Linux, if the unit's `ExecStart=` line is
missing entirely, sync **refuses** to overwrite (avoid clobbering a
heavily-customized service).

`side_effects = ["process", "system", "config-write"]` when subcmd=sync;
`[]` for inspect.

---

## Repository layout

```
src/proxyctl/
  cli.py         entry point, DISPATCH, --help, dry-run helpers
  _io.py         exit codes, envelope v2, fail(), with_lock(), TSV
  explain.py     COMMANDS_META, explain topics, agent-guide markdown,
                 doctor, commands --json, commands --schema
  check.py       full health check (4 stages) + bench
  audit.py       log scan → candidate domains
  trace.py       per-domain rule prediction + connectivity
  status.py      status panel
  completion.py  bash / zsh / fish completion generation
  engine/        Backend abstraction + mihomo / sing-box implementations
  core/plugin.py plugin host
  builtin_plugins/  shipped plugin packs
tests/unit/      pytest, must stay green
tests/integration/
man/proxyctl.1   groff man page (keep in sync when adding commands)
config.yaml.example
launchdaemons/   macOS launchd plists
systemd/         Linux systemd units
```

---

## Build / test (when editing this repo)

```bash
uv sync --group dev            # install runtime + test deps
uv run pytest -q               # all 400+ tests must pass
uv run proxyctl --help         # smoke check from source
```

**Do not** run write commands from tests or sandboxed agent sessions:
`proxyctl start | stop | fix | mode | engine | daemon | dns-lock | dns-unlock`
all touch the user's macOS launchd / DNS state. Use `--dry-run --json` to
inspect what they would do.

---

## Conventions when editing

- **New subcommand**: add an entry to `COMMANDS_META` in `explain.py` *and*
  register a handler in `cli.DISPATCH`. `tests/unit/test_dispatch_coverage.py`
  fails if you forget either side.
- **Errors**: use `_io.fail(msg, hint=, doc=, code=)`. Never `print(...) +
  sys.exit(...)`. `code` must be one of the exit codes in `_io.py`. The
  test `test_no_bare_sys_exit.py` enforces this.
- **Writes**: wrap in `_exec_with_lock("system"|"config"|"daemon", ...)`.
  Concurrent writes return `LockedError` → exit 8.
- **Non-interactive**: never call `input()` or block waiting for stdin.
- **Side effects**: declare them in `COMMANDS_META.side_effects` (list of
  the 5 enum values). Conditional ones go in `conditional_side_effects`.
- **JSON envelope**: schema v2. Use `_io.envelope(cmd, data=, ...)`. Don't
  hand-build dicts that mimic envelope shape.
- **TSV (`--plain`)**: use `_io.emit_tsv(rows, cols)`. No ANSI, no boxes.
- **Dry-run**: add `_plan_<cmd>` (pure function returning list[PlanStep])
  and `_maybe_dry_run(name, plan_fn)` before `_exec_with_lock` in the handler.

---

## Commit / PR

- Conventional-ish: `feat:` / `fix:` / `refactor:` / `docs:` / `test:`.
- Update `CHANGELOG.md` under the `## [Unreleased]` section, then move
  it to the new version section when the release is cut.
- Keep `man/proxyctl.1` in sync when adding/renaming commands or flags.
- Bumping `pyproject.toml` version also requires bumping `cli.VERSION`.

---

## CHANGELOG / Release notes style

**`CHANGELOG.md` 直接驱动 GitHub release notes**：`.github/workflows/release.yml`
的 `Extract release notes from CHANGELOG.md` 步骤会抽对应版本 section 写
`gh release create --notes-file`。**CHANGELOG section 质量 = release notes 质量**。
Release notes 是写给**用户**看的，不是写给维护者复盘用的。

### 必须 — 结果导向

每个版本段按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)
子分类组织：

| Subhead | 内容 |
|---|---|
| `### Added` | 新增的命令 / flag / 字段 / 文件 / 配置项 |
| `### Changed` | 已有行为变化（用户能感知到的） |
| `### Fixed` | 修了什么用户感知 bug（**直接描述症状 + 修复结果**，不写"哥发现的过程"）|
| `### Removed` | 删了什么（破坏性，配 `### Breaking`）|
| `### Breaking` | 破坏性变更清单 + 迁移指引 |
| `### Compatibility` | 向后兼容承诺、schema 不变性、老消费者影响 |

每条 bullet 用**用户视角**："X 命令现在支持 Y" / "Z 字段类型从 A 改为 B" /
"`feature` 在 `condition` 时不再 error"。

### 禁止 — 过程叙事

CHANGELOG **不写**以下内容（这些归 commit message / PR description /
源码 docstring，**不进 release notes**）：

- **反馈引用**：`> "哥 2026-05-19 跑 status 时一眼看到 ..."` 这类 blockquote
- **场景叙事**：`实地跑 X 在哥本机 ...` / `调试 TUIC 栽过的坑 ...`
- **过程追溯**：`v0.4.7 引入的 X 在 v0.5.0 出问题 ... 修复过程 ...`
- **未来设想**：`未来若再有人引入 ... CI 立即抓住`
- **元复盘**：`Backstory` / `复盘` / `Audit 结果总结` / `Meta — 元模式` /
  `Known tech debt` / `Test stats` / `红线` 收尾段
- **审查过程引用**：`UX review 指出 80% 太晚 ...`
- **维护者内部讨论**：`P0 / P1 / P2 优先级` / `留 backlog`

写完一段 CHANGELOG 自检：删掉它，用户还能不能知道这个版本**做了什么、能用什么、要注意什么**？如果能 → 保留。如果不能 → 没必要写。

### 参考好样本

- v0.5.1 / v0.5.0 / v0.4.7 / v0.4.4 entries —— 结果导向典范
- 反面教材 (重写前的版本) 可看 `git log` `docs(CHANGELOG): 重写 ... 结果导向`
  系列 commit 的 `git show <sha>` diff

### `release.yml` 行为

- `push tag v*.*.*` 触发 → CI 抽 CHANGELOG 中对应 `## [<version>]` section →
  `gh release create --notes-file .release-notes.md`
- 找不到对应 section 直接 `::error::` + exit 1（强约束：tag 前必须更新 CHANGELOG）
- 历史 release notes 漏写补法：`gh release edit v<x.y.z> --notes-file <抽出的内容>`

---

## Where to go next

| Need                                    | Where                                |
|-----------------------------------------|--------------------------------------|
| Runtime contract for an agent           | `proxyctl agent-guide`               |
| All command metadata                    | `proxyctl commands --json`           |
| Schema of `commands --json`             | `proxyctl commands --schema`         |
| "Where do I change X?" map              | `proxyctl explain`                   |
| Topic detail (rules / nodes / dns / …)  | `proxyctl explain <topic>`           |
| Exit-code semantics                     | `proxyctl explain exit-codes`        |
| Locks (LOCKED=8 troubleshooting)        | `proxyctl explain locks`             |
| Migration from 0.2.x to 0.3.0           | [MIGRATION-0.3.md](MIGRATION-0.3.md) |

This file is the **repo contract** (stable across versions). Runtime
behavior is described by `proxyctl agent-guide` (dynamic per release).
