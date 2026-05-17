# LLMS.md

This repository's agent contract lives in [AGENTS.md](AGENTS.md).

For runtime usage of the `proxyctl` CLI after installation, run:

```bash
proxyctl agent-guide          # entrypoint markdown for LLMs
proxyctl commands --json      # all commands as machine-readable metadata
proxyctl commands --schema    # JSON Schema describing the above
PROXYCTL_AGENT=1 proxyctl ... # one-shot JSON + no-color + non-interactive
```
