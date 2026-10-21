# 19 – Claude Code Setup

> The personal Claude Code setup behind this repository: three user-level hooks, the settings that wire them, and how to restore both on Windows and in Ubuntu (WSL). It lives outside the repository in `~/.claude`, so this file is its only backup.
> Version 1.1 · 14.10.2026

---

## 1. What this covers

| Part | Where it lives | In the repository |
|---|---|---|
| Project hook `sync_main.sh` | `.claude/hooks/`, wired in `.claude/settings.json`. At session start it fetches, fast-forwards a clean `main` where it is checked out, moves the branch `main` itself where no folder has it checked out, and puts the main folder back on `main` when the branch it was left on is finished (nothing uncommitted, remote branch gone, and its work in `origin/main`: its commits, or after a squash merge its changes, or a merged pull request with exactly its head commit, which needs `gh`; since 12.10.2026). Where another folder has `main` checked out, or `gh` had to be asked and could not answer, it warns instead of switching. A warning names the files that block the sync, or git's reason where a clean folder could not be fast-forwarded. Called with `--json`, as `.claude/settings.json` does, it goes out as a message the user sees (`systemMessage`) and as an instruction for Claude to raise it first (`additionalContext`); plain stdout of a SessionStart hook reaches Claude only (since 14.10.2026). Codex calls it without the flag and gets plain lines. `/done` runs it again after a merge | yes, versioned and tested |
| Project permission rule `"ask": ["Read(/**/.env)"]` | `.claude/settings.json`. Claude asks before its Read tool opens a file named `.env` at any depth; `.env.example` stays open. It guards the Read tool only: tested headless on 19.10.2026, the Grep tool and `cat .env` still read the file. A `deny` rule would block Read, Grep and `cat` too, but also every edit of `.env` by Claude; Maxi keeps it for after the next gates (`docs/01_STATUS.md`, D23). Not a protection against intent: scripts and `docker compose config` read `.env` on their own | yes, versioned |
| User hooks `sync-main.sh`, `model-router.sh`, `session-title.sh` | `~/.claude/hooks/` on each machine | no, backed up in section 4 |
| Launcher `claude-rc` (Ubuntu only) | `~/.local/bin/claude-rc` | no, backed up in section 4 |
| User settings | `~/.claude/settings.json` on each machine | no, the shared part is backed up in section 3 |
| Hook state (stamps, notes already given) | `~/.claude/hook-state/` on each machine, under the home folder so no other local user can prepare it | no, rebuilt by the hooks |
| Memory notes | `~/.claude/projects/` on Windows, linked into Ubuntu | no |

Windows and Ubuntu each have their own copy of the hooks and of the settings. A change on one side has to be repeated on the other. Nothing in this file is secret: the scripts hold no keys, and the settings block below holds none either.

Not backed up here, because it is specific to one machine: the `env` block of the Ubuntu settings (it routes Claude Code through the local proxy), the `autoMode` allow list, and the usage gate on Windows (`python ~/.claude/usage_gate/gate.py hook` on SessionStart and UserPromptSubmit, since 15.10.2026). Ubuntu has no `python` on its path, so the Ubuntu restore below leaves the usage gate entries out.

## 2. The hooks

