#!/usr/bin/env bash
# Model and effort advice per prompt, from the "Model Routing" table in CLAUDE.md.
# Never blocks: it shows the user one line (systemMessage) and tells Claude to open its
# reply with the same line (additionalContext). A hook cannot switch the model itself.
#   SessionStart, PostModelSwitch -> remember the session's model
#   UserPromptSubmit              -> advise when the session differs from the table
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
# Slash commands check the table themselves; machine-delivered prompts have nobody to switch;
# one-word answers ("ja", "weiter") carry no task.
case "$prompt" in "<pasted_content"*|"<!--"*) ;; "<"*|"/"*) exit 0 ;; esac
[ "${#prompt}" -ge 40 ] || exit 0

if grep -qiE "$xhigh" <<<"$prompt"; then want=opus effort=xhigh
elif grep -qiE "$opus" <<<"$prompt"; then want=opus effort=high
elif grep -qiE "$sonnet" <<<"$prompt"; then want=sonnet effort=medium
else want=opus effort=high; fi

model=$(jq -r '.model // empty' <<<"$input")
[ -n "$model" ] || model=$(cat "$state/$sid.model" 2>/dev/null)
[ -n "$model" ] || model=$(cat "$HOME/.claude/hook-state/model-router/$sid.model" 2>/dev/null)
if [ -z "$model" ]; then
  transcript=$(jq -r '.transcript_path // empty' <<<"$input")
  [ -f "$transcript" ] && model=$(tac "$transcript" 2>/dev/null \
    | jq -r 'select(.type == "assistant" and (.isSidechain | not)) | .message.model
             | select(type == "string" and startswith("claude-"))' 2>/dev/null | head -n 1)
fi
have_effort=${CLAUDE_EFFORT:-$(jq -r '.effort // empty' <<<"$input")}

case "$want" in opus) name="Opus 5.5" ;; sonnet) name="Sonnet 5.5" ;; esac
[ -n "$model" ] || model="unknown"
# Same model family and same effort: nothing to say. Unknown effort counts as a mismatch
# only when the model differs, so a missing variable does not nag on every prompt.
[[ "$model" == *"$want"* ]] && { [ -z "$have_effort" ] || [ "$have_effort" = "$effort" ]; } && exit 0

msg="routing advice: this prompt fits $name at effort $effort (CLAUDE.md Model Routing); session is on ${model}, effort ${have_effort:-unknown}. Switch in the model menu if you want, no need to resend."
# The same advice once per session and state: a person who keeps the setting is not nagged.
hash=$(cksum <<<"$msg $model $have_effort")
[ "$(cat "$state/$sid.advised" 2>/dev/null)" != "$hash" ] || exit 0
echo "$hash" > "$state/$sid.advised"
jq -n --arg m "$msg" '{systemMessage: $m, hookSpecificOutput: {hookEventName: "UserPromptSubmit",
  additionalContext: ("Open your reply with this one line, then carry on with the work (the user may switch, do not wait): " + $m)}}'
