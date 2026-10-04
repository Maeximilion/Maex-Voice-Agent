# 15 – README Maintenance

The README is the shop window of the repo. It describes the current state for someone who sees the project for the first time. It is not a change log (that is `docs/01_STATUS.md`) and not a specification (those are `docs/02` to `docs/14`).

## When the README is updated

| Trigger | What changes | Who |
|---|---|---|
| Gate passed (G0 to G5) | Status, version, "What works", next steps | Claude Code via `/gate` |
| New dependency or new service (Docker image, vendor, library) | Requirements, Quick Start | Claude Code in the same task |
| Change to the Quick Start (new command, new environment variable) | Quick Start, Configuration | Claude Code in the same task |
| Known issue that users will run into | Known Issues | Claude Code via `/bug` or `/done` |
| New doc file under `docs/` | Documentation | Claude Code in the same task |

Not with every commit. Nobody reads a README that changes daily.

## Versioning

The version in the README follows the gates. Semantic Versioning, tag in the repo.

| Gate | Version | Meaning |
|---|---|---|
| Repo created | 0.0.x | Skeleton, nothing usable |
| G0 | 0.1.0 | Foundation: vendor, legal, budget settled |
| G1 | 0.2.0 | Reservation runs on a test number |
| G2 | 0.3.0 | Pickup runs, evals on target |
| G3 | 0.5.0 | Delivery runs, functionally complete |
| G4 | 0.8.0 | Shadow measurement passed, ready for real calls |
| G5 | 1.0.0 | Overflow operation with real customers passed |
| G6 ongoing | 1.x | Main line answered by the agent, operations |

Alternative from the early planning: 1.0.0 already after G3. The argument against it: before G5 no real customer has talked to the system. A 1.0 that never ran in production is not one.

Between gates: patch versions (0.2.1, 0.2.2) for fixes that go to `main`.

## Mandatory sections

The README always contains, in this order:

1. Title and one paragraph: what the project does and for whom
2. Status: current version, current stage, next gate, date
3. What works / what does not (one list each, honest)
4. Requirements
5. Quick Start (copyable, tested)
6. Configuration (the most important environment variables, pointer to `.env.example`)
7. Project Structure (short form, pointer to `docs/11_MODULE.md`)
8. Development (tests, lint, evals, slash commands)
9. Documentation (table of the `docs/` files)
10. Known Issues
11. License and Contact

What does not belong in it: marketing text, emojis, feature promises, screenshots of mockups, change history.

## Gate workflow

```text
/gate G1
1. docs/01_STATUS.md: gate table to "passed" with the date, link the evidence (eval report, log)
2. README.md: update status, version, "What works" and "Next steps" according to this file
3. Run the Quick Start once on a clean checkout. What does not work gets fixed, not commented.
4. CHANGELOG.md: section for the new version from the commits since the last tag
5. Commit: docs: README and status for <gate>, version <x.y.z>
6. Tag: v<x.y.z>
```

## Style

- English, sentences instead of keyword lists, no exclamation marks
- Present tense and current state: "The API answers on /health", not "will answer"
- Commands in code blocks, exactly as they are typed
- If something does not work, it is listed under "Known Issues", not in a subordinate clause
- No emojis, no badges that measure nothing