| Script | Event | What it does |
|---|---|---|
| `sync-main.sh --always` | SessionStart | Fetches and fast-forwards local `main`. Warns when `main` could not be fast-forwarded or has diverged. Wired as an async hook so no session start waits for a fetch; the price is that the session may begin before the update lands and that its warning may not be shown. In this repository the project hook `sync_main.sh` does the same sync before the session starts, so here it yields to it on startup and resume, the events the project hook is registered for: two fetches at the same moment race for the same ref locks, and one fails (19.10.2026: 18 of 20 test starts warned "git fetch failed"). On `/clear` and compaction it still syncs. |
| `sync-main.sh` | PostToolUse on shell commands | The same, but only after a command that contains `gh pr merge`; the output of a command does not count. A refused fast-forward is reported to the user and to Claude, with the files that block it (since 14.10.2026). Known limit: `gh pr merge --auto` and a merge queue merge later, and nothing runs the hook at that moment; local `main` then follows at the next session start or the next merge command. |
| `sync-main.sh --branch` | UserPromptSubmit | Tells Claude when the session's branch is behind `origin/main`, once per branch and state of `main`, and again after 30 minutes while the branch is still behind. Claude then merges `main` in. The hook never merges. Silent on `main` and on a branch whose remote is gone. Fetches at most every 10 minutes. |
| `model-router.sh` | SessionStart, PostModelSwitch, UserPromptSubmit | Pauses a prompt that looks too big for the running model. Lets every machine-delivered prompt through (routines, background reports, CI events: anything that starts with a tag, except pasted text and a quoted reply, which come from a person), because nobody is there to switch the model and resend. The same goes for every prompt inside a subagent. Silent in any repository whose `CLAUDE.md` has a "Model Routing" heading, which includes this one. |
| `route_advice.sh` (project hook, `.claude/hooks/`) | SessionStart, PostModelSwitch, UserPromptSubmit | Compares the session's model and effort with the Model Routing table in `CLAUDE.md` (keyword heuristic: migration, price, zone, hours, allergen, confirm, escalation and similar raise to Opus 5.5 `xhigh`; hooks, `domain/`, `agent/`, `deploy` and the rest to Opus 5.5 `high`; GUI, docs, seeds, imports to Sonnet 5.5 `medium`; a prompt that names only Markdown or `docs/` paths and no implementation area stays on Sonnet whatever its topic; a pure search or log read (also asked as "can you search ...") without a changing or debugging verb to Haiku 4.5, checked first so its subject does not count; doc file names such as `docs/13_DEPLOYMENT.md` do not count as code areas; no keyword and four words or more to the default). It compares the exact model ID (`claude-opus-5-5`, a suffix such as `[1m]` allowed) and the effort from `CLAUDE_EFFORT` (the live level Claude Code hands to hooks), else the hook input's `effort` where an event carries it, else `CLAUDE_CODE_EFFORT_LEVEL` (the launch value); an unknown effort is only asked about when `xhigh` is wanted. Never blocks: it shows the user one line and tells Claude to open its reply with it. A session stronger than the prompt needs is pointed at a thread instead of a switch. A line `Route: <model-id> <effort>` as the prompt's first line, right after a wrapper tag, or as the first line after the desktop app's `Task:` line (the brief of a thread, `CLAUDE.md` section 6, "Threads") replaces the keywords: the hook then only checks that the thread runs on what the brief asks for, and a Route line without a valid effort is reported as such. On the first mismatch it tells the thread to do no work and answer only `ROUTE-MISMATCH want=<model> <effort> have=<model> <effort>`, so the coordinating chat can switch the thread and send the brief again; on the same mismatch after that resend it lets the thread carry on and report it; after that it is silent; the count is per requested route, so a switch that only half took also carries on. When the hook does not know the model yet (the desktop app's `SessionStart` omits it, so on every session's first prompt), it does not stop the thread: it is silent when the effort is the routed one and otherwise carries on and reports that the setting could not be checked. It never recommends Fable 5.1 (the table uses it only when Maxi asks); a Fable session whose prompt names Fable is left alone unless the prompt withdraws it ("off Fable", "not on Fable"). Transfer counts as xhigh except as a file or data transfer. It runs in a UTF-8 locale, so German umlauts match without `LANG`. A hook cannot switch a model itself. Silent when model and effort match, for `/task` and `/start` (they check the table themselves), a slash command without arguments, subagents, machine-delivered prompts and plain answers such as "ja" or "weiter", and it repeats the same advice only once per session. The user-level `model-router.sh` yields in this repository because `CLAUDE.md` has its own table, so this is the only router here. Since it is a project hook it also runs in threads that do not use `~/.claude` (cloud, Projects) |
| `merge_gate.sh` (project hook, `.claude/hooks/`) | PreToolUse on shell commands | Guards against a forgotten step, not against a session that tries to get around it. Any command that names `gh pr merge` passes only as `gh pr merge <number> --merge --match-head-commit <full sha>`, with `--delete-branch` after it, checked in the folder the command runs in (the hook input's `cwd`, where gh resolves the PR too); a `cd` in front is refused, it could select another repository; the check for the words ignores case, since PowerShell runs `GH` as `gh`; a wrapped call, another strategy, `--auto`, a branch name, and a commit message or search that names the command are blocked with that form (write such text to a file or as `gh pr merg[e]`). Then, for a PR into `main` whose head is that sha: every check passed (a check that ran twice on the head counts by the run that started last), the branch contains `origin/main`, the status version is higher than `main`'s (not for a `chore/readme-weekly-*` branch whose changes are all `README.md`, `.github/assets/*.svg` or `png-sources.json`), and a review is clean: Codex's newest state comment names that head without open findings, or a comment by the owner or a collaborator that opens with `Merge gate: <head sha>`. `onFailure: "block"` makes a timeout or a crash block too; a missing script does not (the settings line checks for it), because a session whose `CLAUDE_PROJECT_DIR` points elsewhere, for example after a move to another folder, would otherwise have every shell command blocked (seen 21.10.2026). Other commands pass after one grep for `merge` (about 0.1 s on Windows). Needs `jq`, `git` and `gh`. Tests: `api/tests/test_hooks_merge_gate.py`. Known limits: `gh api .../merge`, gh aliases and quoting tricks are not caught. |
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
  key="$state/$(git rev-parse --show-toplevel | sha256sum | cut -c1-64)"
  # ponytail: fetches at most every 10 minutes; lower -mmin if main moves faster.
  # Its own stamp, not FETCH_HEAD: a fetch of one feature ref refreshes that file and leaves origin/main stale.
  if [ -z "$(find "$key.fetched" -mmin -10 2>/dev/null)" ]; then
    git fetch -q --prune origin 2>/dev/null || exit 0
    # Named explicitly: the refspec of a single-branch clone leaves main out of a plain fetch.
    git fetch -q origin '+refs/heads/main:refs/remotes/origin/main' 2>/dev/null || exit 0
    touch "$key.fetched"
  fi
  # A branch whose upstream is gone was merged or deleted on the remote: nothing to sync.
  [ -z "$(git config "branch.$branch.merge")" ] || git rev-parse -q --verify '@{u}' >/dev/null 2>&1 || exit 0
  behind=$(git rev-list --count HEAD..origin/main 2>/dev/null)
  [ "${behind:-0}" -gt 0 ] || exit 0
  # Once per branch, state of origin/main and worktree, so a merge Claude has to postpone is not nagged about.
  # ponytail: repeats after 30 minutes while the branch is still behind, because a note can get lost
  # (a prompt another hook blocked never reaches Claude); raise -mmin if that is too often.
  # One note file per branch: switching back and forth must not bring a note back early.
  note="$key.$(printf '%s' "$branch" | sha256sum | cut -c1-16)"
  tip=$(git rev-parse origin/main)
  [ "$(cat "$note" 2>/dev/null)" != "$tip" ] || [ -z "$(find "$note" -mmin -30 2>/dev/null)" ] || exit 0
  echo "$tip" > "$note"
  msg="Branch sync check: $branch is $behind commits behind origin/main. Bring it up to date before you continue with the request: use the ccd_host sync_with_base_branch tool if this session has it, otherwise commit your work and run git merge origin/main (a merge, no rebase). Resolve conflicts, and push if the branch has an upstream. If the branch has an open pull request, the merge moves its head, so the review of the head commit has to be requested again."
  jq -n --arg m "$msg" '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $m}}'
  exit 0
fi
if [ "$1" != "--always" ]; then
  # Only the command counts: output that merely mentions the text must not start a sync.
  jq -r '.tool_input.command // empty' 2>/dev/null | grep -q 'gh pr merge' || exit 0
fi

git rev-parse --git-dir >/dev/null 2>&1 || exit 0
# A repository with its own SessionStart sync runs it at the same moment, and two fetches
# race for the same ref locks: one fails (19.10.2026, 18 of 20 starts warned). Yield to it,
# but only on startup and resume: the project hook's matcher; on clear and compact it does not run.
if [ "$1" = "--always" ] && [ -f "$(git rev-parse --show-toplevel 2>/dev/null)/.claude/hooks/sync_main.sh" ]; then
  src=; [ -t 0 ] || src=$(jq -r '.source // empty' 2>/dev/null)
  case "$src" in startup|resume) exit 0 ;; esac
