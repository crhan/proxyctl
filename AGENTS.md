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
- Update `CHANGELOG.md` under the `## [Unreleased]` section, with the
  `### Breaking` / `### Agent-facing` / `### Added` / `### Fixed` subheads
  used in 0.3.0.
- Keep `man/proxyctl.1` in sync when adding/renaming commands or flags.
- Bumping `pyproject.toml` version also requires bumping `cli.VERSION`.

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
