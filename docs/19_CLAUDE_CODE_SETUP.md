# 19 – Claude Code Setup

> The personal Claude Code setup behind this repository: three user-level hooks, the settings that wire them, and how to restore both on Windows and in Ubuntu (WSL). It lives outside the repository in `~/.claude`, so this file is its only backup.
> Version 1.0 · 06.10.2026

---

## 1. What this covers

| Part | Where it lives | In the repository |
|---|---|---|
| Project hook `sync_main.sh` | `.claude/hooks/`, wired in `.claude/settings.json` | yes, versioned and tested |
| User hooks `sync-main.sh`, `model-router.sh`, `session-title.sh` | `~/.claude/hooks/` on each machine | no, backed up in section 4 |
| User settings | `~/.claude/settings.json` on each machine | no, the shared part is backed up in section 3 |
| Hook state (stamps, notes already given) | `~/.claude/hook-state/` on each machine, under the home folder so no other local user can prepare it | no, rebuilt by the hooks |
| Memory notes | `~/.claude/projects/` on Windows, linked into Ubuntu | no |

Windows and Ubuntu each have their own copy of the hooks and of the settings. A change on one side has to be repeated on the other. Nothing in this file is secret: the scripts hold no keys, and the settings block below holds none either.

Not backed up here, because it is specific to one machine: the `env` block of the Ubuntu settings (it routes Claude Code through the local proxy) and the `autoMode` allow list.

## 2. The hooks

| Script | Event | What it does |
|---|---|---|
| `sync-main.sh --always` | SessionStart | Fetches and fast-forwards local `main`. Warns when `main` could not be fast-forwarded or has diverged. Wired as an async hook so no session start waits for a fetch; the price is that the session may begin before the update lands and that its warning may not be shown. In this repository the project hook `sync_main.sh` does the same sync before the session starts. |
| `sync-main.sh` | PostToolUse on shell commands | The same, but only after a command that contains `gh pr merge`. |
| `sync-main.sh --branch` | UserPromptSubmit | Tells Claude when the session's branch is behind `origin/main`, once per state of `main` and again after 30 minutes while the branch is still behind. Claude then merges `main` in. The hook never merges. Silent on `main` and on a branch whose remote is gone. Fetches at most every 10 minutes. |
| `model-router.sh` | SessionStart, PostModelSwitch, UserPromptSubmit | Pauses a prompt that looks too big for the running model. Lets every machine-delivered prompt through (routines, background reports, CI events: anything that starts with a tag), because nobody is there to switch the model and resend. Silent in any repository whose `CLAUDE.md` has a "Model Routing" heading, which includes this one. |
| `session-title.sh` | UserPromptSubmit | Tells Claude when the session title should change. In this repository the form is gate, task or topic, branch. It reads the title from the transcript; the desktop app records generated titles there too, a terminal session was not checked. |

A merge from `main` moves the head of an open pull request, so the review of the head commit has to be requested again (`CLAUDE.md` section 6, "Reviews").

## 3. The settings block

