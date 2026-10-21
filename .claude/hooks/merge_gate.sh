#!/usr/bin/env bash
# PreToolUse gate for `gh pr merge` (CLAUDE.md §6 "Merging a PR into main", /done step 7b).
# It guards against a forgotten step, not against a session that tries to get around it.
# Allowlist, not a parser: a command that mentions `gh pr merge` passes only in this form,
#   gh pr merge <number> --merge --match-head-commit <full sha> [--delete-branch]
# and it is checked in the directory the command runs in (the hook input's cwd), where gh
# resolves the PR too. A cd in front is refused: it could select another repository.
# so a wrapped call (if, command, env, a subshell, bash -c), another strategy, --auto, a
# branch name or a mention in a commit message or search is blocked with the form to use.
# --match-head-commit makes gh refuse the merge once the head moves after this check.
# Then, on that head commit of a PR into main:
#   1. every check has finished and passed (success, skipped or neutral),
#   2. the branch contains origin/main, so no merge from main is missing,
#   3. the status version is higher than main's; exempt only chore/readme-weekly-* whose
#      changes are all generated (README.md, .github/assets/*.svg, png-sources.json),
#   4. a review is clean: the Codex state comment names this head without open findings,
#      or a comment by the owner or a collaborator opens with "Merge gate: <head sha>"
#      (Claude's own review when Codex is out of quota, or the review loop limits decision).
# settings.json sets onFailure "block", so a timeout or a crash of this script blocks too.
# Not checkable here: that /ponytail-review ran, green evals, the xhigh rule. Those stay
# with /done and CLAUDE.md.
# Detection ignores case (PowerShell finds GH as gh); the allowed form does not.
# ponytail: `gh api .../merge`, gh aliases and quoting tricks are not caught.
input=$(cat)
# Cheap prefilter first: this runs before every shell command.
grep -qi 'merge' <<<"$input" || exit 0
block() {
  printf 'Merge gate (.claude/hooks/merge_gate.sh) blocked the merge:\n%s\n' "$1" >&2
  exit 2
}
command -v jq >/dev/null || block "- jq is missing, so the gate cannot read the command"
cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
cmd=${cmd//$'\r'/} # jq on Windows writes CRLF, also inside a multi-line command
# A line continuation must not hide the words from the check.
continuation=$'\\\n'
cmd=${cmd//$'`\n'/ } # PowerShell's line continuation is a backtick
# Flags may stand between gh, pr and merge (gh -R owner/repo pr merge, gh pr -R x merge),
# but no command separator; merge counts only as a word of its own (not --json mergeable).
grep -Eqi 'gh(\.exe)?[^;&|[:cntrl:]]*[[:space:]]pr[[:space:]]([^;&|[:cntrl:]]*[[:space:]])?merge([[:space:]]|$)' <<<"${cmd//"$continuation"/ }" || exit 0
form='^[[:space:]]*gh pr merge ([0-9]+) --merge --match-head-commit ([0-9a-f]{40})( --delete-branch)?[[:space:]]*$'
[[ $cmd =~ $form ]] || block "- the gate takes only: gh pr merge <number> --merge --match-head-commit <full head sha> (--delete-branch after it is allowed), run in the repository's folder. A cd in front, a wrapped call, another merge strategy and --auto are refused; text or a search that mentions the command goes into a file or reads \`gh pr merg[e]\`"
target=${BASH_REMATCH[1]}
want=${BASH_REMATCH[2]}

dir=$(jq -r '.cwd // empty' <<<"$input")
cd "${dir:-$(dirname "$0")/../..}" || block "- the folder the command runs in was not found: $dir"
pr=$(gh pr view "$target" --json number,url,headRefOid,headRefName,baseRefName,statusCheckRollup 2>&1) ||
  block "- gh pr view failed: $pr"
# Through $(...), not < <(...): Git Bash drops the CR that jq on Windows ends lines with.
read -r n url head branch base <<<"$(jq -r '[.number, .url, .headRefOid, .headRefName, .baseRefName] | @tsv' <<<"$pr")"
# The repository gh picked (its default can differ from origin), taken from the PR's URL.
repo=${url%/pull/*}
[ "$base" = main ] || block "- the PR targets $base, the gate only merges into main"
[ "$head" = "$want" ] || block "- the PR's head is ${head:0:7}, the command names ${want:0:7}: check the new head first"
fail=""

# A check that ran twice on one head counts by the run that started last: a cancelled run
# can finish after the run that replaced it, and a queued run without a start time is the
# newest of all. Runs that started in the same second go by their Actions job id.
checks=$(jq -r 'def result: if (.conclusion // "") != "" then .conclusion else (.state // .status // "PENDING") end;
  if (.statusCheckRollup | length) == 0 then "no check reported yet"
  else .statusCheckRollup | group_by([.workflowName // "", .name // .context])
    | map(sort_by(.startedAt // (if (.status // "") == "COMPLETED" then "" else "9999" end),
        ((.detailsUrl // "") | capture("/job/(?<j>[0-9]+)").j // "0" | tonumber)) | last)[]
    | select(result | IN("SUCCESS", "SKIPPED", "NEUTRAL") | not)
    | "\(.name // .context): \(result | ascii_downcase)" end' <<<"$pr")
[ -z "$checks" ] || fail+="- CI is not green on ${head:0:7}: ${checks//$'\n'/, }"$'\n'

main=refs/merge-gate/main
if git fetch -q "$repo" "+refs/heads/main:$main" "refs/pull/$n/head" 2>/dev/null && git cat-file -e "$head^{commit}" 2>/dev/null; then
  git merge-base --is-ancestor "$main" "$head" ||
    fail+="- the branch is behind main: merge origin/main into it, then get CI and the review on the new head"$'\n'
  generated='^(README\.md|\.github/assets/[^/]+\.svg|\.github/assets/png-sources\.json)$'
  if [[ $branch != chore/readme-weekly-* ]] || git diff --name-only "$main...$head" | grep -Evq "$generated"; then
    v() { git show "$1:docs/01_STATUS.md" 2>/dev/null | grep -o 'Status version: [0-9.]*' | head -1 | cut -d' ' -f3; }
    m=$(v "$main")
    b=$(v "$head")
    # Exactly three numbers, compared as numbers: 2.7.00 is no bump over 2.7.0, 2.7.0.1 no version.
    num() { [[ $1 =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] &&
      echo "$((10#${BASH_REMATCH[1]})) $((10#${BASH_REMATCH[2]})) $((10#${BASH_REMATCH[3]}))"; }
    read -r m1 m2 m3 <<<"$(num "$m")"
    read -r b1 b2 b3 <<<"$(num "$b")"
    { [ -n "$m3" ] && [ -n "$b3" ] && ((b1 > m1 || (b1 == m1 && (b2 > m2 || (b2 == m2 && b3 > m3))))); } ||
      fail+="- the status is not bumped ($m on main, ${b:-none} on ${head:0:7}): run scripts/status_bump.py, commit, push"$'\n'
  fi
else
  fail+="- head ${head:0:7} could not be fetched, so the sync with main and the status bump are unchecked"$'\n'
fi

comments=$(gh api --paginate "repos/{owner}/{repo}/issues/$n/comments?per_page=100" 2>&1) ||
  block "${fail}- the PR comments could not be read: $comments"
# Codex's newest state decides, unless a "Merge gate:" comment came after it.
clean=$(jq -rs --arg h "$head" '
  (add // []) as $c
  | ([$c[] | select(.user.login == "chatgpt-codex-connector[bot]")
      | {t: .updated_at, s: (.body | capture("codex-review-state:v1 (?<s>\\{.*\\}) -->").s
          | (try fromjson catch {}) | .code_review)}]
    | sort_by(.t) | last // {t: "", s: {}}) as $codex
  | ($codex.s | .commit_sha == $h and .status == "completed" and .has_unresolved_findings == false)
  or ([$c[] | select(.author_association | IN("OWNER", "MEMBER", "COLLABORATOR"))
      | select(.created_at > $codex.t and (.body | startswith("Merge gate: " + $h[0:7])))]
    | length > 0)' <<<"$comments")
[ "$clean" = true ] ||
  fail+="- no clean review on ${head:0:7}: wait for Codex on this head, or post Claude's own review or the review loop limits decision as a comment that opens with \"Merge gate: ${head:0:7}\""$'\n'

[ -z "$fail" ] || block "$fail"
exit 0
