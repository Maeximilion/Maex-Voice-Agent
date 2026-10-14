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
- Badges in the palette below (`labelColor=17102B`, value colour `9D7BFF` or `E8FF2E`); the CI badge stays GitHub's own, because it is the only one that works on a private repository

## Assets and design

The README carries images in `.github/assets/`. All of them are written by `scripts/readme_assets.py` from data in the repository; none is drawn by hand, and none shows a number that is not in a file with a date.

### The concept: "Night Service"

The product's world is a restaurant phone ringing during evening service, answered by a machine that never guesses. Four motifs come from that world and nowhere else:

- **Midnight Purple** is the line after dark: every image sits on its own midnight card, so it looks the same in GitHub's light and dark theme.
- **Neon Yellow** is the lit sign of a restaurant that picks up. It marks what is built, passed or confirmed, and the headline number of each image.
- **The order slip** is what the agent produces. Stat cards are slips: perforated top edge, monospace print, a dated header, line items.
- **The voice waveform** is the conversation. It is the crossbar of the logo, the divider between sections, the line under the banner and the track of the gates.

The logo is **MÆX**: the ligature Æ, in which the A and the E share one stroke, the way the AI and the code share one call. The A half is yellow (the AI understands), the E half violet (the code decides), the crossbar a waveform. The concept is `LOGO_STYLE = "split"` in the script; `python scripts/readme_assets.py concepts /tmp/maex-logo-concepts.svg` draws all four concepts side by side.

### Tokens

| Token | Value | Use |
|---|---|---|
| `bg` | `#0D0819` | Card background (midnight) |
| `surface` | `#17102B` | Slips and nodes |
| `grid` | `#2B2148` | Grid lines, slip borders, the dot pattern |
| `ink` | `#F1ECFF` | Text |
| `muted` | `#A89CCB` | Labels, dates, secondary text |
| `neon` | `#E8FF2E` | Passed, built, done, the headline number |
| `violet` | `#9D7BFF` | In progress, the second series, the spoken reply, the escalation path |
| `coral` | `#FF5277` | Crashed, blocked, a hard violation, a no-go; never anything else |
| `plum` | `#76699F` | Not passed, open, planned |

Checked with the colour validator of the `dataviz` skill against `surface` (14.10.2026): every adjacent pair passes the colour-blind separation and the normal-vision floor, and every colour has at least 3:1 contrast. Neon yellow sits above the lightness band on purpose; it is the brand, and every mark in it carries a text label, so identity never rests on colour alone. Type is the system monospace stack; nothing loads a web font or a script.

### The images

| File | Shows | Source |
|---|---|---|
| `banner.svg` | Logo, one line, the state slip: latest real-model eval, version, stage, test functions in `api/tests` (rounded down to the hundred; parametrized cases make the run larger), next gate | all sources below |
| `call-flow.svg` | One call in two turns as the code runs it: words in through access, Asterisk and the ear; the core asks the LLM and runs one tool per hop against Postgres (the LLM has no line to a tool or the database); the read-back out through the mouth; `confirm` only after the yes, then the spoken reply and the order on the tablet at the same time; the slip after the team's approval (primary mode: at once); escalation to the team phone or, after hours and with the caller's number, a callback on the tablet; a failure of the service `voice` rings the team phone. Each node is built, in progress or planned from its tasks; the tests in `api/tests/test_readme_assets.py` hold the story to the code | `FLOW_NODES`, `FLOW_LINKS`, `FLOW_STORY` in the script, `docs/07_WORKPACKAGES.md` |
| `call-flow-static.svg` | The same without motion | as above |
| `progress.svg` | Tasks done, one bar per block, the gates | `docs/07_WORKPACKAGES.md`, `docs/01_STATUS.md` |
| `eval-latest.svg` | The latest real-model run per suite | `docs/metrics/eval_history.csv` |
| `eval-trend.svg` | Pass rate per suite over time, against the target of 99 % | `docs/metrics/eval_history.csv` |
| `voice-bench.svg` | Voice layer measurements with limit and verdict | `docs/metrics/voice_history.csv` |
| `rules.svg` | The six hard rules of `CLAUDE.md` §2 | `HARD_RULES` in the script |
| `logo-mark.svg`, `logo-wordmark.svg`, `divider.svg` | Logo and divider | the script |
| `social-preview.png`, `logo-mark-512.png` | Link preview and avatar, uploaded by hand in the repository settings. They carry no number, because CI cannot check a PNG | `render --png` |

### Keeping them current

- Every date in an image comes from the data (the run, the last finished task), never from the day of rendering, so the same data always gives the same file.
- CI runs `python scripts/readme_assets.py render --check` and fails when a committed image no longer matches its data. The two PNGs cannot be drawn in CI: `render --png` records the digest of their source SVGs in `.github/assets/png-sources.json`, and the check fails when a source SVG changed without new PNGs. The fix is always `make readme-assets` and a commit of `.github/assets/`; on a merge conflict in a generated file, take either side and render again.
- `/done` renders after the status update; `/gate` also records the latest runs and renders the PNGs.
- A real-model eval run is recorded right after it with `make eval-record`, which takes the newest report (`docs/08_EVALS.md` §4). A voice measurement is recorded like this, with its row in `docs/20_VOICE_LAYER.md` §11:

  ```bash
  python scripts/readme_assets.py add-voice 2026-10-14 T-1.11 "Outage drills end at the team phone" 15 "of 15 points" go --note "Asterisk 22.11.0"
  ```
- **A new component a call passes through gets its node in `FLOW_NODES` and its lines in `FLOW_LINKS` in the same PR that builds it**, with the task IDs that build it, and a step in `FLOW_STORY` where a call passes it; it then turns from planned to built on its own when those tasks are done. A new measured quantity gets a column or a chart in the same PR, never a number typed into the README.
- What an image may not show: a promise, a mockup, a number without a date, a provider name outside the voice layer's own row.
