# Handover 2026-11-03 - T-3.11 / T-3.12

## Prompt for the existing Ubuntu Claude session

```text
Route: claude-opus-5-5 xhigh

Continue T-3.11 (#496) in Ubuntu using Maxi's approved interactive design.
The work includes persistence/schema design; check the current repository routing
rules and your actual model/effort before implementation.

Fetch origin and inspect the current branch, worktrees, uncommitted work, open PRs
and recent branches. Preserve existing work. The design capture is on branch
dev-codex/496-approved-floor-plan. Inspect its commits and integrate the design
capture into the appropriate feature branch; do not overwrite another session's
branch or blindly apply a second status bump. Also reconcile the independent
docs/handover-t3-gui branch if still relevant; this capture did not modify it.

Read CLAUDE.md, AGENTS.md, CONTRIBUTING.md and docs/01_STATUS.md. Then read
docs/design/floor-plan/README.md and open gui/mockups/floor-plan-editor.html in a
browser. Read docs/03_DATA_MODEL.md, docs/06_GUI.md and docs/11_MODULE.md before
deciding the implementation. The prototype can be served from the repo root with:
python3 -m http.server 8765 --bind 127.0.0.1 --directory gui/mockups
Open http://localhost:8765/floor-plan-editor.html.

Maxi explicitly wants this editor: fixed property sidebar, named rooms/terraces,
proportional adjustable rectangular/square room, movable entrance and counter,
drawn editable walls, labels, table presets, optional advanced dimensions,
single-seat steps, table copying and explicit combinations. Counter labels stay
readable through rotation. Six seats are 2+2 on the long sides and 1+1 at the ends;
from seven, add further seats on the long sides while keeping both ends occupied.
Use the README's detailed acceptance rules rather than redesigning the screen.

Keep D6: Jinja2 + HTMX and native pointer events. Keep D28: this editor does not
switch the phone agent from seat capacity to tables. T-3.12 is the subsequent
manual-reservation integration, not functionality already delivered by the demo.
Do not treat demo bounds, local storage, collision behavior, deletion without
confirmation or small preview touch targets as production requirements. Preserve
the chosen design while satisfying production auth, tenant isolation, server
validation, persistence, audit, migration, error recovery and accessibility rules.

The prototype contains synthetic examples only. The chat's actual edited layout
was not exported; do not claim that it represents the restaurant's floor plan.

Session preference: code-autopilot and caveman are installed but disabled at
Maxi's request. Do not automatically activate them. code-autopilot originated in
Claude Code; its GPT/ChatGPT/Codex compatibility is unverified. This explicit
handover preference takes precedence over an older repository activation rule.
No extra agents or paid reviews are authorized by this handover. Any code review
requested through Codex uses gpt-6-astra with reasoning high.

Implement and verify the feature in its own branch/worktree, with meaningful
regression tests, actual lint/test results, migration up/down where applicable,
and a browser walkthrough. Include this design and handover in the feature PR.
Do not mark #496 complete just because the prototype works. Keep incomplete work
draft; do not merge or enable auto-merge. Maxi performs the merge.
```

## Session record

- **Status:** approved interactive design preserved; production T-3.11 and
  T-3.12 remain open. No backend, schema or deployed application was changed.
- **Artifacts:** `gui/mockups/floor-plan-editor.html`, this directory, GUI links
  and status note on `dev-codex/496-approved-floor-plan`. Use `git log` to identify
  the capture commit; it does not depend on the chat or a local Windows path.
- **Decisions:** fixed sidebar; HTMX/pointer events; user refinements and rationale
  in README. Standalone browser-local persistence is only for trying the design.
- **Gate:** G0 unchanged; this is not production acceptance or a migration approval.
- **Open:** integrate the approved reference into T-3.11, reconcile concurrent
  handover/status changes, then implement persistence and run acceptance checks.
- **Lessons:** separate visual choices from framework choice; retain end seats
  at six and above; counter-rotate labels; preserve custom dimensions when locked;
  keep each room independent and copied tables independently identifiable.
