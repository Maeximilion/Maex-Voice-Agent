#!/usr/bin/env bash
# Model and effort advice per prompt, from the "Model Routing" table in CLAUDE.md.
# Never blocks: it shows the user one line (systemMessage) and tells Claude to open its
# reply with the same line (additionalContext). A hook cannot switch the model itself.
#   SessionStart, PostModelSwitch -> remember the session's model
#   UserPromptSubmit              -> two modes:
#     a brief whose first line is "Route: <model-id> <effort>" (what a coordinating session
#       gives a thread) -> check the running model and effort against that line
#     otherwise -> classify the prompt by keyword and compare with the table
# The user-level model-router.sh stays out of this repo (it yields to CLAUDE.md), so this
# is the only router here. The keyword lists are a heuristic: tune them below.
# Umlauts fold and match as letters only in a UTF-8 locale; the hook's own may be empty.
export LC_ALL=C.UTF-8
xhigh='migration|alembic|\bprices?\b|\bpreis|\btotals?\b|delivery.?zone|liefer.?zone|opening.?hours|öffnungszeit|availability|verfügbar|allergen|menu_item_id|(speech|spoken|utterance|transcript)[- ]?to[- ]?(menu|item)|menu[- ]?item matching|match(ing|es)? (speech|what the caller says|a spoken)|confirm[a-z]* (the |an? )?(order|booking|reservation|draft)|(order|booking|reservation) confirm|draft.?to.?confirmed|\bconfirm(\(|`| step| tool| logic| function| handles)|bestätig[a-z]* (die |der )?(bestellung|reservierung)|escalat|eskal|transfer|outage|hang.?up|auflegen|callbacks?|rückruf|calls? (should |must |is |gets? )?(be )?(ended|terminated|dropped|hung up)|\b(end|terminate|drop)(s|ing)? (the |a |an )?([a-z]+ )?calls?\b|(call|phone|transfer) fallback|fallback (to|for) (the )?(team|phone|call|human)|drafts? (orders?|status|state|bookings?|reservations?|transactions?|expir[a-z]*|lifecycle|cleanup)|drafts? ?(->|→|to) ?confirmed|(order|booking|reservation)s? drafts?|delivery.?fees?|discounts?|rabatt|\btax(es)?\b|steuer|coupons?|gutschein|surcharges?|zuschlag|refunds?|erstattung|\btips?\b|gratuit|trinkgeld|reschedul|umbuch|liefer.?gebühr|\bfees?\b|(reservation|booking)s? (conflict|check|rule|logic|capacity|slot|overlap)|double.?book|(bookings?|reservations?) (should |must |can )?(be |are |is |gets? )?(accepted|rejected|cancell?ed|created|declined|rescheduled|moved)|\b(accept|reject|cancel|decline)(s|ing|ed)? (a |an |the )?(bookings?|reservations?)|(booking|reservation) (acceptance|cancell?ation|creation)|überbuch|\bcapacity\b|(two|2|zwei) (fix.?)?(attempts|versuche)|still fail[a-z]* after|\b([3-9](rd|th)|[1-9][0-9]+(st|nd|rd|th)|[a-z]+(teenth|tieth)|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|dritte[nr]?|vierte[nr]?|fünfte[nr]?) (fix.?)?(attempt|try|versuch)|orders? (are |is |gets? |becomes? )?confirmed|(becomes?|turns?) confirmed'
# Hooks here are this project's hooks (.claude/hooks), never React or other framework hooks.
opus='\bdomain\b|\bagent/|\btools/|\bevents/|telephony|\bjobs/|\bprompts?/|deploy|printbridge|\bn8n\b|\.claude/hooks|hook scripts?|(project|routing|session|git|claude) hooks?\b|\bhooks? (and|in|of) (the )?(settings|project)|route_advice|model.?router|asterisk|\bvoice\b|\bllm\b|settings\.json|docker-compose'
# Imports here are the menu and data imports (scripts/import_menu.py), never a Python import.
dataimport='import_menu|kasse_to_csv|imports/|import(ing)? (the |a )?(menu|csv|register|data)|(menu|csv|data) import'
sonnet="\\bgui\\b|\\bdocs?\\b|readme|\\bseeds?\\b|$dataimport|"'status update|update (the )?(project |task |gate )?status\b|templates?\b|\bjinja|htmx|tests? for (the )?existing|existing tests?|review comments?|(address|fix|apply|resolve|handle) (the )?([a-z]+ )?(review|codex|feedback)|review (findings|feedback|changes|threads?)|(run|execute) (the )?([a-z]+ )?(tests?|test suite|suite)\b|\bpytest\b|make test\b'
# Docs-only: a Markdown file or docs/ path and no code path. Hard-rule words in such a
# prompt are the topic of the text, not code that decides.
# ponytail: "fix migration 007 and update docs/03" counts as docs-only; name the code path to avoid it.
docpath='\.md\b|\bdocs/|readme|\bdocs?\b|\bdocument(ation)?\b'
# Implementation work next to docs, GUI or an import: a code noun or verb, or a second
# action ("update README and fix authentication"); the mixed task takes the higher setting.
impl='\b(code|logic|matching|matcher|calculation|parser|handler|function|query|validation|refactor|implement|debug|migrate|deploy)[a-z]*\b|\b(and|then|plus|und) (also )?(fix|implement|refactor|debug|migrate|deploy)\b'
codepath='\.(py|sh|sql|js|ts|html|j2|json|ya?ml|toml)\b|\b(api|db|scripts|evals|prompts|deploy|n8n|sim|printbridge)/'
# Pure lookups go to Haiku: a search verb and no verb that changes anything.
search='^(please |(can|could|would) you (please )?)?(search|find|locate|grep|list|where (is|are|does)|look up|read (the )?([a-z]+ )?logs?|inspect|show|tail|suche|finde|wo (ist|sind|wird))\b'
change='\b(fix|change|update|add|build|write|implement|edit|refactor|remove|delete|rename|create|correct|modify|adjust|replace|move|patch|rewrite|migrate|repair|clean|improve|ändere|baue|schreibe|behebe|füge|lösche|erstelle|korrigiere|ersetze|run|execute|deploy|release|ship|optimi[sz]e|speed up|tune|harden|ausführen|starte|verschiebe|why|warum|debug|investigate|find out|figure out)\b'
# Answers that carry no task.
ack='^(ja|yes|y|ok(ay)?|nein|no|n|weiter|continue|go( on| ahead( with it)?)?|proceed|sounds good|leg los|mach weiter|danke|thanks|passt|mach( das)?|do it|stop|fertig|done)[.! ]*$'

