#!/usr/bin/env bash
# SessionStart hook: bring a clean local main up to date before work starts.
# Silent on success, a warning on failure (see below), and never blocks the session.
# Git runs against the repository that holds this script, not the current directory:
# after Claude changes directory the working directory no longer is the project.
root="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)" || exit 0
git -C "$root" rev-parse --git-dir >/dev/null 2>&1 || exit 0

# Warnings go out once, at exit. Plain stdout of a SessionStart hook reaches Claude only,
# and from 12 to 14.10.2026 the main folder fell 192 commits behind while such a note
# went unread. So with --json (Claude Code) they become a message the user sees plus an
# instruction for Claude; without it (Codex, a run by hand) they stay plain lines.
# ${1-}: an exported SHELLOPTS with nounset reaches this script, and Codex passes nothing.
mode=${1-} warnings=
warn() { warnings+="SessionStart sync: $1, check git status"$'\n'; }
json_string() {
  local s=$1 i o c u t
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\r'/}; s=${s//$'\n'/\\n}
  # Every other control character too: a path may hold one, and Claude Code drops
  # output it cannot parse.
  for i in {1..31}; do
    printf -v o '%03o' "$i"; printf -v c "\\$o"; printf -v u '\\u%04x' "$i"
    s=${s//"$c"/$u}
  done
  # A Unix path may also hold bytes that are not UTF-8, and such output cannot be read
  # at all: iconv drops them. Without iconv the text goes out as it is.
  t="$(printf '%s' "$s" | iconv -c -f UTF-8 -t UTF-8 2>/dev/null)"
  [ -n "$t" ] && s=$t
  printf '"%s"' "$s"
}
report() {
  [ -n "$warnings" ] || return 0
  warnings=${warnings%$'\n'}
  if [ "$1" = --json ]; then
    printf '{"systemMessage":%s,"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}\n' \
      "$(json_string "$warnings")" \
      "$(json_string "$warnings. Say so at the top of your first reply, with the files that block it, and get it fixed with Maxi before other work.")"
  else
    printf '%s\n' "$warnings"
  fi
}
trap 'report "$mode"' EXIT

# main named explicitly: the refspec of a single-branch clone leaves it out of a plain
# fetch, and everything below reads origin/main.
if ! git -C "$root" fetch --prune --quiet 2>/dev/null \
  || ! git -C "$root" fetch --quiet origin '+refs/heads/main:refs/remotes/origin/main' 2>/dev/null; then
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
  # Only a merge without conflicts counts: for a conflict git still prints a tree, and
  # for a file the branch deleted and main changed that tree is main's own.
  merged="$(git -C "$primary" merge-tree --write-tree origin/main "$1" 2>/dev/null)" \
    && [ "$merged" = "$(git -C "$primary" rev-parse 'origin/main^{tree}' 2>/dev/null)" ] && return 0
  tip="$(git -C "$primary" rev-parse "$1" 2>/dev/null)"
  # --base main: a pull request merged into another branch has not reached main.
  heads="$(cd "$primary" && timeout 10 gh pr list --state merged --base main --head "$1" \
    --json headRefOid --jq '.[].headRefOid' 2>/dev/null)" || return 2
  printf '%s\n' "$heads" | grep -qx "$tip"
}
# The files that moving the clean folder $1 from HEAD to $2 would add where an ignored
# file already sits. Git takes ignored files as expendable and overwrites them without a
# word (audit of 15.10.2026: a local .env once origin tracks one). Names only, never content.
clobbered() {
  local f
  git -C "$1" diff --name-only -z --no-renames --diff-filter=A HEAD "$2" 2>/dev/null \
    | while IFS= read -r -d '' f; do
        { [ -e "$1/$f" ] || [ -L "$1/$f" ]; } && printf '%s; ' "$f"
      done
  # The loop's status is its last path's, not whether the scan worked (Codex PR #411).
  return 0
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
      # The GitHub call above can take seconds: a branch started or a file edited in
      # that time is not ours to leave (audit of 15.10.2026).
      elif [ "$(git -C "$primary" branch --show-current 2>/dev/null)" != "$branch" ] \
        || [ -n "$(git -C "$primary" status --porcelain 2>/dev/null)" ]; then
        :
      elif kept="$(clobbered "$primary" main)" && [ -n "$kept" ]; then
        warn "the main folder is on the finished branch $branch, not switched: main would overwrite the ignored files ${kept%; }"
      else
        git -C "$primary" switch --quiet main 2>/dev/null \
          || warn "the main folder is on the finished branch $branch and could not be switched to main"
      fi ;;
    # Not the same as "not merged": without the answer the folder may sit on an obsolete branch.
    2) warn "the main folder is on $branch, whose remote branch is gone, and GitHub could not be asked whether it was merged (gh missing, signed out or too slow), left on it" ;;
  esac
fi

# main is checked out in at most one worktree, often not the one this session runs in.
# Read again here, after the switch and any GitHub call, so the folder merged into below
# is the one that has main now (audit of 15.10.2026: a branch started in between was
# fast-forwarded). shortcut: milliseconds between this list and the merge stay open, git
# has no fast-forward that names the branch and updates its folder; take a lock in every
# writer if that ever bites.
worktrees
if [ -z "$main_dir" ]; then
  # Checked out nowhere: move the branch itself, so the next task starts from today's main.
  git -C "$root" fetch --quiet origin main:main 2>/dev/null \
    || warn "local main has commits origin/main lacks and was not updated"
  exit 0
fi
blocking="$(git -C "$main_dir" status --porcelain 2>/dev/null)"
if [ -z "$blocking" ] && kept="$(clobbered "$main_dir" origin/main)" && [ -n "$kept" ]; then
  behind="$(git -C "$main_dir" rev-list --count main..origin/main 2>/dev/null)"
  warn "main in $main_dir is $behind commits behind origin/main, not fast-forwarded: it would overwrite the ignored files ${kept%; }"
elif [ -z "$blocking" ]; then
  # Fetched above, so no second fetch: a failed merge is about the folder, not the network.
  failed="$(git -C "$main_dir" merge --ff-only --quiet origin/main 2>&1 >/dev/null)"
  # Another session may have moved it in the meantime (14.10.2026: a warning for a folder
  # that was in sync); only a folder still behind is worth a word.
  behind="$(git -C "$main_dir" rev-list --count main..origin/main 2>/dev/null)"
  [ "${behind:-0}" -gt 0 ] \
    && warn "main in $main_dir is $behind commits behind origin/main and could not be fast-forwarded (git: ${failed##*$'\n'})"
else
  behind="$(git -C "$main_dir" rev-list --count main..origin/main 2>/dev/null)"
  count="$(printf '%s\n' "$blocking" | wc -l | tr -d ' ')"
  files="$(printf '%s\n' "$blocking" | head -n 10 | sed 's/^ //' | paste -sd ';' - | sed 's/;/; /g')"
  [ "${behind:-0}" -gt 0 ] \
    && warn "main in $main_dir has uncommitted changes and is $behind commits behind origin/main, not synced ($count files block it: $files)"
fi
exit 0
