#!/usr/bin/env bash
# Set up the local test model on the workbench (docs/18_MODEL_SELECTION.md §4).
#
#   bash scripts/setup_local_llm.sh [--dry-run] [--model NAME] [--no-fallback]
#                                   [--skip-install] [--no-smoke]
#
#   --dry-run       detect and print every step, change nothing
#   --model NAME    pull and test this model instead of the one the hardware tier picks
#   --no-fallback   pull only the main model, not the comparison model of the tier
#   --skip-install  never install Ollama, fail if it is missing
#   --no-smoke      skip the test request
#
# Steps: detect WSL or native Linux, the NVIDIA GPU and its memory, the RAM; install
# Ollama with its official installer if it is missing; start it; pull the model the
# hardware tier picks; send one invented German order line per model to the chat
# completions API the conversation core uses (agent/llm.py) and check that one JSON
# object comes back; print cold and warm latency and the lines for .env.
#
# For tests with invented data only. A local model never answers a real call
# (docs/13_DEPLOYMENT.md §0).
#
# Configuration from the environment: OLLAMA_URL (default http://127.0.0.1:11434).
#
# Exit codes: 0 set up and the test answer is valid, 1 a step or the test failed,
# 2 wrong call or unsupported system.
set -euo pipefail

OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
INSTALLER_URL="https://ollama.com/install.sh"
WARM_RUNS=3
SLOW_WARM_SECONDS=2

dry_run=0
model_override=""
with_fallback=1
allow_install=1
smoke=1

die() {
    local code="$1"
    shift
    printf 'setup_local_llm: %s\n' "$*" >&2
    exit "$code"
}

say() { printf '%s\n' "$*"; }

step() { printf '\n== %s\n' "$*"; }

# Run a command that changes the system, or only print it on --dry-run.
run() {
    if [ "$dry_run" -eq 1 ]; then
        printf '   [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            dry_run=1
            shift
            ;;
        --model)
            [ $# -ge 2 ] || die 2 "--model needs a name"
            model_override="$2"
            shift 2
            ;;
        --no-fallback)
            with_fallback=0
            shift
            ;;
        --skip-install)
            allow_install=0
            shift
            ;;
        --no-smoke)
            smoke=0
            shift
            ;;
        *) die 2 "unknown argument: $1 (usage in the head of scripts/setup_local_llm.sh)" ;;
    esac
done
[[ -z "$model_override" || "$model_override" =~ ^[A-Za-z0-9._:/-]+$ ]] ||
    die 2 "--model may only hold letters, digits and . _ : / -"

[ "$(uname -s)" = "Linux" ] || die 2 "runs on Ubuntu or Ubuntu in WSL only"
command -v curl >/dev/null || die 2 "curl is missing: sudo apt install curl"
command -v python3 >/dev/null || die 2 "python3 is missing: sudo apt install python3"

# ---------------------------------------------------------------------------
step "1/5 System"

is_wsl=0
if grep -qi microsoft /proc/version 2>/dev/null; then
    is_wsl=1
    say "   Ubuntu in WSL"
else
    say "   Linux, native"
fi

ram_gib=$(awk '/^MemTotal:/ { printf "%d", $2 / 1024 / 1024 + 0.5 }' /proc/meminfo)
say "   RAM: ${ram_gib} GiB"

vram_mib=0
gpu_name=""
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
    # One line per GPU; the largest one decides the tier.
    while IFS=',' read -r name mem; do
        mem="${mem// /}"
        if [[ "$mem" =~ ^[0-9]+$ ]] && [ "$mem" -gt "$vram_mib" ]; then
            vram_mib="$mem"
            gpu_name="${name}"
        fi
    done < <(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits)
fi
if [ "$vram_mib" -gt 0 ]; then
    say "   GPU: ${gpu_name}, $(((vram_mib + 512) / 1024)) GiB"
else
    say "   GPU: no NVIDIA GPU found, the model runs on the CPU (slow, still fine for tests)"
    if [ "$is_wsl" -eq 1 ]; then
        say "   WSL: the GPU needs only the NVIDIA driver on Windows, no driver inside Ubuntu."
        say "        If the PC has one: update the Windows driver, run 'wsl --shutdown' in"
        say "        PowerShell, open Ubuntu again and check 'nvidia-smi'."
    fi
fi

# ---------------------------------------------------------------------------
step "2/5 Model choice"

# Tiers (docs/18_MODEL_SELECTION.md §4). Main model first, comparison model second.
# Measured on an RTX 4080 under WSL (docs/18 §5): the Windows desktop keeps about
# 1.6 GB of the 16 GB, so mistral-small (about 14 GB) spills into RAM and answers
# five times slower than qwen3:14b. It is the main model only from 20 GB on.
if [ "$vram_mib" -ge 20000 ]; then
    tier="GPU with 20 GB or more"
    main_model="mistral-small"
    fallback_model="qwen3:14b"
