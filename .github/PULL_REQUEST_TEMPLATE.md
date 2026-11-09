## What Changed

<!-- Brief and factual, one to three points. Use task checkboxes when separate
deliverables need tracking. Remaining work is allowed as an open box only when an
issue tracks it, written `- [ ] #N what remains`; GitHub ticks it when the issue closes. -->

## Why

<!-- Which problem does this solve, or which work package does it implement. -->

## How Tested

<!-- Tests, latency measurement, manual run. What actually ran, not what should run.
Distinguish local results from CI and name the tested revision. -->

<!-- Keep only the checklist items that matter for this PR and delete the others:
no hot-path line without a tool change, no migration line without a schema change.
Tick an item only after it is verified, with the evidence above. A PR with an
applicable item still open is not merged. After the merge no box stays open except
`- [ ] #N` remaining work. Update these states after changes and CI results. -->

- [ ] `make lint` clean
- [ ] `make test` green
- [ ] For hot-path tools: response time measured, under 300 ms with local database
- [ ] For schema changes: migration up and down tested
- [ ] Affected documentation and `docs/01_STATUS.md` synced
- [ ] No credentials, no real operations or customer data in diff

## Issue

Closes #
