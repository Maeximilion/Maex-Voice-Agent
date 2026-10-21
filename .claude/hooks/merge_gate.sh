#!/usr/bin/env bash
# PreToolUse gate for `gh pr merge` (CLAUDE.md §6 "Merging a PR into main", /done step 7b).
# Blocks the merge (exit 2, the reasons on stderr for Claude) unless, on the PR's head commit:
#   1. every check has finished and passed (success, skipped or neutral),
#   2. the branch contains origin/main, so no merge from main is missing,
#   3. the status version is higher than main's (exempt: chore/readme-weekly-*),
#   4. a review is clean: the Codex state comment names this head without open findings,
#      or a comment by the owner or a collaborator opens with "Merge gate: <head sha>"
#      (Claude's own review when Codex is out of quota, or the review loop limits decision).
# Not checkable here: that /ponytail-review ran, green evals, the xhigh rule. Those stay
# with /done and CLAUDE.md. Every other command passes untouched.
# ponytail: only `gh pr merge` at the start of a command is caught, `gh api .../merge` is not.
input=$(cat)
# Cheap prefilter first: this runs before every shell command.
grep -Eq 'gh[[:space:]]+pr[[:space:]]+merge' <<<"$input" || exit 0
block() {
  printf 'Merge gate (.claude/hooks/merge_gate.sh) blocked the merge:\n%s\n' "$1" >&2
  exit 2
}
command -v jq >/dev/null || block "- jq is missing, so the gate cannot read the command"
cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
merge=$(grep -Eo '(^|[;&|(])[[:space:]]*gh[[:space:]]+pr[[:space:]]+merge([[:space:]]+[^;&|[:space:]]+)*' <<<"$cmd" | head -1)
[ -n "$merge" ] || exit 0
# The PR is the first number or pull URL after `merge`; without one, gh takes the branch's PR.
target=$(tr -s '[:space:]' '\n' <<<"${merge#*merge}" | grep -E '^([0-9]+|https://[^ ]+/pull/[0-9]+)$' | head -1)

cd "$(dirname "$0")/../.." || block "- the repository of the hook was not found"
pr=$(gh pr view $target --json number,headRefOid,headRefName,statusCheckRollup 2>&1) ||
  block "- gh pr view failed: $pr"
read -r n head branch < <(jq -r '[.number, .headRefOid, .headRefName] | @tsv' <<<"$pr")
fail=""

checks=$(jq -r 'def result: if (.conclusion // "") != "" then .conclusion else (.state // .status // "PENDING") end;
  if (.statusCheckRollup | length) == 0 then "no check reported yet"
  else .statusCheckRollup[] | select(result | IN("SUCCESS", "SKIPPED", "NEUTRAL") | not)
    | "\(.name // .context): \(result | ascii_downcase)" end' <<<"$pr")
[ -z "$checks" ] || fail+="- CI is not green on ${head:0:7}: ${checks//$'\n'/, }"$'\n'

if git fetch -q origin main "refs/pull/$n/head" 2>/dev/null && git cat-file -e "$head^{commit}" 2>/dev/null; then
  git merge-base --is-ancestor origin/main "$head" ||
    fail+="- the branch is behind main: merge origin/main into it, then get CI and the review on the new head"$'\n'
  if [[ $branch != chore/readme-weekly-* ]]; then
    v() { git show "$1:docs/01_STATUS.md" 2>/dev/null | grep -o 'Status version: [0-9.]*' | head -1 | cut -d' ' -f3; }
    m=$(v origin/main)
    b=$(v "$head")
    { [ -n "$b" ] && [ "$m" != "$b" ] && [ "$(printf '%s\n' "$m" "$b" | sort -V | tail -1)" = "$b" ]; } ||
      fail+="- the status is not bumped ($m on main, ${b:-none} on ${head:0:7}): run scripts/status_bump.py, commit, push"$'\n'
  fi
else
  fail+="- head ${head:0:7} could not be fetched, so the sync with main and the status bump are unchecked"$'\n'
fi

comments=$(gh api --paginate "repos/{owner}/{repo}/issues/$n/comments?per_page=100" 2>&1) ||
  block "${fail}- the PR comments could not be read: $comments"
clean=$(jq -rs --arg h "$head" '
  (add // []) as $c
  | ([$c[] | select(.user.login == "chatgpt-codex-connector[bot]") | .body
      | capture("codex-review-state:v1 (?<s>\\{.*\\}) -->").s | (try fromjson catch {}) | .code_review
      | select(.commit_sha == $h and .status == "completed" and .has_unresolved_findings == false)]
    + [$c[] | select(.author_association | IN("OWNER", "MEMBER", "COLLABORATOR")) | .body
      | select(test("(^|\n)Merge gate: " + $h[0:7]))]) | length > 0' <<<"$comments")
[ "$clean" = true ] ||
  fail+="- no clean review on ${head:0:7}: wait for Codex on this head, or post Claude's own review or the review loop limits decision as a comment that opens with \"Merge gate: ${head:0:7}\""$'\n'

[ -z "$fail" ] || block "$fail"
exit 0
