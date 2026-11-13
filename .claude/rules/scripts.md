---
paths:
  - "scripts/**"
  - ".claude/commands/**"
  - ".claude/hooks/**"
---

# Shell in scripts

Convention of `CLAUDE.md` section 8 for scripts, commands and hooks (moved here by #695 Loop D PR 4). It loads when a file under `scripts/`, `.claude/commands/` or `.claude/hooks/` is read or edited.

- New shell longer than a single command lives in `scripts/` with a test (Maxi, 2026-10-22); docs and `.claude/commands/` call it and do not embed it. Shell already embedded in a command stays valid until that command is next reworked.
