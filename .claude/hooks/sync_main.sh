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
# Sets `primary` (the main folder, listed first) and `main_dir` (where main is checked
# out, or empty). NUL-separated, so a path with a line break in it stays one path; git
# before 2.36 has no -z, there the lines of the plain list stand in for the records.
worktrees() {
  primary= main_dir= w=
  while IFS= read -r -d '' line; do
    case "$line" in
      "worktree "*) w=${line#worktree }; [ -n "$primary" ] || primary=$w ;;
      "branch refs/heads/main") main_dir=$w ;;
    esac
  done < <(git -C "$root" worktree list --porcelain -z 2>/dev/null \
    || git -C "$root" worktree list --porcelain | tr '\n' '\0')
}
# A branch is in main when its commits are there, or its changes are: after a squash
# merge the commits never land. Then either merging the branch into origin/main changes
# nothing (needs git 2.38), or, where main has moved on over the same lines since,
# GitHub lists a pull request merged into main with exactly this head commit (needs gh).
# Answers 0 yes, 1 no, 2 when GitHub had to be asked and gave no answer.
in_main() {
  git -C "$primary" merge-base --is-ancestor "$1" origin/main 2>/dev/null && return 0
  merged="$(git -C "$primary" merge-tree --write-tree origin/main "$1" 2>/dev/null | head -n 1)"
  [ -n "$merged" ] && [ "$merged" = "$(git -C "$primary" rev-parse 'origin/main^{tree}' 2>/dev/null)" ] && return 0
  tip="$(git -C "$primary" rev-parse "$1" 2>/dev/null)"
  # --base main: a pull request merged into another branch has not reached main.
  heads="$(cd "$primary" && timeout 10 gh pr list --state merged --base main --head "$1" \
    --json headRefOid --jq '.[].headRefOid' 2>/dev/null)" || return 2
  printf '%s\n' "$heads" | grep -qx "$tip"
}

# The main folder left on a finished branch goes back to main, or its files stay at the
# old state while every ref is current (12.10.2026: a kit merged that day was "not
# there"). Finished means: nothing uncommitted, the remote branch is gone, and its work
# is in origin/main. Only that folder; session worktrees are tidied by /start.
worktrees
branch="$(git -C "$primary" branch --show-current 2>/dev/null)"
if [ -n "$branch" ] && [ "$branch" != main ] \
  && [ -z "$(git -C "$primary" status --porcelain 2>/dev/null)" ] \
  && [ "$(git -C "$primary" for-each-ref --format='%(upstream:track)' "refs/heads/$branch")" = "[gone]" ]; then
  in_main "$branch"
  case $? in
    0)
      if [ -n "$main_dir" ]; then
        # Git checks a branch out in one folder only; which of the two gives way is not the hook's call.
        warn "the main folder is on the finished branch $branch and main is checked out in $main_dir, not switched"
      else
        git -C "$primary" switch --quiet main 2>/dev/null \
          || warn "the main folder is on the finished branch $branch and could not be switched to main"
        worktrees
      fi ;;
    # Not the same as "not merged": without the answer the folder may sit on an obsolete branch.
    2) warn "the main folder is on $branch, whose remote branch is gone, and GitHub could not be asked whether it was merged (gh missing, signed out or too slow), left on it" ;;
  esac
fi

# main is checked out in at most one worktree, often not the one this session runs in.
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
