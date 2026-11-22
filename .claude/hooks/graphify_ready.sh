#!/usr/bin/env bash
# A Graphify code graph for every folder a session works in (Maxi, 2026-11-15).
# `graphify-out/` is gitignored, so a fresh checkout or worktree has no graph: the
# `graphify query` rule in CLAUDE.md and the hook guards in `.claude/settings.json` then
# have nothing to read. At session start this builds the missing graph in the background
# (code only, no model call, nothing leaves the machine, about 30 seconds) and says so.
# Two folders count: the session's working folder (`cwd` of the payload), where Claude
# runs `graphify query`, and the project folder (`CLAUDE_PROJECT_DIR`), where the hook
# guards run. In a desktop app worktree session these differ: the guards run in the main
# checkout. Never blocks; without `graphify` on PATH it names the install steps once.
# GRAPHIFY_READY_WAIT=1 builds in the foreground, for the tests.
export LC_ALL=C
payload=$(cat)
say() {
  jq -n --arg m "$1" '{systemMessage: $m, hookSpecificOutput: {hookEventName: "SessionStart",
    additionalContext: $m}}'
}
if ! command -v graphify >/dev/null 2>&1; then
  say "graphify is not installed, so the code graph is off: docs/19_CLAUDE_CODE_SETUP.md section 5 \"Graphify\"."
  exit 0
fi
cwd=$(jq -r '.cwd // empty' <<<"$payload" 2>/dev/null)
build() {
  graphify update . >graphify-out/build.log 2>&1
  rmdir graphify-out/.building 2>/dev/null
}
started=() seen=()
for dir in "$cwd" "${CLAUDE_PROJECT_DIR:-}" "$PWD"; do
  [ -n "$dir" ] || continue
  dir=$(cd "$dir" 2>/dev/null && pwd -P) || continue
  case " ${seen[*]} " in *" $dir "*) continue ;; esac
  seen+=("$dir")
  cd "$dir" || continue
  # Only a checkout of this repository, never a folder that merely contains one.
  [ -f CLAUDE.md ] && [ -e .git ] || continue
  [ -f graphify-out/graph.json ] && continue
  mkdir -p graphify-out
  # A lock older than ten minutes is a build that died.
  find graphify-out/.building -maxdepth 0 -mmin +10 -exec rmdir {} \; 2>/dev/null
  # mkdir is atomic: a second session starting now leaves the build to the first.
  mkdir graphify-out/.building 2>/dev/null || continue
  if [ -n "${GRAPHIFY_READY_WAIT:-}" ]; then
    build
  else
    build </dev/null >/dev/null 2>&1 &
    disown
  fi
  started+=("$dir")
done
[ ${#started[@]} -gt 0 ] || exit 0
say "graphify: no code graph yet in ${started[*]}; building it in the background (about 30 seconds). Until then use the Graphify MCP connector; if graphify-out/graph.json is still missing when you need the CLI, run \`graphify update .\` there as a background command yourself."