fi
git fetch -q --prune origin 2>/dev/null || exit 0
git fetch -q origin '+refs/heads/main:refs/remotes/origin/main' 2>/dev/null || exit 0

# main is checked out in exactly one worktree (or none); update it there.
# NUL-separated, so a path with a line break in it stays one path. Git before 2.36 has
# no -z; there the lines of the plain list stand in for the records.
dir=
while IFS= read -r -d '' line; do
  case "$line" in
    "worktree "*) w=${line#worktree } ;;
    "branch refs/heads/main") dir=$w; break ;;
  esac
done < <(git worktree list --porcelain -z 2>/dev/null || git worktree list --porcelain | tr '\n' '\0')
if [ -n "$dir" ]; then
  git -C "$dir" merge -q --ff-only origin/main 2>/dev/null
  # Still behind means the fast-forward was refused; say so instead of drifting.
  behind=$(git -C "$dir" rev-list --count main..origin/main 2>/dev/null)
  if [ "${behind:-0}" -gt 0 ]; then
    # systemMessage reaches the user only, additionalContext Claude only: both, so the
    # warning is neither missed nor left without action (14.10.2026, 192 commits behind).
    event=PostToolUse; [ "$1" = "--always" ] && event=SessionStart
    files=$(git -C "$dir" status --porcelain 2>/dev/null | head -n 10 | sed 's/^ //' | paste -sd ';' - | sed 's/;/; /g')
    jq -n --arg d "$dir" --arg b "$behind" --arg f "${files:-none, local commits block it}" --arg e "$event" \
      '"sync-main: main in \($d) is \($b) commits behind origin/main and was not fast-forwarded (uncommitted files: \($f)), check git status" as $m
      | {systemMessage: $m, hookSpecificOutput: {hookEventName: $e, additionalContext: ($m + ". Say so at the top of your reply, with the files that block it, and get it fixed with Maxi before other work.")}}'
  fi
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
  # A start without a model (after /clear) keeps what is already remembered.
  SessionStart)    m=$(jq -r '.model // empty' <<<"$input"); [ -n "$m" ] && echo "$m" > "$state/$sid.model"; exit 0 ;;
  PostModelSwitch) jq -r '.to_model // empty' <<<"$input" > "$state/$sid.model"; exit 0 ;;
