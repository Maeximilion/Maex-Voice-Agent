---
paths:
  - "api/tests/**"
  - "evals/**"
---

# German test input

Convention of `CLAUDE.md` section 8 for tests and eval cases (moved here by #695 Loop D PR 4). It loads when a file under `api/tests/` or `evals/` is read or edited.

- German is product content in caller sentences in `evals/` and in caller sentences as test input in `api/tests/` for code that parses German speech (Maxi, 06.10.2026: escalation keywords, the yes detector, number words and the stand-in model only react to German, an English sentence would test nothing). Test names, docstrings and comments around that input stay English.
- The assertions on what the team reads on the tablet (labels, messages and banners of `api/gui/`) stay German like that text (`.claude/rules/gui.md`).
