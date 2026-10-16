#!/usr/bin/env bash
# Is a security audit due? Rules and log: docs/09_OPERATIONS_LEGAL.md section 5.
# The baseline is the last audit that covered every exposed area (kind "full" or
# "scoped"); a "component" audit does not move it, so changes elsewhere are never
# dropped. Prints the baseline, the exposed files changed since, and the verdict.
#   bash scripts/audit_due.sh           /start: scoped audit due after 30 days with changes
#   bash scripts/audit_due.sh --gate    /gate:  due as soon as anything changed
#   bash scripts/audit_due.sh --task T  /task:  is the last audit naming task T still current
#   bash scripts/audit_due.sh --tasks   /start: --task for every open row with the go-live note
set -euo pipefail
root=${AUDIT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
cd "$root"
# When main is judged, main's own copy of this script judges it: a worktree that is
# behind would otherwise apply an older list of exposed areas or an older verdict.
case "${1-}" in
  "" | --tasks)
    if [ -z "${AUDIT_FROM_MAIN-}" ] && main_hash=$(git rev-parse -q --verify origin/main:scripts/audit_due.sh) \
      && [ "$main_hash" != "$(git hash-object "$0")" ]; then
      copy=$(git rev-parse --git-path audit_due.main.sh)
      git cat-file blob "$main_hash" > "$copy"
      AUDIT_FROM_MAIN=1 AUDIT_ROOT=$root exec bash "$copy" "$@"
    fi
    ;;
esac
log=docs/09_OPERATIONS_LEGAL.md
# The exposed areas: what faces the outside or decides whether a call reaches the team.
# All of api/ (only its tests left out): the tools, the GUI and the agent reach almost
# every package, so a list of single packages kept missing one (schemas, domain, models).
# prompts/: the system prompt goes into the live model's context.
exposed=(api/ prompts/ printbridge/ deploy/ n8n/ db/migrations/ docker-compose.yml
  docker-compose.override.yml .github/workflows/ scripts/backup.sh scripts/restore.sh
  scripts/lib_pg.sh scripts/db_role.sh scripts/migrate.sh ':!*README.md' ':!api/tests/')
# Only READMEs are left out of the Markdown: the prompts in prompts/ are Markdown too.

mode=${1-}
ref=origin/main
# A gate and a go-live step judge what is checked out; /start judges main, also when
# it checks the tasks (--tasks sets AUDIT_REF), from a worktree that may be behind.
case "$mode" in --gate | --task) ref=${AUDIT_REF:-HEAD} ;; esac
# The docs as of the ref judged: main's task list and audit log when main is judged.
doc() { if [ "$ref" = origin/main ]; then git show "origin/main:$1"; else cat "$1"; fi; }

if [ "$mode" = --tasks ]; then
  # Rows that carry the note and are open or in progress (the last column is the status).
  awk -F'|' -v note='Security audit before go-live (docs/09 §5)' 'index($0, note) {
    s = $(NF - 1); sub(/^ +/, "", s)
    if (s ~ /^(open|offen|in progress|in Arbeit)/) { id = $2; gsub(/ /, "", id); print id }
  }' <(doc docs/07_WORKPACKAGES.md) | while read -r id; do AUDIT_REF=origin/main bash "$0" --task "$id"; done
  exit 0
fi
rows=$(doc "$log" | sed -n '/^### Audit log/,/^## /p' | grep -E '^\| [0-9]{2}\.[0-9]{2}\.[0-9]{4} \|' || true)

if [ "$mode" = --task ]; then
  task=${2:?task ID missing}
  # The task ID in the Scope column, the one before Result.
  row=$(grep -E "\| [^|]*\b${task//./\\.}\b[^|]*\|[^|]*\|$" <<<"$rows" | tail -n 1 || true)
else
  row=$(grep -E '^\| [^|]+ \| (full|scoped)' <<<"$rows" | tail -n 1 || true)
fi
if [ -z "$row" ]; then
  if [ "$mode" = --task ]; then
    echo "audit for $task: due, none recorded; before the step that makes it reachable from outside"
    exit 0
  fi
  echo "audit log: no full or scoped audit in $log" >&2
  exit 2
fi

date=$(cut -d'|' -f2 <<<"$row" | tr -d ' ')
# The Commit column itself, so a Scope or Result that names other commits cannot mislead.
commit=$(cut -d'|' -f4 <<<"$row" | tr -d ' `')
# The log records commits on main: a feature-branch commit is gone after a squash merge.
if ! git merge-base --is-ancestor "$commit" "$ref" 2>/dev/null; then
  echo "audit log: commit $commit of $date is not on $ref; record the main commit the audit read" >&2
  exit 2
fi
age=$(( ($(date +%s) - $(date -d "${date:6:4}-${date:3:2}-${date:0:2}" +%s)) / 86400 ))
if [ "$ref" = HEAD ]; then
  # A gate or a go-live step judges the checkout as it is: committed, staged, unstaged
  # and new files alike, so an edit not yet committed cannot slip past.
  files=$( (git diff --name-only "$commit" -- "${exposed[@]}"
    git diff --cached --name-only "$commit" -- "${exposed[@]}"
    git ls-files --others --exclude-standard -- "${exposed[@]}") | sort -u)
else
  files=$(git diff --name-only "$commit..$ref" -- "${exposed[@]}")
fi
changed=$(grep -c . <<<"$files" || true)

if [ "$mode" = --task ]; then
  echo "last audit for $task: $date $commit, $age days ago"
  verdict="audit for $task: recorded"
  [ "$changed" -eq 0 ] || verdict="audit for $task: due again, scoped to the files below; before the step that makes it reachable from outside"
else
  echo "baseline: $date $commit, $age days ago"
  if [ "$mode" = --gate ]; then
    if [ "$changed" -eq 0 ]; then verdict="audit due: no"
    elif [ "$age" -lt 30 ]; then verdict="audit due: yes, scoped"
    else verdict="audit due: yes, full"; fi
  elif [ "$age" -ge 30 ] && [ "$changed" -gt 0 ]; then
    verdict="audit due: yes, scoped"
  else
    verdict="audit due: no"
  fi
fi
echo "exposed files changed since: $changed"
[ "$changed" -eq 0 ] || sed 's/^/  /' <<<"$files"
echo "$verdict"