esac

# Nobody can switch the model of a subagent and resend its task: let its prompts through.
[ -z "$(jq -r '.agent_id // empty' <<<"$input")" ] || exit 0
prompt=$(jq -r '.prompt // ""' <<<"$input")
# Machine-delivered prompts (routines, background reports, messages from other sessions,
# CI events) arrive as a tagged block: nobody is there to switch the model and resend.
# Pasted text and a quoted reply also start with "<" and come from a person.
case "$prompt" in "<pasted_content"*|"<!--"*) ;; "<"*) exit 0 ;; esac
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

### claude-rc (Ubuntu only)

Remote Control refuses to start while `ANTHROPIC_BASE_URL` points anywhere but `api.anthropic.com`, and the `env` block of the Ubuntu settings points it at Headroom (`http://127.0.0.1:8787`). A shell variable does not help: the settings file wins over it (checked 15.10.2026), and `--settings` before `remote-control` is refused. So `claude-rc` runs Claude with its own config folder `~/.claude-rc`: login, hooks, plugins, skills, memory and history are links into `~/.claude`, `settings.json` is rebuilt from the Ubuntu settings without `ANTHROPIC_BASE_URL` and without an API key (Remote Control takes only the claude.ai login) at every start, and the daemon and caches stay apart. Session transcripts and memory live under the linked `projects` folder on purpose, so history and memory carry over between both launchers; each session writes its own transcript file. The launcher refuses to run when `~/.claude-rc` is a link or resolves to `~/.claude`, because its settings rewrite would then replace the real `~/.claude/settings.json`. Sessions started through it run without Headroom, so they cost the uncompressed tokens.