input=$(cat)
sid=$(jq -r '.session_id // empty' <<<"$input")
[ -n "$sid" ] || exit 0
state="$HOME/.claude/hook-state/route-advice"
mkdir -p "$state"

case "$(jq -r '.hook_event_name // empty' <<<"$input")" in
  SessionStart)    m=$(jq -r '.model // empty' <<<"$input"); [ -n "$m" ] && echo "$m" > "$state/$sid.model"; exit 0 ;;
  PostModelSwitch) m=$(jq -r '.to_model // empty' <<<"$input")
                   if [ -n "$m" ]; then echo "$m" > "$state/$sid.model"; else rm -f "$state/$sid.model"; fi; exit 0 ;;
esac

[ -z "$(jq -r '.agent_id // empty' <<<"$input")" ] || exit 0
prompt=$(jq -r '.prompt // ""' <<<"$input" | tr -d '\r')

family() { case "$1" in *haiku*) echo 1 ;; *sonnet*) echo 2 ;; *opus*) echo 3 ;; *fable*) echo 4 ;; *) echo 0 ;; esac; }
level() { case "$1" in low) echo 1 ;; medium) echo 2 ;; high) echo 3 ;; xhigh) echo 4 ;; max) echo 5 ;; *) echo 0 ;; esac; }

# The Route line counts only as the brief's first non-empty line, or right after a
# wrapper tag (<brief>...), so a Route line quoted or pasted further down is just text.
first=$(awk 'NF { print; exit }' <<<"$prompt")
case "$first" in
  "<pasted_content"*|"<!--"*) head= ;;
  "<"*) head=${first#*>}; [ -n "${head// }" ] || head=$(awk 'NF { n++ } n == 2 { print; exit }' <<<"$prompt") ;;
  *) head=$first ;;
