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
# The main folder left on a finished branch goes back to main, or its files stay at the
# old state while every ref is current (12.10.2026: a kit merged that day was "not
# there"). Finished means: nothing uncommitted, the remote branch is gone, and its work
# is in origin/main. Only that folder; session worktrees are tidied by /start.
# In main is a branch whose commits are there, or whose changes are: after a squash
# merge the commits never land, but merging the branch into origin/main then changes
# nothing. A git without `merge-tree --write-tree` (before 2.38) answers no.
in_main() {
  git -C "$primary" merge-base --is-ancestor "$1" origin/main 2>/dev/null && return 0
  merged="$(git -C "$primary" merge-tree --write-tree origin/main "$1" 2>/dev/null | head -n 1)"
  [ -n "$merged" ] && [ "$merged" = "$(git -C "$primary" rev-parse 'origin/main^{tree}' 2>/dev/null)" ]
}
primary="$(git -C "$root" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print substr($0,10); exit}')"
branch="$(git -C "$primary" branch --show-current 2>/dev/null)"
if [ -n "$branch" ] && [ "$branch" != main ] \
  && [ -z "$(git -C "$primary" status --porcelain 2>/dev/null)" ] \
  && [ "$(git -C "$primary" for-each-ref --format='%(upstream:track)' "refs/heads/$branch")" = "[gone]" ] \
  && in_main "$branch"; then
  # Refused when main is checked out in another worktree: then that one is synced below.
  git -C "$primary" switch --quiet main 2>/dev/null || true
fi

# main is checked out in at most one worktree, often not the one this session runs in.
main_dir="$(git -C "$root" worktree list --porcelain 2>/dev/null | awk '
  /^worktree /{w=substr($0,10)}
  /^branch refs\/heads\/main$/{print w; exit}')"
if [ -z "$main_dir" ]; then
  # Checked out nowhere: move the branch itself, so the next task starts from today's main.
  git -C "$root" fetch --quiet origin main:main 2>/dev/null \
    || warn "local main has commits origin/main lacks and was not updated"
  exit 0
fi
if [ -z "$(git -C "$main_dir" status --porcelain 2>/dev/null)" ]; then
  git -C "$main_dir" pull --ff-only --quiet 2>/dev/null || warn "git pull --ff-only failed"
else
  behind="$(git -C "$main_dir" rev-list --count main..origin/main 2>/dev/null)"
  [ "${behind:-0}" -gt 0 ] \
    && warn "main in $main_dir has uncommitted changes and is $behind commits behind origin/main, not synced"
fi
exit 0
