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
# main is checked out in at most one worktree, often not the one this session runs in.
main_dir="$(git -C "$root" worktree list --porcelain 2>/dev/null | awk '
  /^worktree /{w=substr($0,10)}
  /^branch refs\/heads\/main$/{print w; exit}')"
[ -n "$main_dir" ] || exit 0
if [ -z "$(git -C "$main_dir" status --porcelain 2>/dev/null)" ]; then
  git -C "$main_dir" pull --ff-only --quiet 2>/dev/null || warn "git pull --ff-only failed"
else
  behind="$(git -C "$main_dir" rev-list --count main..origin/main 2>/dev/null)"
  [ "${behind:-0}" -gt 0 ] \
    && warn "main in $main_dir has uncommitted changes and is $behind commits behind origin/main, not synced"
fi
exit 0