```bash
#!/usr/bin/env bash
# Claude Code with Remote Control, without the Headroom proxy.
# Remote Control refuses a custom ANTHROPIC_BASE_URL, and ~/.claude/settings.json sets one
# (Headroom on 127.0.0.1:8787) that a shell variable cannot override. This launcher runs
# Claude with its own config folder ~/.claude-rc: login, hooks, plugins, skills, memory and
# history are links into ~/.claude, and settings.json is rebuilt from ~/.claude/settings.json
# without ANTHROPIC_BASE_URL and API keys at every start. The daemon and caches stay
# separate, so a Headroom session and this one never share a process; session transcripts
# and memory under projects/ are shared on purpose (each session has its own file).
# Usage, inside the checkout (cd ~/Maex-Voice-Agent):
#   claude-rc           interactive session, linked to claude.ai through Remote Control
#   claude-rc server    Remote Control server: claude.ai, the app and projects start sessions here
#   further arguments go to "claude remote-control" after "server", else to claude
set -euo pipefail
# Owner only while the settings are copied (they may hold a credential in their env block);
# Claude itself runs with the caller's usual mask.
old_umask=$(umask)
umask 077
src="$HOME/.claude" dst="$HOME/.claude-rc"
# A ~/.claude-rc that is a link, or the same folder as ~/.claude, would let the settings
# rewrite below replace the real ~/.claude/settings.json.
if [ -L "$dst" ] || { [ -d "$dst" ] && [ "$(cd "$dst" && pwd -P)" = "$(cd "$src" && pwd -P)" ]; }; then
  echo "claude-rc: $dst must be its own folder, not a link to or the same as $src; nothing changed." >&2
  exit 1
fi
mkdir -p "$dst"
chmod 700 "$dst"
# A login refresh replaces the linked .credentials.json with a file of its own. Hand a newer
# login back to ~/.claude and link again, so the normal launcher never keeps a stale token.
# ponytail: only at the next claude-rc start; while both run, a refresh in one stays there until then.
c=.credentials.json
if [ -f "$dst/$c" ] && [ ! -L "$dst/$c" ]; then
  if [ ! -e "$src/$c" ] || [ "$dst/$c" -nt "$src/$c" ]; then cp -p "$dst/$c" "$src/$c"; fi
  rm -f "$dst/$c"
fi
shared=(.credentials.json hooks hook-state plugins projects skills plans agents commands
        history.jsonl CLAUDE.md keybindings.json .ponytail-active ponytail-modes ponytail-statusline.sh)
for name in "${shared[@]}"; do
  # A real file in ~/.claude-rc (Claude replaced a link) is left alone.
  if [ -e "$src/$name" ] && { [ -L "$dst/$name" ] || [ ! -e "$dst/$name" ]; }; then
    ln -sfn "$src/$name" "$dst/$name"
  fi
done
if [ -L "$dst/.claude.json" ] || [ ! -e "$dst/.claude.json" ]; then
  ln -sfn "$HOME/.claude.json" "$dst/.claude.json"
fi
# Remote Control takes only the claude.ai login, no API key. A file of its own per start,
# so two launches at once never move each other's file.
tmp=$(mktemp "$dst/settings.json.XXXXXX")
jq 'del(.apiKeyHelper, .env.ANTHROPIC_BASE_URL, .env.ANTHROPIC_API_KEY, .env.ANTHROPIC_AUTH_TOKEN,
        .env.CLAUDE_CODE_OAUTH_TOKEN, .env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC,
        .env.DISABLE_GROWTHBOOK, .env.CLAUDE_CODE_USE_BEDROCK, .env.CLAUDE_CODE_USE_VERTEX,
        .env.CLAUDE_CODE_USE_FOUNDRY)' "$src/settings.json" > "$tmp"
mv "$tmp" "$dst/settings.json"
umask "$old_umask"

unset ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN \
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC DISABLE_GROWTHBOOK \
      CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY
# Telemetry opt-outs stay as they are (a privacy choice), but where the organization requires
# Trusted Devices they make Remote Control unavailable: say so instead of failing silently.
if [ -n "${DISABLE_TELEMETRY-}${DO_NOT_TRACK-}" ] \
  || jq -e '.env | has("DISABLE_TELEMETRY") or has("DO_NOT_TRACK")' "$dst/settings.json" > /dev/null 2>&1; then
  echo "claude-rc: DISABLE_TELEMETRY or DO_NOT_TRACK is set; if Remote Control does not start, Trusted Devices needs telemetry." >&2
fi
export CLAUDE_CONFIG_DIR="$dst"
if [ "${1-}" = server ]; then
  shift
  exec claude remote-control "$@"
fi
exec claude --remote-control "$@"
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
[ -f ~/.claude/settings.json ] || (umask 077; echo '{}' > ~/.claude/settings.json)
for f in sync-main.sh model-router.sh session-title.sh; do t="$(mktemp ~/.claude/hooks/$f.new.XXXXXX)" && tr -d '\r' < "$WIN_CLAUDE/hooks/$f" > "$t" && bash -n "$t" && chmod 755 "$t" && { [ ! -f ~/.claude/hooks/$f ] || [ -f ~/.claude/hooks/$f.bak ] || cp -p ~/.claude/hooks/$f ~/.claude/hooks/$f.bak; } && mv "$t" ~/.claude/hooks/$f || { rm -f "$t"; echo "broken, kept the old copy: $f"; }; done
tmp=; bak="$(mktemp ~/.claude/settings.json.bak.XXXXXX)" \
  && jq '{hooks, enabledPlugins, extraKnownMarketplaces, outputStyle} | with_entries(select(.value != null))' ~/.claude/settings.json > "$bak" \
  && jq -e . "$bak" > /dev/null \
  && tmp="$(mktemp ~/.claude/settings.json.new.XXXXXX)" \
  && jq --slurpfile win "$WIN_CLAUDE/settings.json" '. + ($win[0] | .hooks |= (if . then map_values(map(.hooks |= map(select(.command // "" | contains("usage_gate") | not))) | map(select(.hooks != []))) | with_entries(select(.value != [])) else . end) | {hooks, enabledPlugins, extraKnownMarketplaces, outputStyle} | with_entries(select(.value != null)))' ~/.claude/settings.json > "$tmp" \
  && jq -e . "$tmp" > /dev/null \
  && chmod --reference="$HOME/.claude/settings.json" "$tmp" \
  && mv "$tmp" ~/.claude/settings.json \
  || { rm -f "$bak" "$tmp"; echo "restore stopped, the settings are unchanged"; }
jq -c '{env: has("env"), plugins: ((.enabledPlugins // {}) | keys), hooks: ((.hooks // {}) | keys)}' ~/.claude/settings.json
```

