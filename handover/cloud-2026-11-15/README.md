# Cloud session 15.11.2026: work to pick up locally

This folder is a carrier, not part of the product. **Do not merge this branch.** Its only job is to move
the work of one cloud session (which may push to one branch only) to the local Opus session that triages,
decides and ships. Delete the folder (or drop the branch) once the work is taken over.

Base of everything: `main` at `69ef3678`. Nothing here was merged, nothing opened a PR, nothing was posted
on GitHub.

## 1. Take the branches over

```bash
cd ~/Maex-Voice-Agent
git fetch origin ccr-8ada70c0-3yo967
git show origin/ccr-8ada70c0-3yo967:handover/cloud-2026-11-15/branches.bundle > /tmp/cloud.bundle
git bundle verify /tmp/cloud.bundle
git fetch /tmp/cloud.bundle 'refs/heads/*:refs/heads/cloud/*'
git branch --list 'cloud/*'
```

Each `cloud/<name>` branch sits on `69ef3678` and holds one coherent change (commits: `commits.md`).
Rename on take-over (`git branch -m cloud/fix/764-allergen-guard fix/764-allergen-guard`), merge
`origin/main` in, run CI and the evals, then the usual `/done` and `/ship`. Every commit message was
written without attribution lines; check with `git log -1 --format=%B` anyway.

## 2. What is where

- `review764/REPORT.md`: the batch review of the 14 open lines of ledger #764 (`/code-review max`
  level: four area agents, a cross-cutting wave, skeptics). Decisions, issue drafts and the ledger tick
  commands are in it. Raw notes: `area-*.md`, `cross.md`, verdicts `skeptic-*.md`, red probes `probes-*`.
- `review207/`: a second batch over 15 fix PRs of the #546 batch that had no deep review (#712 #713 #720
  #721 #722 #724 #725 #726 #728 #729 #730 #731 #732 #734 #735), for the rolling review #207. Notes,
  verdicts, probes, and `fable-consult.md` (the redesign consult for the menu parser, see below).
- `build/`: one report per build branch, plus `status-row-decisions.md` (row-by-row archive proposals
  for `docs/01_STATUS.md`, #562).
- `SUMMARY.md`: the overview for Maxi with every open decision as a closed question.

## 3. Sandbox limits that apply to every result here

Python 3.13 (CI uses 3.12: `audioop` is missing, so `test_telephony_voice.py` and the speech adapter
tests error at import here), no Docker (no drills, no compose), local Postgres 16. Baseline full suite on
`main`: 9422 passed, 1 failed (`test_scripts_db_role.py`, local role setup), 13 import errors (audioop).
