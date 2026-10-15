#!/usr/bin/env bash
# Model and effort advice per prompt, from the "Model Routing" table in CLAUDE.md.
# Never blocks: it shows the user one line (systemMessage) and tells Claude to open its
# reply with the same line (additionalContext). A hook cannot switch the model itself.
#   SessionStart, PostModelSwitch -> remember the session's model
#   UserPromptSubmit              -> two modes:
#     a line "Route: <model-id> <effort>" in the prompt (the brief a coordinating session
#       gives a thread) -> check the running model and effort against that line
#     otherwise -> classify the prompt by keyword and compare with the table
# The user-level model-router.sh stays out of this repo (it yields to CLAUDE.md), so this
# is the only router here. The keyword lists are a heuristic: tune them below.
xhigh='migration|alembic|\bprices?\b|\bpreis|\btotals?\b|delivery.?zone|liefer.?zone|opening.?hours|öffnungszeit|availability|verfügbar|allergen|menu_item_id|\bconfirm|bestätig|escalat|eskal|transfer|outage|fallback|\bdraft\b'
opus='\bdomain\b|\bagent/|\btools/|\bevents/|telephony|\bjobs/|\bprompts?/|deploy|printbridge|\bn8n\b|\bhooks?\b|asterisk|\bvoice\b|\bllm\b|settings\.json|docker-compose'
sonnet='\bgui\b|\bdocs?\b|readme|\bseeds?\b|\bimport|status update|templates?\b|\bjinja|htmx'

input=$(cat)
sid=$(jq -r '.session_id // empty' <<<"$input")
[ -n "$sid" ] || exit 0
state="$HOME/.claude/hook-state/route-advice"
mkdir -p "$state"

case "$(jq -r '.hook_event_name // empty' <<<"$input")" in
  SessionStart)    m=$(jq -r '.model // empty' <<<"$input"); [ -n "$m" ] && echo "$m" > "$state/$sid.model"; exit 0 ;;
  PostModelSwitch) jq -r '.to_model // empty' <<<"$input" > "$state/$sid.model"; exit 0 ;;
esac

[ -z "$(jq -r '.agent_id // empty' <<<"$input")" ] || exit 0
prompt=$(jq -r '.prompt // ""' <<<"$input")

family() { case "$1" in *haiku*) echo 1 ;; *sonnet*) echo 2 ;; *opus*) echo 3 ;; *fable*) echo 4 ;; *) echo 0 ;; esac; }
level() { case "$1" in low) echo 1 ;; medium) echo 2 ;; high) echo 3 ;; xhigh) echo 4 ;; max) echo 5 ;; *) echo 0 ;; esac; }

# A Route line wins over every filter below: a brief may arrive wrapped in a tag.
route=$(grep -oiE '^Route: *claude-[a-z0-9.-]+( +(low|medium|high|xhigh|max|none))?' <<<"$prompt" | head -n 1)
if [ -n "$route" ]; then
  read -r _ name effort <<<"${route,,}"
  [ "$effort" = none ] && effort=
else
  # Slash commands check the table themselves; machine-delivered prompts have nobody to
  # switch; one-word answers ("ja", "weiter") carry no task.
  case "$prompt" in "<pasted_content"*|"<!--"*) ;; "<"*|"/"*) exit 0 ;; esac
  [ "${#prompt}" -ge 40 ] || exit 0
  if grep -qiE "$xhigh" <<<"$prompt"; then name="Opus 5.5" effort=xhigh
  elif grep -qiE "$opus" <<<"$prompt"; then name="Opus 5.5" effort=high
  elif grep -qiE "$sonnet" <<<"$prompt"; then name="Sonnet 5.5" effort=medium
  else name="Opus 5.5" effort=high; fi
fi

model=$(jq -r '.model // empty' <<<"$input")
[ -n "$model" ] || model=$(cat "$state/$sid.model" 2>/dev/null)
[ -n "$model" ] || model=$(cat "$HOME/.claude/hook-state/model-router/$sid.model" 2>/dev/null)
if [ -z "$model" ]; then
  transcript=$(jq -r '.transcript_path // empty' <<<"$input")
  [ -f "$transcript" ] && model=$(tac "$transcript" 2>/dev/null \
    | jq -r 'select(.type == "assistant" and (.isSidechain | not)) | .message.model
             | select(type == "string" and startswith("claude-"))' 2>/dev/null | head -n 1)
fi
[ -n "$model" ] || model="unknown"
have_effort=${CLAUDE_EFFORT:-$(jq -r '.effort // empty' <<<"$input")}

want_f=$(family "${name,,}") have_f=$(family "$model")
# Same model family and same effort: nothing to say. An effort that is unknown on either
# side is not compared, so a missing variable does not nag on every prompt.
[ "$want_f" = "$have_f" ] && { [ -z "$have_effort" ] || [ -z "$effort" ] || [ "$have_effort" = "$effort" ]; } && exit 0

now="session is on $model, effort ${have_effort:-unknown}"
if [ -n "$route" ]; then
  msg="routing check: the brief asks for $name at effort ${effort:-any}, but this $now."
  # First time: stop before any work, so the starting session can switch this thread
  # (set_session_model / set_session_effort work on a thread it started, never on itself)
  # and send the brief again. Same mismatch after that resend: the switch did not take
  # (fork, cloud), so carry on and report it. After that: silent, never a loop.
  hash=$(cksum <<<"$msg")
  seen=$(grep -cx "$hash" "$state/$sid.seen" 2>/dev/null)
  echo "$hash" >> "$state/$sid.seen"
  case "${seen:-0}" in
    0) ctx="Routing mismatch: do not start the task and use no tools. Reply with exactly this one line, then end your turn: ROUTE-MISMATCH want=$name ${effort:-any} have=$model ${have_effort:-unknown}. The session that started you switches your model and effort and sends the brief again." ;;
    1) ctx="The brief was sent again but this thread still runs on the wrong setting: carry on with the task and name the mismatch in your report. $msg" ;;
    *) exit 0 ;;
  esac
  jq -n --arg m "$msg" --arg c "$ctx" '{systemMessage: $m, hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $c}}'
  exit 0
elif [ "$have_f" -lt "$want_f" ] || { [ "$have_f" = "$want_f" ] && [ "$(level "$have_effort")" -lt "$(level "$effort")" ]; }; then
  msg="routing advice: this prompt fits $name at effort $effort (CLAUDE.md Model Routing); $now. Switch up in the model menu if you want, no need to resend."
else
  msg="routing advice: this prompt fits $name at effort $effort (CLAUDE.md Model Routing); $now. A coordinating session hands it to a fresh thread with that model and effort and a Route line; otherwise switching down saves tokens."
fi
# The same advice once per session and state: a person who keeps the setting is not nagged.
hash=$(cksum <<<"$msg")
[ "$(cat "$state/$sid.advised" 2>/dev/null)" != "$hash" ] || exit 0
echo "$hash" > "$state/$sid.advised"
jq -n --arg m "$msg" '{systemMessage: $m, hookSpecificOutput: {hookEventName: "UserPromptSubmit",
  additionalContext: ("Open your reply with this one line, then carry on with the work (the user may switch, do not wait): " + $m)}}'
