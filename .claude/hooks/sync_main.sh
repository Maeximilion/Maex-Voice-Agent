#!/usr/bin/env bash
# SessionStart hook: bring a clean local main up to date before work starts.
# Silent on success, one warning line on failure, and never blocks the session.
git fetch --prune --quiet 2>/dev/null
if [ "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" = "main" ] && [ -z "$(git status --porcelain 2>/dev/null)" ]; then
  git pull --ff-only --quiet 2>/dev/null || echo "SessionStart sync: git pull --ff-only failed, check git status"
fi
exit 0
