#!/usr/bin/env bash
# SessionStart hook: bring a clean local main up to date before work starts.
# Silent on success, one warning line on failure, and never blocks the session.
# Git runs against the repository that holds this script, not the current directory:
# after Claude changes directory the working directory no longer is the project.
root="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)" || exit 0
git -C "$root" rev-parse --git-dir >/dev/null 2>&1 || exit 0
warn() { echo "SessionStart sync: $1, check git status"; }

if ! git -C "$root" fetch --prune --quiet 2>/dev/null; then
  warn "git fetch failed"
  exit 0
fi
if [ "$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null)" = "main" ] \
  && [ -z "$(git -C "$root" status --porcelain 2>/dev/null)" ]; then
  git -C "$root" pull --ff-only --quiet 2>/dev/null || warn "git pull --ff-only failed"
fi
exit 0
