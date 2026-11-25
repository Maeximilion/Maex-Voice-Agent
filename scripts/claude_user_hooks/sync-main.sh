#!/usr/bin/env bash
# Keeps local main in step with origin/main. Fast-forward only: never touches
# local commits or uncommitted work. Called by Claude Code hooks:
#   SessionStart         -> sync-main.sh --always
#   PostToolUse (shell)  -> sync-main.sh   (acts only after "gh pr merge")
#   UserPromptSubmit     -> sync-main.sh --branch   (tells Claude when the
#                           session's branch is behind origin/main; a hook does
#                           not merge into a branch itself, that can conflict)
if [ "$1" = "--branch" ]; then
  cwd=$(jq -r '.cwd // empty')
  cd "$cwd" 2>/dev/null || exit 0
  branch=$(git branch --show-current 2>/dev/null)
  [ -n "$branch" ] && [ "$branch" != "main" ] || exit 0
  # State lives under the home folder: a shared temp folder can be prepared by another local user.
  state="$HOME/.claude/hook-state/branch-sync"
  mkdir -p "$state"
  key="$state/$(git rev-parse --show-toplevel | sha256sum | cut -c1-64)"
  # ponytail: fetches at most every 10 minutes; lower -mmin if main moves faster.
  # Its own stamp, not FETCH_HEAD: a fetch of one feature ref refreshes that file and leaves origin/main stale.
  if [ -z "$(find "$key.fetched" -mmin -10 2>/dev/null)" ]; then
    git fetch -q --prune origin 2>/dev/null || exit 0
    # Named explicitly: the refspec of a single-branch clone leaves main out of a plain fetch.
    git fetch -q origin '+refs/heads/main:refs/remotes/origin/main' 2>/dev/null || exit 0
    touch "$key.fetched"
  fi
  # A branch whose upstream is gone was merged or deleted on the remote: nothing to sync.
  [ -z "$(git config "branch.$branch.merge")" ] || git rev-parse -q --verify '@{u}' >/dev/null 2>&1 || exit 0
  behind=$(git rev-list --count HEAD..origin/main 2>/dev/null)
  [ "${behind:-0}" -gt 0 ] || exit 0
  # Once per branch, state of origin/main and worktree, so a merge Claude has to postpone is not nagged about.
  # ponytail: repeats after 30 minutes while the branch is still behind, because a note can get lost
  # (a prompt another hook blocked never reaches Claude); raise -mmin if that is too often.
  # One note file per branch: switching back and forth must not bring a note back early.
  note="$key.$(printf '%s' "$branch" | sha256sum | cut -c1-16)"
  tip=$(git rev-parse origin/main)
  [ "$(cat "$note" 2>/dev/null)" != "$tip" ] || [ -z "$(find "$note" -mmin -30 2>/dev/null)" ] || exit 0
  echo "$tip" > "$note"
  msg="Branch sync check: $branch is $behind commits behind origin/main. Bring it up to date before you continue with the request: use the ccd_host sync_with_base_branch tool if this session has it, otherwise commit your work and run git merge origin/main (a merge, no rebase). Resolve conflicts, and push if the branch has an upstream. If the branch has an open pull request, the merge moves its head, so the review of the head commit has to be requested again."
  jq -n --arg m "$msg" '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $m}}'
  exit 0
fi
if [ "$1" != "--always" ]; then
  # Only the command counts: output that merely mentions the text must not start a sync.
  jq -r '.tool_input.command // empty' 2>/dev/null | grep -q 'gh pr merge' || exit 0
fi

git rev-parse --git-dir >/dev/null 2>&1 || exit 0
# A repository with its own SessionStart sync runs it at the same moment, and two fetches
# race for the same ref locks: one fails (19.10.2026, 18 of 20 starts warned). Yield to it,
# but only on startup and resume: the project hook's matcher; on clear and compact it does not run.
if [ "$1" = "--always" ] && [ -f "$(git rev-parse --show-toplevel 2>/dev/null)/.claude/hooks/sync_main.sh" ]; then
  src=; [ -t 0 ] || src=$(jq -r '.source // empty' 2>/dev/null)
  case "$src" in startup|resume) exit 0 ;; esac
fi
git fetch -q --prune origin 2>/dev/null || exit 0
git fetch -q origin '+refs/heads/main:refs/remotes/origin/main' 2>/dev/null || exit 0

# main is checked out in exactly one worktree (or none); update it there.
# NUL-separated, so a path with a line break in it stays one path. Git before 2.36 has
# no -z; there the lines of the plain list stand in for the records.
dir=
while IFS= read -r -d '' line; do
  case "$line" in
    "worktree "*) w=${line#worktree } ;;
    "branch refs/heads/main") dir=$w; break ;;
  esac
done < <(git worktree list --porcelain -z 2>/dev/null || git worktree list --porcelain | tr '\n' '\0')
if [ -n "$dir" ]; then
  # Git takes ignored files as expendable: a fast-forward that adds a tracked .env replaces
  # the local one without a word (audit of 15.10.2026). Files the move would add where one
  # already sits keep main where it is; names only, never content.
  kept=$(git -C "$dir" diff --name-only -z --no-renames --diff-filter=A HEAD origin/main 2>/dev/null \
    | while IFS= read -r -d '' f; do { [ -e "$dir/$f" ] || [ -L "$dir/$f" ]; } && printf '%s; ' "$f"; done)
  [ -n "$kept" ] || git -C "$dir" merge -q --ff-only origin/main 2>/dev/null
  # Still behind means the fast-forward was refused or skipped; say so instead of drifting.
  behind=$(git -C "$dir" rev-list --count main..origin/main 2>/dev/null)
  if [ "${behind:-0}" -gt 0 ]; then
    # systemMessage reaches the user only, additionalContext Claude only: both, so the
    # warning is neither missed nor left without action (14.10.2026, 192 commits behind).
    event=PostToolUse; [ "$1" = "--always" ] && event=SessionStart
    files=$(git -C "$dir" status --porcelain 2>/dev/null | head -n 10 | sed 's/^ //' | paste -sd ';' - | sed 's/;/; /g')
    why="uncommitted files: ${files:-none, local commits block it}"
    [ -z "$kept" ] || why="it would overwrite the ignored files ${kept%; }"
    jq -n --arg d "$dir" --arg b "$behind" --arg w "$why" --arg e "$event" \
      '"sync-main: main in \($d) is \($b) commits behind origin/main and was not fast-forwarded (\($w)), check git status" as $m
      | {systemMessage: $m, hookSpecificOutput: {hookEventName: $e, additionalContext: ($m + ". Say so at the top of your reply, with the files that block it, and get it fixed with Maxi before other work.")}}'
  fi
else
  # Refused means local main has commits origin/main lacks; say so instead of drifting.
  git fetch -q origin main:main 2>/dev/null \
    || printf '{"systemMessage":"sync-main: local main has diverged from origin/main and was not updated, check git log origin/main..main"}\n'
fi
exit 0
