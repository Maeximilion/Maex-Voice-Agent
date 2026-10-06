A gate has been passed: $ARGUMENTS

Workflow per `docs/15_README_STRATEGY.md`, section "Gate workflow":
1. Check in docs/00_PCF.md which criteria this gate has, and name evidence for each (eval report, measurement log, roleplay log). If evidence is missing, stop and say which.
1a. Ultrareview reminder per `CLAUDE.md` "Model Routing": if anything other than `.md` files changed since the last gate tag (`git diff --name-only $(git describe --tags --abbrev=0)..HEAD -- . ':!*.md'`; with no tag yet, treat it as changed), tell Maxi in one line that a gate is the point for `/code-review ultra` on the current branch (billed, started by Maxi only). Go on with step 2 without waiting unless Maxi says to wait.
2. docs/01_STATUS.md: gate to "passed" with date, link evidence, update next gate and next steps.
3. Update README.md per mandatory sections in docs/15: status, version, "What works", "Not yet", "Next steps". Current state, no promises.
4. Execute quick start from README on a clean checkout. Fix any deviations.
5. CHANGELOG.md: new section for version from commits since last tag.
6. Suggest commit message and tag, execute after confirmation.