This is the shared part of `~/.claude/settings.json`. Merge it into the existing file; do not replace the file with it.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/sync-main.sh --always",
            "timeout": 30,
            "async": true
          },
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/model-router.sh",
            "timeout": 10
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/model-router.sh",
            "timeout": 10
          },
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/session-title.sh",
            "timeout": 10
          },
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/sync-main.sh --branch",
            "timeout": 20
          }
        ]
      }
    ],
    "PostModelSwitch": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/model-router.sh",
            "timeout": 10
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash|PowerShell",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/sync-main.sh",
            "timeout": 30
          }
        ]
      }
    ]
  },
  "enabledPlugins": {
    "ponytail@ponytail": true
  },
  "extraKnownMarketplaces": {
    "ponytail": {
      "source": {
        "source": "github",
        "repo": "DietrichGebert/ponytail"
      }
    }
  },
  "outputStyle": "Concise"
}
```

Claude cannot write this file: the edit is blocked as a change to its own configuration, on Windows and in Ubuntu alike. Claude edits the hook scripts and hands over one command for the settings entry.

## 4. The scripts

All three need `bash`, `git` and `jq` on the path. Git for Windows does not ship `jq`; it is installed separately.

### sync-main.sh

```bash
#!/usr/bin/env bash
# Keeps local main in step with origin/main. Fast-forward only: never touches
# local commits or uncommitted work. Called by Claude Code hooks:
#   SessionStart         -> sync-main.sh --always
#   PostToolUse (shell)  -> sync-main.sh   (acts only after "gh pr merge")
#   UserPromptSubmit     -> sync-main.sh --branch   (tells Claude when the
#                           session's branch is behind origin/main; a hook does
#                           not merge into a branch itself, that can conflict)
if [ "$1" = "--branch" ]; then
  cwd=$(jq -r '.cwd // empty')
  cd "$cwd" 2>/dev/null || exit 0
  branch=$(git branch --show-current 2>/dev/null)
  [ -n "$branch" ] && [ "$branch" != "main" ] || exit 0
  # State lives under the home folder: a shared temp folder can be prepared by another local user.
  state="$HOME/.claude/hook-state/branch-sync"
  mkdir -p "$state"
  key="$state/$(git rev-parse --show-toplevel | cksum | cut -d' ' -f1)"
  # ponytail: fetches at most every 10 minutes; lower -mmin if main moves faster.
  # Its own stamp, not FETCH_HEAD: a fetch of one feature ref refreshes that file and leaves origin/main stale.
  if [ -z "$(find "$key.fetched" -mmin -10 2>/dev/null)" ]; then
    git fetch -q --prune origin 2>/dev/null || exit 0
    touch "$key.fetched"
  fi
  # A branch whose upstream is gone was merged or deleted on the remote: nothing to sync.
  [ -z "$(git config "branch.$branch.merge")" ] || git rev-parse -q --verify '@{u}' >/dev/null 2>&1 || exit 0
  behind=$(git rev-list --count HEAD..origin/main 2>/dev/null)
  [ "${behind:-0}" -gt 0 ] || exit 0
  # Once per state of origin/main and worktree, so a merge Claude has to postpone is not nagged about.
  # ponytail: repeats after 30 minutes while the branch is still behind, because a note can get lost
  # (a prompt another hook blocked never reaches Claude); raise -mmin if that is too often.
  tip=$(git rev-parse origin/main)
  [ "$(cat "$key" 2>/dev/null)" != "$tip" ] || [ -z "$(find "$key" -mmin -30 2>/dev/null)" ] || exit 0
  echo "$tip" > "$key"
  msg="Branch sync check: $branch is $behind commits behind origin/main. Bring it up to date before you continue with the request: use the ccd_host sync_with_base_branch tool if this session has it, otherwise commit your work and run git merge origin/main (a merge, no rebase). Resolve conflicts, and push if the branch has an upstream. If the branch has an open pull request, the merge moves its head, so the review of the head commit has to be requested again."
  jq -n --arg m "$msg" '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $m}}'
  exit 0
fi
if [ "$1" != "--always" ]; then
  grep -q 'gh pr merge' || exit 0
fi

git rev-parse --git-dir >/dev/null 2>&1 || exit 0
git fetch -q --prune origin 2>/dev/null || exit 0

# main is checked out in exactly one worktree (or none); update it there.
dir=$(git worktree list --porcelain | awk '
  /^worktree /{w=substr($0,10)}
  /^branch refs\/heads\/main$/{print w; exit}')
if [ -n "$dir" ]; then
  git -C "$dir" merge -q --ff-only origin/main 2>/dev/null
  # Still behind means the fast-forward was refused; say so instead of drifting.
  behind=$(git -C "$dir" rev-list --count main..origin/main 2>/dev/null)
  [ "${behind:-0}" -gt 0 ] && printf '{"systemMessage":"sync-main: main in %s is %s commits behind origin/main and was not fast-forwarded (uncommitted changes or local commits), check git status"}\n' "$dir" "$behind"
else
  # Refused means local main has commits origin/main lacks; say so instead of drifting.
  git fetch -q origin main:main 2>/dev/null \
    || printf '{"systemMessage":"sync-main: local main has diverged from origin/main and was not updated, check git log origin/main..main"}\n'
fi
exit 0
```

### model-router.sh

```bash
#!/usr/bin/env bash
# Model router: pauses a prompt that looks too big for the session's model, so
# the model can be changed in the model menu before any work starts. A hook
# cannot switch models itself. Called by Claude Code hooks:
#   SessionStart, PostModelSwitch -> remember the session's model
#   UserPromptSubmit              -> block once when a bigger model fits,
#                                    one hint per session when a smaller one would do
# The patterns are a keyword heuristic and nothing more: tune them here.
complex='\b(architektur|architect|refactor|debug|multi-file|design|security|complex)'
simple='\b(rename|typo|format|lookup|simple|explain this line)'

input=$(cat)
sid=$(jq -r '.session_id // empty' <<<"$input")
[ -n "$sid" ] || exit 0
# State lives under the home folder: a shared temp folder can be prepared by another local user.
state="$HOME/.claude/hook-state/model-router"
mkdir -p "$state"

case "$(jq -r '.hook_event_name // empty' <<<"$input")" in
  SessionStart)    jq -r '.model // empty' <<<"$input" > "$state/$sid.model"; exit 0 ;;
  PostModelSwitch) jq -r '.to_model // empty' <<<"$input" > "$state/$sid.model"; exit 0 ;;
esac