elif [ "$vram_mib" -ge 12000 ]; then
    tier="GPU with 12 to 16 GB"
    main_model="qwen3:14b"
    fallback_model="mistral-small"
elif [ "$vram_mib" -ge 7500 ]; then
    tier="GPU with 8 to 12 GB"
    main_model="qwen3:8b"
    fallback_model="qwen3:4b"
else
    tier="CPU only"
    main_model="qwen3:4b"
    fallback_model=""
fi
if [ -n "$model_override" ]; then
    main_model="$model_override"
    fallback_model=""
fi
[ "$with_fallback" -eq 1 ] || fallback_model=""
models=("$main_model")
[ -z "$fallback_model" ] || models+=("$fallback_model")
say "   tier: ${tier}"
say "   main model: ${main_model}"
[ -z "$fallback_model" ] || say "   comparison model: ${fallback_model}"

# ---------------------------------------------------------------------------
step "3/5 Ollama"

# Root (a container) has no sudo; everyone else needs it for apt and systemctl.
sudo_cmd=()
[ "$(id -u)" -eq 0 ] || sudo_cmd=(sudo)

server_up() { curl -fsS --max-time 3 "${OLLAMA_URL}/api/version" >/dev/null 2>&1; }

if command -v ollama >/dev/null; then
    say "   installed: $(ollama --version 2>/dev/null | tail -n 1 | sed "s/^Warning: client //")"
elif [ "$allow_install" -eq 0 ]; then
    die 1 "Ollama is missing and --skip-install is set"
else
    say "   not installed, installing with the official installer (asks for the sudo password)"
    # The installer unpacks with zstd, which a minimal Ubuntu (WSL) lacks.
    if ! command -v zstd >/dev/null; then
        say "   zstd is missing, installing it first"
        run "${sudo_cmd[@]}" apt-get update -qq
        run "${sudo_cmd[@]}" apt-get install -y -qq zstd
    fi
    installer_dir=$(mktemp -d)
    trap 'rm -rf "$installer_dir"' EXIT
    run curl -fsSL "$INSTALLER_URL" -o "${installer_dir}/install.sh"
    run sh "${installer_dir}/install.sh"
fi

if [ -n "${OLLAMA_HOST:-}" ] && [[ "${OLLAMA_HOST}" == 0.0.0.0* ]]; then
    say "   warning: OLLAMA_HOST=${OLLAMA_HOST} opens the model to the local network."
    say "            Unset it; the workbench needs loopback only (docs/13 §2)."
fi

if server_up; then
    say "   server answers at ${OLLAMA_URL}"
else
    if [ -d /run/systemd/system ] && systemctl list-unit-files ollama.service >/dev/null 2>&1; then
        say "   starting the service"
        run "${sudo_cmd[@]}" systemctl enable --now ollama
    else
        # WSL without systemd: start the server in the background, log next to the user.
        say "   no systemd, starting 'ollama serve' in the background (log: ~/.ollama/serve.log)"
        run mkdir -p "${HOME}/.ollama"
        if [ "$dry_run" -eq 1 ]; then
            say "   [dry-run] OLLAMA_KEEP_ALIVE=1h nohup ollama serve >~/.ollama/serve.log 2>&1 &"
        else
            OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-1h}" nohup ollama serve >"${HOME}/.ollama/serve.log" 2>&1 &
        fi
    fi
    if [ "$dry_run" -eq 0 ]; then
        for _ in $(seq 1 30); do
            server_up && break
            sleep 1
        done
        server_up || die 1 "the Ollama server does not answer at ${OLLAMA_URL}"
        say "   server answers at ${OLLAMA_URL}"
    fi
fi

# ---------------------------------------------------------------------------
step "4/5 Download"

if [ "$dry_run" -eq 0 ]; then
    free_gib=$(df -Pk "${HOME}" | awk 'NR == 2 { printf "%d", $4 / 1024 / 1024 }')
    say "   free disk space: ${free_gib} GiB (4080 tier: about 25 GiB for both models)"
fi
for m in "${models[@]}"; do
    say "   ${m}"
    run ollama pull "$m"
done

# ---------------------------------------------------------------------------
step "5/5 Test request"

if [ "$smoke" -eq 0 ] || [ "$dry_run" -eq 1 ]; then
    say "   skipped"
else
    smoke_dir=$(mktemp -d)
    # The installer trap, if set, is replaced: clean both.
    trap 'rm -rf "${installer_dir:-}" "$smoke_dir"' EXIT
    failed=0
    for m in "${models[@]}"; do
        say ""
        say "   model ${m}"
        # Invented guest sentence and a reduced answer format of docs/05 §5. No real data.
        python3 -I - "$m" >"${smoke_dir}/request.json" <<'PY'
import json
import sys