The last line has to show the plugin, the four hook groups and no `broken` line before it, and `"env":true` on a machine that had an `env` block before. Each script is copied to a new file, checked for syntax and only then moved over the old one, so a damaged Windows copy never replaces a working hook; the script as it was before the first run stays as `<name>.bak`, and a later run keeps that copy instead of overwriting it. Before it rewrites the settings, every run saves the previous values of the four shared keys to a new, uniquely named `settings.json.bak.*` file and reads it back. The backup leaves out everything else, the `env` block included, so nothing sensitive is copied, and it never overwrites an older backup; the oldest holds the state before the first run. The new settings are written to an owner-only file next to the old one, read back, given the permissions of the old file and then moved over it in one step, so the settings file is never half written. A settings file the block has to create is readable by its owner only. When any step of the settings part fails, that part stops, removes what it created and says that the settings are unchanged; a broken script only skips that script.

Undo, same console: put the values of the oldest backup back, written in one step like the restore, and the scripts from their `.bak` copies. Keys and scripts that did not exist before the restore stay: on a machine that had none, delete the three scripts and remove the keys from `~/.claude/settings.json` by hand.

```bash
old="$(ls -tr ~/.claude/settings.json.bak.* | head -n 1)"
tmp="$(mktemp ~/.claude/settings.json.new.XXXXXX)" \
  && jq --slurpfile old "$old" '. + $old[0]' ~/.claude/settings.json > "$tmp" \
  && jq -e . "$tmp" > /dev/null \
  && chmod --reference="$HOME/.claude/settings.json" "$tmp" \
  && mv "$tmp" ~/.claude/settings.json \
  || { rm -f "$tmp"; echo "undo stopped, the settings are unchanged"; }
for f in sync-main.sh model-router.sh session-title.sh; do [ ! -f ~/.claude/hooks/$f.bak ] || mv ~/.claude/hooks/$f.bak ~/.claude/hooks/$f; done
```