esac
# A wrapper closed on the same line (<brief>Route: ...</brief>) leaves its closing tag.
head=${head%%</*}
# Model and a valid effort, nothing after: "xhgh" or a missing effort is no route at all.
route=$(grep -oiE '^ *Route: *claude-[a-z0-9.-]+ +(low|medium|high|xhigh|max|none) *$' <<<"$head" || true)
# "none" is Haiku's effort only; on another model it would switch the effort check off.
[[ "${route,,}" == *" none"* && "${route,,}" != *haiku* ]] && route=
[[ "${route,,}" == *haiku* && "${route,,}" != *" none"* ]] && route=
# Only a model ID the routing table in CLAUDE.md names can be switched to; a typo is malformed.
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
if [ -n "$route" ] && [ -f "$root/CLAUDE.md" ]; then
  id=$(grep -oiE 'claude-[a-z0-9.-]+' <<<"$route" | tr 'A-Z' 'a-z')
  grep -qF "\`$id\`" "$root/CLAUDE.md" || route=
fi
if [ -z "$route" ] && grep -qiE '^ *Route:' <<<"$head"; then
  msg="routing check: the brief's Route line is not \"Route: <model-id> <effort>\" (effort low, medium, high, xhigh, max or none), so the setting of this thread cannot be checked."
  jq -n --arg m "$msg" '{systemMessage: $m, hookSpecificOutput: {hookEventName: "UserPromptSubmit",
    additionalContext: ("Carry on with the task and name this in your report to the session that started you: " + $m)}}'
  exit 0
fi
if [ -n "$route" ]; then
  # After the colon: "Route:claude-..." without a space is the same line.
  route=${route,,}
  read -r want effort <<<"${route#*:}"
else
  lower=${prompt,,}
  # /task and /start check the table themselves; a bare command carries no task of its own;
  # machine-delivered prompts have nobody to switch.
  case "$lower" in
    "<pasted_content"*|"<!--"*) ;;
    "/task"*|"/start"*|"<"*) exit 0 ;;
    /*) [[ "$lower" == *" "* ]] || exit 0 ;;
  esac
  # One line without paste wrappers, so ^ and $ below mean the start and end of the prompt.
  lower=$(sed -E 's#</?pasted_content[^>]*>##g' <<<"$lower" | tr '\n' ' ' | sed -E 's/^ +//; s/ +$//')
  grep -qiE "$ack" <<<"$lower" && exit 0
  # Fable runs only when Maxi asks for it by name; the hook never recommends it (below).
  # Named and not withdrawn ("off Fable", "not on Fable", "ohne Fable").
  grep -qiE '\b(on|use|using|with|mit|via|switch(ed)? to|run|keep) (claude-)?fable\b|\bfable\b.{0,40}\b(gate review|debug)|\b(gate review|debug)[a-z]*\b.{0,40}\bfable\b' <<<"$lower" \
    && ! grep -qiE '\b(off|away from|without|not (on|with|use|using|run|switch to)|don.?t (use|run|switch to)|never (use|run)|ohne|nicht (mit|auf)|weg von) fable\b' <<<"$lower" \
    && fable_named=1
  # A pure lookup first: what it looks for (menu_item_id, a migration) is its subject, not its work.
  # A verb inside what is looked for ("find where we write X") changes nothing.
  if grep -qiE "$search" <<<"$lower" \
    && ! sed -E 's/\b(where|which|that|wherever) ([a-z_]+ ){1,3}(write|update|change|add|create|edit|remove|delete|build)s?\b//g' <<<"$lower" \
      | grep -qiE "$change"; then
    want=claude-haiku-4-5 effort=none
  # Docs-only: no code path and no implementation area (domain, agent/, code, logic,
  # matching, ...); a mixed docs and code task takes the higher setting below.
  # The topic of a doc ("the telephony docs") and a typo fix in it are not code work.
  elif grep -qiE "$docpath" <<<"$lower" \
    && ! sed -E 's#[^ ]*\.md\b##g; s#\bdocs/[^ ]*##g; s#[a-z_-]+ (docs?|documentation)\b##g
        s#\bfix(es|ing)? (a |the )?(typos?|wording|spelling|links?|sections?|grammar|formatting)\b##g' <<<"$lower" \
      | grep -qiE "$codepath|$opus|alembic|migrations/|$impl"; then
    want=claude-sonnet-5-5 effort=medium
  # A menu or data import is import work, also when it carries prices or allergens.
  elif grep -qiE "$dataimport" <<<"$lower" \
    && ! grep -qiE "$codepath|$opus|$impl" <<<"$lower"; then
    want=claude-sonnet-5-5 effort=medium
  # A file or data transfer is not a call transfer, and how a price looks is not what it is,
  # whichever comes first ("price badge", "badge color for the price").
  elif sed -E 's/\b(file|data|byte|upload|download)[ -]?transfers?\b//g
      s/\b(prices?|fees?|totals?|allergens?|availability|capacity|opening.?hours|öffnungszeiten|delivery.?zones?|zones?|outages?) (typography|banners?|pages?|views?|forms?|filters?|selectors?|titles?|headings?|tabs?|buttons?|cards?|font|colou?r|layout|label|display|style|styling|alignment|badges?|icons?|column|text|pages?|views?|forms?|filters?|selectors?|titles?|headings?|tabs?|buttons?|cards?)\b//g
      s/\b(typography|banners?|pages?|views?|forms?|filters?|selectors?|titles?|headings?|tabs?|buttons?|cards?|font|colou?r|layout|label|display|style|styling|alignment|badges?|icons?|column|text|pages?|views?|forms?|filters?|selectors?|titles?|headings?|tabs?|buttons?|cards?)( [a-z]+){0,2} (for|of) (the |an? )?(prices?|fees?|totals?|allergens?|availability|capacity|opening.?hours|öffnungszeiten|delivery.?zones?|zones?|outages?)\b//g' \
      <<<"$lower" | grep -qiE "$xhigh"; then
    want=claude-opus-5-5 effort=xhigh
  elif grep -qiE "$opus" <<<"$lower"; then want=claude-opus-5-5 effort=high
  # Sonnet work only when no other code path or implementation work is named (api/gui/ and
  # templates are Sonnet's own).
  # Docs-only prompts were taken above, so a docs mention left here sits next to other work.
  elif sed -E 's#[^ ]*\.md\b##g; s#\bdocs/[^ ]*##g; s#\b(readme|docs?)\b##g' <<<"$lower" | grep -qiE "$sonnet" \
    && ! sed -E 's#\bapi/gui/[^ ]*##g; s#[^ ]*\.(html|j2|css|md)\b##g; s#tests? for (the )?existing code##g' <<<"$lower" \
      | grep -qiE "$codepath|$impl"; then
    want=claude-sonnet-5-5 effort=medium
  else
    # No keyword: the table's default, but not for a short line that names no work.
    [ "$(wc -w <<<"$lower")" -ge 4 ] || grep -qiE "$change" <<<"$lower" || exit 0
    want=claude-opus-5-5 effort=high
  fi
fi
# Haiku takes no effort setting: "none" means effort is not compared, but messages keep it.
shown=${effort:-any}
[ "$effort" = none ] && effort=

model=$(jq -r '.model // empty' <<<"$input")
[ -n "$model" ] || model=$(cat "$state/$sid.model" 2>/dev/null)
[ -n "$model" ] || model=$(cat "$HOME/.claude/hook-state/model-router/$sid.model" 2>/dev/null)
if [ -z "$model" ]; then
  transcript=$(jq -r '.transcript_path // empty' <<<"$input")
  [ -f "$transcript" ] && model=$(tac "$transcript" 2>/dev/null \
    | jq -r '(select(.type == "assistant" and (.isSidechain | not)) | .message.model),
             (select(.type == "attachment" and .attachment.type == "model") | .attachment.identity.modelId)
             | select(type == "string" and startswith("claude-"))' 2>/dev/null | head -n 1)
fi
# No model known (SessionStart may omit it, a prompt never carries it): nothing to compare.
if [ -z "$model" ]; then
  [ -n "$route" ] || exit 0
  msg="routing check: the brief asks for $want at effort $shown, but this session's model is not known to the hook yet, so the setting of this thread cannot be checked."
  jq -n --arg m "$msg" '{systemMessage: $m, hookSpecificOutput: {hookEventName: "UserPromptSubmit",
    additionalContext: ("Carry on with the task and name this in your report to the session that started you: " + $m)}}'
  exit 0
fi
# A Fable session asked for by name keeps Fable, at the table's effort for it.
[ -n "${fable_named-}" ] && [ "$(family "$model")" = 4 ] && want=claude-fable-5-1 effort=high
# The live effort first: Claude Code hands the effective level to hooks as CLAUDE_EFFORT,
# and some events carry an effort object; CLAUDE_CODE_EFFORT_LEVEL is the launch value.
have_effort=${CLAUDE_EFFORT:-$(jq -r '(.effort | objects | .level) // (.effort | strings) // empty' <<<"$input")}
have_effort=${have_effort:-$CLAUDE_CODE_EFFORT_LEVEL}

want_f=$(family "$want") have_f=$(family "$model")
# The exact routed model, a suffix such as [1m] allowed. An unknown effort counts as a
# match except where xhigh is wanted: that one is asked about once.
if [[ "$model" == "$want"* ]]; then
  if [ -z "$effort" ] || [ "$have_effort" = "$effort" ]; then [ -z "$route" ] || rm -f "$state/$sid.seen"; exit 0; fi
  [ -n "$have_effort" ] || [ "$effort" = xhigh ] || exit 0
fi

now="session is on $model, effort ${have_effort:-unknown}"
if [ -n "$route" ]; then
  msg="routing check: the brief asks for $want at effort $shown, but this $now."
  # First time: stop before any work, so the starting session can switch this thread
  # (set_session_model / set_session_effort work on a thread it started, never on itself)
  # and send the brief again. Same mismatch after that resend: the switch did not take
  # (fork, cloud) or only half took, so carry on and report it. After that: silent, never
  # a loop. Counted per requested route, whatever setting the thread is on now.
  hash=$(cksum <<<"$want ${effort:-any}")
  seen=$(grep -cx "$hash" "$state/$sid.seen" 2>/dev/null)
  echo "$hash" >> "$state/$sid.seen"
  case "${seen:-0}" in
    0) ctx="Routing mismatch: do not start the task and use no tools. Reply with exactly this one line, then end your turn: ROUTE-MISMATCH want=$want $shown have=$model ${have_effort:-unknown}. The session that started you switches your model and effort and sends the brief again." ;;
    1) ctx="The brief was sent again but this thread still runs on the wrong setting: carry on with the task and name the mismatch in your report. $msg" ;;
    *) exit 0 ;;
  esac
  jq -n --arg m "$msg" --arg c "$ctx" '{systemMessage: $m, hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $c}}'
  exit 0
elif [ "$have_f" -lt "$want_f" ] || { [ "$have_f" = "$want_f" ] && { [[ "$model" != "$want"* ]] || [ "$(level "$have_effort")" -lt "$(level "$effort")" ]; }; }; then
  msg="routing advice: this prompt fits $want at effort ${effort:-none} (CLAUDE.md Model Routing); $now. Switch in the model menu if you want, no need to resend."
else
  msg="routing advice: this prompt fits $want at effort ${effort:-none} (CLAUDE.md Model Routing); $now. A coordinating session hands it to a fresh thread with that model and effort and a Route line; otherwise switching down saves tokens."
fi
# The same advice once per session and state: a person who keeps the setting is not nagged.
hash=$(cksum <<<"$msg")
grep -qxF "$hash" "$state/$sid.advised" 2>/dev/null && exit 0
echo "$hash" >> "$state/$sid.advised"
jq -n --arg m "$msg" '{systemMessage: $m, hookSpecificOutput: {hookEventName: "UserPromptSubmit",
  additionalContext: ("Open your reply with this one line, then carry on with the work (the user may switch, do not wait): " + $m)}}'