prompt=$(jq -r '.prompt // ""' <<<"$input")
# Machine-delivered prompts (routines, background reports, messages from other sessions,
# CI events) arrive as a tagged block: nobody is there to switch the model and resend.
case "$prompt" in "<"*) exit 0 ;; esac
# A repo with its own "Model Routing" table in CLAUDE.md picks the model itself; stay out of its way.
root=$(git -C "$(jq -r '.cwd // "."' <<<"$input")" rev-parse --show-toplevel 2>/dev/null)
grep -qs '^#\+ Model Routing' "$root/CLAUDE.md" && exit 0
if grep -qiE "$complex" <<<"$prompt"; then
  want=opus
elif grep -qiE "$simple" <<<"$prompt"; then
  want=haiku
else
  exit 0
fi

# Current model: what the session hooks recorded, else the newest model entry in the transcript.
# ponytail: stale if a switch was not reported; sending the prompt again passes anyway.
model=$(cat "$state/$sid.model" 2>/dev/null)
if [ -z "$model" ]; then
  transcript=$(jq -r '.transcript_path // empty' <<<"$input")
  [ -f "$transcript" ] && model=$(tac "$transcript" 2>/dev/null \
    | jq -r 'select((.type == "assistant" and (.isSidechain | not)) or .attachment.type == "model")
             | (.message.model // .attachment.identity.modelId)
             | select(type == "string" and startswith("claude-"))' 2>/dev/null | head -n 1)
fi
case "$model" in
  *haiku*) have=1 ;; *sonnet*) have=2 ;; *opus*) have=3 ;; *fable*) have=4 ;; *) have=0 ;;
esac

if [ "$want" = haiku ]; then
  [ "$have" -gt 1 ] && [ ! -e "$state/$sid.hinted" ] || exit 0
  touch "$state/$sid.hinted"
  jq -n --arg m "model-router: this looks like a small task, Haiku would be enough (session is on $model)." \
    '{systemMessage: $m}'
  exit 0
fi

[ "$have" -lt 3 ] || exit 0
hash=$(cksum <<<"$prompt")
[ "$(cat "$state/$sid.blocked" 2>/dev/null)" != "$hash" ] || exit 0

if [ "$have" -eq 0 ]; then
  jq -n '{systemMessage: "model-router: this looks like an Opus-level task and the session model is unknown, check the model menu."}'
  exit 0
fi
echo "$hash" > "$state/$sid.blocked"
jq -n --arg m "$model" --arg p "$prompt" '{decision: "block", reason:
  ("model-router: this looks like an Opus-level task and the session is on \($m). "
   + "Switch in the model menu, then send the prompt again. "
   + "Sending it again unchanged continues on \($m).\n\nYour prompt:\n\($p)")}'
```

### session-title.sh

```bash
#!/usr/bin/env bash
# Session title: tells Claude when the session's title should change, because a
# hook cannot rename a session itself. Called by Claude Code on UserPromptSubmit.
#   repo with a "Next gate" in docs/01_STATUS.md -> "<gate> - <task ID or topic> - <branch>"
#   task ID readable from the branch (task/9-3-x, task/T-2.4-x) -> that exact title
#   anywhere else -> one reminder if the first title is still there after $after prompts
# Speaks once per wanted title and is silent everywhere else.
after=5

input=$(cat)
sid=$(jq -r '.session_id // empty' <<<"$input")
cwd=$(jq -r '.cwd // empty' <<<"$input")
transcript=$(jq -r '.transcript_path // empty' <<<"$input")
[ -n "$sid" ] && [ -f "$transcript" ] || exit 0
case "$(jq -r '.prompt // ""' <<<"$input")" in "<scheduled-task"*) exit 0 ;; esac

# Every title the session has had, oldest first; the app records each one in the transcript.
titles=$(grep '^{"type":"custom-title"' "$transcript" | jq -r '.customTitle // empty' 2>/dev/null | uniq)
current=$(tail -n 1 <<<"$titles")
[ -n "$current" ] || exit 0

# State lives under the home folder: a shared temp folder can be prepared by another local user.
state="$HOME/.claude/hook-state/session-title"
mkdir -p "$state"
how='Rename it with the mcp__ccd_session_mgmt__set_session_title tool (session_id "self"; load it with ToolSearch if it is deferred), then carry on with the request. If that tool does not exist in this session, ignore this note.'

branch=$(git -C "$cwd" branch --show-current 2>/dev/null)
root=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)
gate=$(head -n 10 "$root/docs/01_STATUS.md" 2>/dev/null | grep -m1 -oE 'Next gate: \**G[0-9]+' | grep -oE 'G[0-9]+')
task=
[[ "$branch" =~ ^task/[Tt]?-?([0-9]+)[.-]([0-9]+)(-|$) ]] && task="T-${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"