Delete the backups once the check passed.

`claude-rc`, same console: `mkdir -p ~/.local/bin`, save the script above as `~/.local/bin/claude-rc`, then `chmod 755 ~/.local/bin/claude-rc`. Ubuntu's `~/.profile` puts `~/.local/bin` on `PATH` at the next login once the folder exists; until then call it as `~/.local/bin/claude-rc`.

### Ubuntu threads for projects and claude.ai

A project in the desktop app (or claude.ai/code) can only start a session inside Ubuntu when a Remote Control server runs there. Console: Ubuntu terminal.

```bash
cd ~/Maex-Voice-Agent && ~/.local/bin/claude-rc server --spawn worktree --no-create-session-in-dir --name ubuntu-workbench
```

The first start asks `Enable Remote Control? (y/n)` once; answer `y`. Each session it starts gets its own git worktree, so two sessions never write in the main folder. The server runs as long as the terminal stays open and the PC is on; `Ctrl+C` stops it. A plain interactive session that can also be followed from claude.ai is `claude-rc` without `server`.

## 6. Check that a hook fires

A hook that runs without anything to report leaves no visible trace. For `sync-main.sh --branch`, feed it by hand from a branch that is behind `main`. Console: Git Bash or Ubuntu terminal, inside the checkout.

```bash
jq -n --arg cwd "$PWD" '{cwd: $cwd}' | bash ~/.claude/hooks/sync-main.sh --branch
```

It prints a JSON object with a "Branch sync check" text when the branch is behind and nothing when it is not. The note is given once per branch and state of `main` and repeats after 30 minutes: a second run right away prints nothing, and a run by hand uses up the note the session would have got in that time. Inside a session the sign that it ran is a fresh `.fetched` stamp file for the checkout in `~/.claude/hook-state/branch-sync/`; the hook keeps its own stamp because any other fetch also refreshes `FETCH_HEAD`. A session that was already open when the settings changed may not load the new entry; a new session does.

### Projects (beta): do hooks load, and do threads get their model

A project in the desktop app is one coordinating chat that starts threads. Whether a project chat and its threads load `.claude/settings.json` and `~/.claude/settings.json` has not been checked yet; this test answers it. Setup: the project has the repository and the local folder `C:\Users\Maxi\Documents\GitHub\Maex-Voice-Agent` as context, and `route_advice.sh` is on `main` (threads start from `main`, cloud threads from the default branch, so a hook only on a branch is invisible to them). Run it once, fill in the results table, and repeat it after an app update that changes projects.