system = (
    "Du nimmst am Telefon eines Restaurants Reservierungen an. Du entscheidest nichts "
    "selbst, du rufst Werkzeuge auf. Antworte mit genau einem JSON-Objekt und sonst "
    'nichts: {"say": <Satz an den Gast oder null>, "tool": <Werkzeugname oder null>, '
    '"args": <Objekt>, "slots": <was der Gast genannt hat>, "not_understood": null}. '
    "Entweder say oder tool, nie beides. Werkzeug: check_slot(party_size, reserved_for)."
)
guest = "Hallo, ich haette gern fuer Samstag um 19 Uhr einen Tisch fuer vier Personen."
print(json.dumps({
    "model": sys.argv[1],
    "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": guest},
    ],
    "temperature": 0,
    "max_tokens": 600,
    "response_format": {"type": "json_object"},
    # Without it Qwen3 reasons until max_tokens and returns an empty answer
    # (measured with Ollama 0.35.1); models without a reasoning mode ignore it.
    "reasoning_effort": "none",
}))
PY
        times=()
        ok=1
        for i in $(seq 0 "$WARM_RUNS"); do
            if ! t=$(curl -fsS --max-time 120 -o "${smoke_dir}/answer.json" -w '%{time_total}' \
                -H 'Content-Type: application/json' \
                --data @"${smoke_dir}/request.json" \
                "${OLLAMA_URL}/v1/chat/completions"); then
                say "   request failed"
                ok=0
                break
            fi
            times+=("$t")
            if [ "$i" -eq 0 ]; then
                # The first answer is the one checked against the format.
                if ! python3 -I - "${smoke_dir}/answer.json" <<'PY'; then
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    body = json.load(f)
msg = body["choices"][0]["message"]
content = msg.get("content") or ""
usage = body.get("usage") or {}
print(f"   tokens: {usage.get('prompt_tokens')} in, {usage.get('completion_tokens')} out")
if msg.get("reasoning") or "<think>" in content:
    print("   note: the model reasons before it answers (slower; switch it off in T-2.4 part 3)")
content = content.split("</think>")[-1].strip()
try:
    turn = json.loads(content)
except ValueError:
    print(f"   answer is no JSON: {content[:200]!r}")
    sys.exit(1)
if not isinstance(turn, dict):
    print("   answer is JSON but no object")
    sys.exit(1)
has_say = bool(turn.get("say"))
has_tool = bool(turn.get("tool"))
if has_say == has_tool:
    print(f"   answer has {'both' if has_say else 'neither'} say and tool: {content[:200]}")
    sys.exit(1)
print(f"   answer: {json.dumps(turn, ensure_ascii=False)[:300]}")
PY
                    ok=0
                    break
                fi
            fi
        done
        if [ "${#times[@]}" -gt 0 ]; then
            say "   cold (loads the model): ${times[0]} s"
        fi
        if [ "${#times[@]}" -gt 1 ]; then
            warm=$(printf '%s\n' "${times[@]:1}" | sort -n | awk '{ a[NR] = $1 } END { print a[int((NR + 1) / 2)] }')
            say "   warm (median of $((${#times[@]} - 1))): ${warm} s"
            if awk -v w="$warm" -v s="$SLOW_WARM_SECONDS" 'BEGIN { exit !(w > s) }'; then
                say "   slower than ${SLOW_WARM_SECONDS} s: fine for evals, too slow as a stand-in for a call"
            fi
        fi
        processor=$(ollama ps 2>/dev/null | awk -v m="$m" 'NR > 1 && index($1, m) == 1 { print $5, $6 }')
        [ -z "$processor" ] || say "   runs on: ${processor}"
        if [[ "$processor" == *CPU* ]] && [ "$vram_mib" -gt 0 ]; then
            say "   part of the model sits in RAM, which makes it several times slower (docs/18 §5)"
        fi
        if [ "$ok" -eq 1 ]; then
            say "   result: valid"
        else
            say "   result: FAILED"
            failed=1
        fi
    done
    [ "$failed" -eq 0 ] || {
        say ""
        say "At least one model failed the test request; see the lines above."
        exit 1
    }
fi

# ---------------------------------------------------------------------------
cat <<EOF

== Done. Lines for .env (text phone and evals on this machine):

LLM_BASE_URL=${OLLAMA_URL}/v1
LLM_MODEL=${main_model}
LLM_API_KEY=
LLM_TIMEOUT_SECONDS=60
LLM_INPUT_CENTS_PER_MTOK=0
LLM_OUTPUT_CENTS_PER_MTOK=0

The test request sends "reasoning_effort": "none". The conversation core has to send
it too (open for T-2.4 part 3), or a reasoning model such as Qwen3 answers empty.

Keep the model in memory between eval cases (the first call loads it, 15 to 20 s):
  systemd:   sudo systemctl edit ollama   ->  [Service]  Environment="OLLAMA_KEEP_ALIVE=1h"
  no systemd: OLLAMA_KEEP_ALIVE=1h ollama serve
Invented test data only. A local model never answers a real call (docs/13 §0).
EOF
