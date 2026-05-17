# Migration: 0.2.x → 0.3.0

0.3.0 is the **agent-friendly + clig.dev compliance** release. It contains
several breaking changes — by design, with **no compatibility window**.
External agents / scripts must update once, then the contract stabilises.

If you are a user who only invokes `proxyctl` interactively in a terminal,
nothing changes for you: prompts, exit codes, default `proxyctl status`
behavior are all preserved.

If you are an agent / RAG indexer / shell script that parses `proxyctl`
output, read below.

---

## What changed

### 1. JSON envelope: schema_version=1 → 2

Every `--json` command now emits a v2 envelope:

```json
{
  "schema_version": 2,                          // was 1
  "cmd": "status",
  "ok": true,
  "data": { ... },
  "error": null,
  "code": 0,
  "hints":    [],                               // NEW (was singular "hint": str|null)
  "warnings": [],                               // NEW (non-fatal warnings)
  "doc":      null,
  "meta": {                                     // NEW
    "ts":               "2026-05-17T08:30:00Z",
    "elapsed_ms":       12,
    "proxyctl_version": "0.3.0",
    "request_id":       "abc...uuid4..."
  }
}
```

**Breaking**:
- `schema_version` is now `2`. Hard `== 1` assertions break.
- `hint: string | null` is gone. Use `hints: string[]` instead (it's a list).
  If you used `hint`, swap to `hints[0]` (which may be empty array).

**Additive (won't break readers)**:
- `warnings`, `meta.ts`, `meta.elapsed_ms`, `meta.proxyctl_version`,
  `meta.request_id`.

### 2. `commands --json`: `side_effects` is now a list of enums

Old (free-form string):
```json
"side_effects": "config-write (only with apply)"
```

New (enum list + optional conditional):
```json
"side_effects":             [],
"conditional_side_effects": {"apply": ["config-write"]}
```

The enum is fixed:
`["process", "system", "config-write", "cache", "network-io"]`.

If your jq expression was `.side_effects`, it now needs to be
`.side_effects | join("+")` or check membership with `contains([...])`.

### 3. `commands --json`: new fields per command

- `supports_dry_run: bool` — whether the command accepts `--dry-run`.
- `conditional_side_effects: object` — per-subcommand effect map (e.g.
  `audit apply` writes config but `audit` alone does not).
- New: `proxyctl commands --schema` outputs a JSON Schema describing
  `commands --json` (so agents can validate the shape they parse).

### 4. New global flags

- `--dry-run` — supported by all write commands; outputs `data.plan`
  (list of `PlanStep`). PlanStep fields:
  `step, action, target, reversible, requires_sudo, side_effects, summary`.
- `--plain` — TSV output (no ANSI, no boxes). Currently `audit` and
  `check` support it. Mutually exclusive with `--json` (USAGE=2 if both).

### 5. New exit codes

- `9  TIMEOUT` — command timed out (e.g., bench / curl long-poll).
- `10 DEPENDENCY_MISSING` — required binary missing (mihomo / sing-box / scripts).

Old code `1` (GENERIC) remains for legacy paths; new error paths use the
semantic codes.

### 6. `proxyctl --version` → also accepts `--json`

```bash
proxyctl --version --json
# → { schema_version: 2, ok: true, data: {
#       version, schema_version, python, platform, default_backend,
#       supported_features: { envelope_v2: true, dry_run: true, ... }
#   } }
```

`supported_features` is the **feature-flag table** agents should consult
before invoking new behaviour. Keys are stable; values evolve per release.

### 7. `proxyctl help <cmd>` is now a real top-level command

Previously `<cmd> --help` was the only way. Now both work and they share
the same metadata-driven renderer.

### 8. Bare `proxyctl` (no args)

- **Human (TTY)**: prints a 4-line banner to **stderr** (engine + port +
  pointers to `agent-guide` / `explain` / `doctor`), then continues with
  the default `status` output on stdout. Existing scripts that pipe
  `proxyctl` somewhere see no change to stdout.
- **JSON / `PROXYCTL_AGENT=1`**: emits a *discovery envelope* with
  `cmd: ""`, `data.entrypoints` and `data.engine` — no longer the full
  status payload. Use `proxyctl status --json` if you wanted that.

### 9. New explain topics

`subscription`, `agent-protocol`, `locks`, `flags` are new. The existing
`next` field on each topic card was renamed `next_commands` (field still
emitted alongside the new name during 0.3 for tooling that scraped it,
but new code should consume `next_commands`).

### 10. `LOCKED(8)` error message now carries the lock path

Old hint:
> 稍后重试；或排查是否有挂死的 proxyctl 进程

New hints (list):
> 锁文件: /home/USER/.config/proxyctl/.lock.system
> 排查: lsof <path>  # 看谁持有
> 确认无 proxyctl 进程后可手动: rm <path>
> doc: proxyctl explain locks

### 11. Did-you-mean expanded

Misspelled subcommand-level enums (`proxyctl mode tunn`,
`proxyctl engine mihomoo`, `proxyctl daemon name stat`,
`proxyctl audit yesterday`, `proxyctl completion zsh-foo`) now return
USAGE(2) with a `hint` suggesting the closest valid value. Previously
some of these silently fell back to defaults.

### 12. Removed: `audit <not-a-number>` no longer falls back to 1

```bash
proxyctl audit yesterday        # 0.2: silently scans 1 day; 0.3: USAGE(2)
proxyctl audit 7                # unchanged
proxyctl audit apply            # unchanged
```

---

## Recommended adoption sequence

1. Run `proxyctl --version --json` from your agent. If it returns a v2
   envelope, you are on 0.3.
2. Read `supported_features` and gate behaviour on it (`dry_run`, `plain`,
   `commands_schema`, ...).
3. Update jq expressions that touched `.hint` → `.hints[0]` (or iterate).
4. Update jq expressions that touched `.data.commands[].side_effects` for
   the string-vs-list change.
5. For write operations you ran via agent, prefer `--dry-run --json` first
   and validate the `data.plan` before actually executing.

For background on the agent contract, see [AGENTS.md](AGENTS.md) and
`proxyctl agent-guide`.