if [ -n "$branch" ] && [ -n "$gate$task" ]; then
  if [ -n "$task" ]; then
    want="${gate:+$gate - }$task - $branch"
    [ "$current" != "$want" ] || exit 0
    msg="Session title check: this session is on branch $branch, so its title should be \"$want\", but it is \"$current\". $how"
  else
    case "$current" in "$gate - "*" - $branch") exit 0 ;; esac
    want="$gate - <task ID or topic> - $branch"
    msg="Session title check: the title should have the form \"$want\", but it is \"$current\". Use the task ID (T-x.y) if this session works on a task from docs/07_WORKPACKAGES.md, otherwise two to four words for the topic. If the topic is not clear yet, do it as soon as it is. $how"
  fi
  [ "$(cat "$state/$sid.asked" 2>/dev/null)" != "$want" ] || exit 0
  echo "$want" > "$state/$sid.asked"
else
  # ponytail: one reminder per session; a later change of topic relies on Claude noticing it.
  n=$(($(cat "$state/$sid.count" 2>/dev/null || echo 0) + 1))
  echo "$n" > "$state/$sid.count"
  [ "$n" -eq "$after" ] && [ "$(wc -l <<<"$titles")" -eq 1 ] || exit 0
  msg="Session title check: after $after prompts this session still has its first title \"$current\". If that no longer says what the session is about, give it a short, specific title. $how"
fi
jq -n --arg m "$msg" '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $m}}'
```

## 5. Restore

### Windows

1. Save the three scripts from section 4 into `~/.claude/hooks/` under their names.
2. Merge the block from section 3 into `~/.claude/settings.json` in an editor.
3. Check the result. Console: Git Bash, any folder.

```bash
for f in sync-main.sh model-router.sh session-title.sh; do bash -n ~/.claude/hooks/$f || echo "broken: $f"; done; jq -c '.hooks | map_values(map(.hooks | map(.command)))' ~/.claude/settings.json
```

The check passed when it prints the four hook groups and no `broken:` line. A settings file with broken JSON switches off everything in it without a message, so this check is not optional.

### Ubuntu

Ubuntu takes the scripts and the shared settings keys of section 3 from the Windows copy and keeps everything else in its own file, the `env` block included. Console: Ubuntu terminal, any folder. The first line finds the Windows user folder by itself. The commands must not be prefixed with `wsl`; that program only exists on the Windows side.

```bash
WIN_CLAUDE="$(wslpath "$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')")/.claude"
mkdir -p ~/.claude/hooks
[ -f ~/.claude/settings.json ] || echo '{}' > ~/.claude/settings.json
for f in sync-main.sh model-router.sh session-title.sh; do tr -d '\r' < "$WIN_CLAUDE/hooks/$f" > ~/.claude/hooks/$f; bash -n ~/.claude/hooks/$f || echo "broken: $f"; done
cp ~/.claude/settings.json "$(mktemp ~/.claude/settings.json.bak.XXXXXX)"
new=$(jq --slurpfile win "$WIN_CLAUDE/settings.json" '. + ($win[0] | {hooks, enabledPlugins, extraKnownMarketplaces, outputStyle} | with_entries(select(.value != null)))' ~/.claude/settings.json) && printf '%s\n' "$new" > ~/.claude/settings.json
jq -c '{env: has("env"), plugins: (.enabledPlugins | keys), hooks: (.hooks | keys)}' ~/.claude/settings.json
```

The last line has to show the plugin, the four hook groups and no `broken:` line before it, and `"env":true` on a machine that had an `env` block before. Every run copies the settings to a new, uniquely named `settings.json.bak.*` file that only the owner can read, and never overwrites an older one; the oldest holds the state before the first run. Delete them once the check passed. The settings file itself is rewritten in place and keeps its permissions.

## 6. Check that a hook fires

A hook that runs without anything to report leaves no visible trace. For `sync-main.sh --branch`, feed it by hand from a branch that is behind `main`. Console: Git Bash or Ubuntu terminal, inside the checkout.

```bash
jq -n --arg cwd "$PWD" '{cwd: $cwd}' | bash ~/.claude/hooks/sync-main.sh --branch
```

It prints a JSON object with a "Branch sync check" text when the branch is behind and nothing when it is not. The note is given once per state of `main` and repeats after 30 minutes: a second run right away prints nothing, and a run by hand uses up the note the session would have got in that time. Inside a session the sign that it ran is a fresh `.fetched` stamp file for the checkout in `~/.claude/hook-state/branch-sync/`; the hook keeps its own stamp because any other fetch also refreshes `FETCH_HEAD`. A session that was already open when the settings changed may not load the new entry; a new session does.

## 7. Keeping this file true

The scripts in section 4 are copies. Whoever changes a hook in `~/.claude/hooks/` updates this file in the same session and repeats the change on the other machine. State of the copies: 06.10.2026.
