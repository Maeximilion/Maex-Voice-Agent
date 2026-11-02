#!/usr/bin/env bash
# Today's agent limit at every session start, read from the "Agents per weekday" table
# in CLAUDE.md (Maxi, 2026-11-02). Never blocks: one line for the user (systemMessage)
# and the same for Claude (additionalContext). Nothing in Claude Code caps agents per
# day, so the line is the rule's only reminder.
# AGENT_LIMIT_DAY overrides the day (English name), for the tests.
# shortcut: told once per session start; a session that runs past midnight keeps the
# day it started on until it resumes or compacts. Upgrade to a UserPromptSubmit check
# with per-session state if long sessions across midnight start to matter.
export LC_ALL=C
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
day=${AGENT_LIMIT_DAY:-$(TZ=Europe/Berlin date +%A)}
# The row whose first cell is exactly the day: "| Monday | 10 | welcome |".
row=$(grep -E "^\| *$day *\|" "$root/CLAUDE.md" 2>/dev/null | head -n 1)
[ -n "$row" ] || exit 0
IFS='|' read -r _ _ agents runs _ <<<"$row"
agents=$(sed -E 's/^ +| +$//g' <<<"$agents") runs=$(sed -E 's/^ +| +$//g' <<<"$runs")
[ -n "$agents" ] || exit 0
msg="agent limit today ($day, CLAUDE.md): $agents agents at once, a workflow's agents included; workflows, ultracode, review and audit agents: $runs."
jq -n --arg m "$msg" '{systemMessage: $m, hookSpecificOutput: {hookEventName: "SessionStart",
  additionalContext: ("Keep to this for the whole session: " + $m)}}'