| Step | Where, what to send | Passes when |
|---|---|---|
| 1 | Project chat, model Opus 5.5, effort `xhigh`. Send: `Routing test: read only the docs in README.md, change nothing, and list every routing line you saw in this turn.` | A line `routing advice: this prompt fits claude-sonnet-5-5 at effort medium ... fresh thread ...` shows up, and Claude opens its reply with it. Proves the project chat loads project hooks. If the chat also asks to rename the session, the user-level `session-title.sh` ran too |
| 2 | Same chat. Send: `Start a thread for a docs test on Sonnet 5.5 at effort medium. The thread only runs printenv CLAUDE_EFFORT and reports its model and effort; it changes nothing.` | Claude starts the thread fresh with `model` `claude-sonnet-5-5` and `effort` `medium`, the brief starts with `Route: claude-sonnet-5-5 medium`, the thread reports Sonnet 5.5 and `medium`, and no routing line appears in the thread |
| 3 | Same chat. Send: `Start the same test as a fork, with the brief line Route: claude-sonnet-5-5 medium.` | A fork ignores `model` and `effort`, so the thread runs on Opus 5.5, shows `routing check: the brief asks for claude-sonnet-5-5 ...` and answers only `ROUTE-MISMATCH ...`. The project chat switches the thread to Sonnet 5.5 and `medium` and sends the brief again; the thread then reports Sonnet 5.5 and `medium`. Proves the switch loop. If the project chat has no tool to switch a thread, note it here: then only the report remains |
| 4 | Same chat. Send step 2 again, but as a cloud thread. | Only to learn the cloud's behaviour; threads stay local afterwards. Cloud threads ignore `model` and `effort` at the start as well: `ROUTE-MISMATCH` is expected, then the switch as in step 3. No line at all means project hooks do not load in the cloud |
| 5 | Ubuntu terminal: `cd ~/Maex-Voice-Agent && ~/.local/bin/claude-rc server --spawn worktree --no-create-session-in-dir --name ubuntu-workbench`. Project chat: `Start a thread in Ubuntu (ubuntu-workbench) on Sonnet 5.5 at effort medium: run only uname -a and printenv CLAUDE_EFFORT and report.` | The thread runs in a worktree under `~/Maex-Voice-Agent`, reports `Linux ... microsoft-standard-WSL2`, Sonnet 5.5 and `medium`, and no routing line appears |

If step 1 shows nothing although a plain local session on `main` does, the project chat does not load hooks: say so here, and the coordinating chat has to apply the table on its own (`CLAUDE.md` section 6, "Threads").

Results:

| Date | App version | Step 1 | Step 2 | Step 3 | Step 4 | Step 5 |
|---|---|---|---|---|---|---|
| 2026-10-20 | Desktop 2.42304.2, Claude Code 2.1.299; plain local session, not a project chat | not run | pass for the hand-down: the thread ran on `claude-sonnet-5-5` at `medium`. The hook saw no Route line, fixed (below) | open | open | open |

What the run of 2026-10-20 showed about the hook in the desktop app:

- The app sends a fresh thread its brief behind lines of its own: a `<system-reminder>` block when the thread shares a worktree, then `Background (from the session that started this one):`, the background and a line `Task:`. `route_advice.sh` read the reminder as the first line and stayed silent; it now reads the Route line as the first line after `Task:`. A Route line inside the background stays text, and a second `Task:` line anywhere in the wrapper makes the boundary unknowable, so no Route line is read then (silent, never a wrong stop; Codex PR #342).
- The app's `SessionStart` carries no model, and on a session's first prompt the transcript holds no model entry yet, so the hook knows no model on the first prompt of any session: plain prompts get no advice there, and a thread's brief is checked by its effort only. A routed effort that took counts as a match (the app sets model and effort together at the start); any other effort gives the "cannot be checked" line, never a stop.

## 7. Keeping this file true

The scripts in section 4 are copies. Whoever changes a hook in `~/.claude/hooks/` updates this file in the same session and repeats the change on the other machine. State of the copies: 19.10.2026 (`sync-main.sh`, Windows and Ubuntu), 15.10.2026 (`claude-rc`), 06.10.2026 (the others).
