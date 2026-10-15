#!/usr/bin/env bash
# Is a security audit due? Rules and log: docs/09_OPERATIONS_LEGAL.md section 5.
# The baseline is the last audit that covered every exposed area (kind "full" or
# "scoped"); a "component" audit does not move it, so changes elsewhere are never
# dropped. Prints the baseline, the exposed files changed since, and the verdict.
#   bash scripts/audit_due.sh           /start: scoped audit due after 30 days with changes
#   bash scripts/audit_due.sh --gate    /gate:  due as soon as anything changed
#   bash scripts/audit_due.sh --task T  /task:  has the log a row whose scope names task T
set -euo pipefail
cd "$(dirname "$0")/.."
log=docs/09_OPERATIONS_LEGAL.md
# The exposed areas: what faces the outside or decides whether a call reaches the team.
exposed=(api/core/ api/agent/ api/telephony/ api/tools/ api/gui/ api/events/
  api/domain/calls/ api/domain/callbacks/ api/main.py api/config.py api/Dockerfile
  api/requirements.in api/requirements.txt api/requirements-voice.in api/requirements-voice.txt deploy/ n8n/
  docker-compose.yml docker-compose.override.yml .github/workflows/
  scripts/backup.sh scripts/restore.sh scripts/lib_pg.sh ':!*.md' ':!api/tests/')

rows=$(sed -n '/^### Audit log/,/^## /p' "$log" | grep -E '^\| [0-9]{2}\.[0-9]{2}\.[0-9]{4} \|' || true)

if [ "${1-}" = --task ]; then
  task=${2:?task ID missing}
  if grep -qE "\| [^|]*\b${task//./\\.}\b[^|]*\|[^|]*\|$" <<<"$rows"; then
    echo "audit for $task: recorded"
  else
    echo "audit for $task: due before the step that makes it reachable from outside"
  fi
  exit 0
fi

row=$(grep -E '^\| [^|]+ \| (full|scoped)' <<<"$rows" | tail -n 1)
[ -n "$row" ] || { echo "audit log: no full or scoped audit in $log" >&2; exit 2; }
date=$(cut -d'|' -f2 <<<"$row" | tr -d ' ')
commit=$(grep -oE '`[0-9a-f]{7,40}`' <<<"$row" | head -n 1 | tr -d '`')
age=$(( ($(date +%s) - $(date -d "${date:6:4}-${date:3:2}-${date:0:2}" +%s)) / 86400 ))
ref=origin/main
[ "${1-}" = --gate ] && ref=HEAD
files=$(git diff --name-only "$commit..$ref" -- "${exposed[@]}")
changed=$(grep -c . <<<"$files" || true)

echo "baseline: $date $commit, $age days ago"
echo "exposed files changed since: $changed"
if [ "${1-}" = --gate ]; then
  if [ "$changed" -eq 0 ]; then echo "audit due: no"
  elif [ "$age" -lt 30 ]; then echo "audit due: yes, scoped"
  else echo "audit due: yes, full"; fi
elif [ "$age" -ge 30 ] && [ "$changed" -gt 0 ]; then
  echo "audit due: yes, scoped"
else
  echo "audit due: no"
fi
