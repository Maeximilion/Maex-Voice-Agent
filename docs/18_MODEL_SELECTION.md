# 18 – Model Selection: Operation Model and Local Test Model

> Version 1.0 · 06.10.2026 · Decisions by Maxi in §6. The final operation model is a
> measurement result (T-5.3), not a choice made in this file.

---

## 1. What the model has to do (derived from the hard rules)

The model behind `agent/llm.py` understands; tools and code decide (CLAUDE.md §2). That
reduces its job to five things, and each one sets a requirement:

| Job | Requirement | Where it is fixed |
|---|---|---|
| Understand a German caller, spoken and transcribed with errors | good German, robust against filler words and broken sentences | docs/05 §1, evals `cases/` and `targets/` |
| Answer with exactly one JSON object: a sentence or a tool call, never both | reliable structured output, no free text around it | docs/05 §5 (T-2.4) |
| Call the right tool instead of answering from its own knowledge | strict instruction following; never a price, time or allergen from memory | rules 1 and 2, evals: 0 guessed items |
| Read back and wait for "ja" before `confirm`; escalate on complaint, cancellation or a request for a human | follows rules over many turns | rules 3 and 5, evals: 0 unconfirmed, 0 missed escalations |
| Be fast | the guest hears an answer < 1.5 s after speaking (`docs/00_PCF.md` §2). Speech recognition and voice take their share, so the model turn needs roughly 0.6 to 0.8 s | G1 measures it |

Further limits:

- **Small context by design (rule 6):** about 1,180 prompt tokens per request (system prompt v2 plus answer format, measured on the T-2.4 branch), compact state instead of a transcript, output capped at `LLM_MAX_OUTPUT_TOKENS=600`. Every small model handles this context size; a large context window is no criterion.
- **Operation: EU processing and a signed data processing agreement (DPA, German AVV)** (`docs/13_DEPLOYMENT.md` §0, assumption E9).
- **Workbench: a local model, invented data only**, never a real call (same place).
- **Swappable:** the core talks the chat completions format over HTTP (`ChatCompletionsLLM`, PR #208). A vendor that speaks it needs only `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`; any other vendor needs a second client class in `agent/llm.py`, nothing outside it.
- **D7 is still open:** if the voice platform runs its own model loop instead of calling our core, the platform's model list limits this choice. For C2 this makes "the platform can call our own model endpoint" a selection criterion.

## 2. What a model costs per call (first principles)

```text
cost per call = turns x (input tokens x input price + output tokens x output price)
```

Assumption until T-2.4 part 2 logs real numbers (`calls.cost_cents`): about 10 model
turns per call (tool hops included), about 1,500 input and 100 output tokens per turn,
so **about 15,000 input and 1,000 output tokens per call**. Month: 30 calls a day,
900 calls (assumption; the call log in `docs/17_ANRUFPROTOKOLL.md` gives the real count).

| Model | Price per million tokens (in / out) | Per call | Per month (900 calls) |
|---|---|---|---|
| Mistral Small | about $0.10-0.20 / $0.30-0.60 | about 0.3 ct | about $3 |
| Gemini Flash-Lite / Flash | about $0.10 / $0.40 up to $0.30 / $2.50, newer Flash generations higher | 0.2 to 1 ct | $2 to $9 |
| GPT-5 mini (not shortlisted) | about $0.25 / $2.00 | about 0.6 ct | about $5 |
| Claude Haiku 4.5 | $1.00 / $5.00 | about 2 ct | about $18 |
| Claude Sonnet 5.5 (not shortlisted) | $2.00 / $10.00 | about 4 ct | about $36 |

Prices are list prices from public sources as of 06.10.2026 and vary between sources and
regions (EU processing can cost a surcharge). Anthropic prices are from Anthropic's model
table; everything else needs a check on the vendor's own price page before a contract.
Prompt caching lowers the input share further: the system prompt is the same on every
turn.

**Conclusion:** even the most expensive candidate costs a few cents per call. The voice
platform bills per minute and dominates the bill. Token price is therefore a tie-breaker,
not the decision; the decision is made by the hard eval metrics, latency and the EU path.

## 3. Operation model: candidates

| | Mistral Small | Claude Haiku 4.5 | Gemini Flash | GPT-5 mini | Claude Sonnet 5.5 |
|---|---|---|---|---|---|
| Shortlist (Maxi, 06.10.2026) | **yes, favourite** | **yes, quality reference** | **yes** | no | no |
| Vendor, EU path | Mistral AI (Paris), EU company, EU hosting | Anthropic via AWS Bedrock (EU region) or Google Vertex AI (EU region); availability of Haiku 4.5 in the EU region to verify | Google Vertex AI, EU region (Frankfurt) | Microsoft Azure OpenAI, EU region | as Haiku |
| DPA | with Mistral | with AWS or Google | with Google | with Microsoft | with AWS or Google |
| Connection to `agent/llm.py` | chat completions, settings only | second client class (Messages API) | chat completions endpoint of Vertex; login by a short-lived Google token instead of a fixed key, a small addition | chat completions, settings only | as Haiku |
| Same model locally | **yes**, open weights (Apache 2.0) as `mistral-small` | no | no | no | no |
| Strengths | cheapest; European; test on the workbench and operation on the same model family | strongest rule following and tool use of the shortlist; good German | very fast, cheap; the same Vertex account could also carry Haiku | mature structured output | highest quality |
| Weaknesses | rule following below Claude/GPT class, to be measured | most expensive of the shortlist; extra adapter; EU region availability open | Google account and token login; untested in this project | US group; reasoning has to be set to minimal for speed; Maxi left it out | cost and latency higher than needed for a phone turn |

**Recommendation:** Mistral Small as the favourite. If it reaches 0 guessed items, 0
unconfirmed transactions and 0 missed escalations on `make eval` and `make eval-targets`,
it wins: smallest model that passes (rule 6), European, and the workbench runs the same
model family. Haiku 4.5 runs in the same comparison as the quality reference: if Mistral
Small fails a hard metric and Haiku passes, the few cents per call buy the rules.
Gemini Flash is the third data point and the cheap fallback if both have a latency
problem.

**How the decision is made (T-5.3):** same eval suite, same prompt version, one model
changed per run (playbook S5), report with hard metrics, accuracy, false escalations,
tokens per case, cost per case and p50/p95 latency of the model turn. The smallest model
with all hard metrics at 0 and latency in budget wins. Before that: T-2.4 part 3
(`--model` in sim and eval runner).

## 4. Local test model (workbench, one agent, free)

Free means: no token cost, the model runs on Maxi's PC (RTX 4080 with 16 GB, 64 GB RAM).
For invented data only (`docs/13_DEPLOYMENT.md` §0). One agent: the workbench serves one
text-phone conversation or one eval run at a time.

| Tier (detected by the script) | Main model | Comparison model | Notes |
|---|---|---|---|
| NVIDIA GPU with 20 GB or more | `mistral-small` (24B, about 14 GB) | `qwen3:14b` (about 9 GB) | Mistral Small fits the GPU completely; same family as the operation favourite |
| NVIDIA GPU with 12 to 16 GB (Maxi: RTX 4080) | `qwen3:14b` (about 9 GB) | `mistral-small` | measured 06.10.2026: next to the Windows desktop Mistral Small spills into RAM, warm 5.3 s against 1.1 s (§5); it stays the comparison model for eval runs where time does not matter |
| NVIDIA GPU with 8 to 12 GB | `qwen3:8b` | `qwen3:4b` | |
| CPU only | `qwen3:4b` | – | slow, failed the answer format in the container test (§5) |

Optional with 64 GB RAM, only on request (`--model qwen3:30b`): a mixture-of-experts model
with about 3B active parameters. It does not fit 16 GB completely; the rest sits in RAM and
it stays usable because few parameters are active per token. Gemma 4 is another candidate
with good German; try it the same way (`--model <tag>`). Which local model the evals use is
decided by `make eval` once T-2.4 part 3 lands, the same way as in §3.

## 5. Workbench setup

`scripts/setup_local_llm.sh` does everything that can be automated:

1. detects WSL or native Ubuntu, the NVIDIA GPU and its memory, the RAM;
2. installs `zstd` and then Ollama with the official installer if Ollama is missing (asks for the sudo password);
3. starts the Ollama server on loopback at `OLLAMA_URL`: the systemd service for the default address, otherwise in the background; every `ollama` command uses the same address;
4. pulls the main and the comparison model of the tier;
5. sends an invented German reservation sentence to `/v1/chat/completions`, the same API the core uses, checks the answer with the same contract as the core (`parse_turn` in `api/agent/llm.py`: one JSON object, field types, exactly one of `say` and `tool`), and prints tokens, cold and warm latency and whether the model runs on the GPU;
6. prints the lines for `.env`.

`--dry-run` prints every step without changing anything. Exit code 0 means set up and the
test answer is valid.

**Measured in the cloud container (06.10.2026, 4 CPU cores, no GPU, Ollama 0.35.1, `qwen3:4b`):**

- Without `"reasoning_effort": "none"` Qwen3 reasons until the output limit and returns an empty answer (87 s, 600 output tokens, no JSON). With it: 12 s cold, 88 output tokens. **The conversation core has to send this parameter too** (open for T-2.4 part 3); models without a reasoning mode ignore it (checked with `qwen2.5:0.5b`).
- `qwen3:4b` and `qwen2.5:0.5b` both answered with a sentence and a tool call at once, which the contract rejects. Small models are not enough; this is why the RTX 4080 tier starts at 14B and 24B.
- The setup on a minimal Ubuntu needs `zstd` before the Ollama installer runs; the script installs it.

**GPU numbers (Maxi's workbench, 06.10.2026, Ubuntu in WSL, RTX 4080 16 GB with 1.6 GB taken by the Windows desktop, 31 GiB RAM visible to WSL, Ollama 0.35.1, same invented sentence):**

| Model | Answer | Tokens in / out | Cold | Warm (median of 3) | Runs on |
|---|---|---|---|---|---|
| `mistral-small` | valid, `check_slot` | 139 / 94 | 19.6 s | 5.3 s | 17 % CPU, 83 % GPU |
| `qwen3:14b` | valid, `check_slot` | 158 / 66 | 14.0 s | 1.1 s | 100 % GPU |

- `mistral-small` does not fit next to the desktop's share of the GPU memory; the spilled part makes it five times slower than `qwen3:14b`. **The workbench uses `qwen3:14b`** (`LLM_MODEL=qwen3:14b` in `.env`); `mistral-small` stays usable for eval runs where time does not matter.
- `mistral-small` filled `reserved_for` with an invented absolute date (`2024-07-20T19:00:00`) for "Samstag"; the test prompt gives no current date. `qwen3:14b` kept the guest's words. The real prompt carries the date; the evals have to show whether this happens there too.
- **WSL memory (keep for later):** WSL sees 31 GiB of the 64 GB RAM (WSL default: half). Enough for `qwen3:14b` and `mistral-small`. Only for `--model qwen3:30b` or larger models raise it first: on Windows create or edit `%UserProfile%\.wslconfig` with

  ```ini
  [wsl2]
  memory=48GB
  ```

  then run `wsl --shutdown` in PowerShell, open Ubuntu again and check with `free -g` (total about 47). The rest stays for Windows.

### Handover for a Claude Code session in Maxi's Ubuntu terminal

Start `claude` in the repository folder in Ubuntu (WSL) and paste:

```text
Task: set up the local test model on this workbench (docs/18_MODEL_SELECTION.md §5).
Read CLAUDE.md, then docs/18_MODEL_SELECTION.md §4-5. Work in this order and report
the printed numbers after each step:
1. git fetch origin && git checkout claude/llm-selection-agents-qgrkmi (or main, once
   the PR is merged) && git pull
2. nvidia-smi. If it fails in WSL: tell me to update the NVIDIA driver on Windows and
   run "wsl --shutdown" in PowerShell; do not install a driver inside Ubuntu.
3. bash scripts/setup_local_llm.sh --dry-run, show me the plan.
4. bash scripts/setup_local_llm.sh. It asks for my sudo password; wait for me.
5. Report per model: answer valid or not, cold and warm latency, "runs on" (GPU share).
   If the main model runs partly on CPU or warm is above 2 s, say so and recommend
   the comparison model if that one runs fully on the GPU.
6. Put the printed LLM_* lines into .env (create it from .env.example if missing).
   Never commit .env.
7. Only invented test data. No real call content into the local model (docs/13 §0).
Do not change code. Write the measured numbers into docs/18_MODEL_SELECTION.md §5
under "GPU numbers" and docs/01_STATUS.md, on a new branch, and open a PR.
```

## 6. Decisions (Maxi, 06.10.2026)

| Question | Decision |
|---|---|
| Local model | automatic by hardware. RTX 4080 (16 GB, 64 GB RAM): `qwen3:14b`, comparison `mistral-small`; swapped after the measurement in §5, first planned the other way round |
| Shortlist for the operation comparison (T-5.3) | Mistral Small, Claude Haiku 4.5, Gemini Flash via Vertex EU |
| Not shortlisted | GPT-5 mini (Azure EU), Claude Sonnet 5.5 (only if every small model fails a hard metric) |
| 1 to 3 agents at the restaurant | schedule plus overflow, see §7 |

Open: the final operation model (T-5.3 measures it), the vendor contracts and DPAs (Maxi,
in chat), D7 together with D1.

## 7. One to three agents at the restaurant

An agent here means one AI call running at the same time as others. Three facts decide
what is possible:

1. **The model is no limit and costs nothing while idle.** A hosted model bills per token; a second and third parallel call cost only their own tokens. There is nothing to switch on or off for the weekend at the model level.
2. **The phone line is the limit today.** The Fritz!Box 6591 has 4 voice channels; a forward takes two and a transfer to the team one more, so the safe limit is **one** AI call at a time (`docs/01_STATUS.md`, phone line). Two or three need forwarding in the provider's network, or the number moved to the voice platform or a SIP provider (C2).
3. **The voice platform may bill per parallel channel.** If it does, a schedule saves money; if it bills per minute only, a schedule protects the team from too many parallel orders.

**Decision: schedule plus overflow.** One setting in the core, `max_concurrent_calls`, with
a weekly schedule: weekdays 1, Friday to Sunday evenings up to 3 (values in the database,
editable on the tablet later). A call above the limit is not taken by the AI; it rings at
the team (rule 5, no call is lost). Built after D1, because it depends on how the platform
signals a new call; proposed as a work package, not part of T-2.4.

## 8. Next steps

1. Maxi runs §5 on the workbench (handover above) and reports the GPU numbers.
2. ~~T-2.4 part 3~~ **built 06.10.2026:** `--model` in sim and eval runner, `LLM_REASONING_EFFORT` (default empty, sent as `reasoning_effort` when set), tool reference in the prompt, `.env.example` documents every `LLM_*` setting with `qwen3:14b` as the workbench example. The cart in the conversation state followed on 07.10.2026: the order a model has understood so far travels in the compact state, so found dishes survive between turns (`docs/05_DIALOG_PROMPTS.md` §5).
3. T-2.4 part 4: first eval run on `qwen3:14b` locally, then the same suite once on `mistral-small` (same family as the operation favourite); prompt work on what they show.
4. T-5.3: hosted comparison of the shortlist with API test accounts and invented data only; report per §3; recommendation to Maxi.
5. C2: "the platform can call our own model endpoint" as a criterion (D7); concurrency limits and per-channel pricing of the platforms.
6. Proposal: work package for `max_concurrent_calls` with schedule (§7), after D1.

Sources (prices, 06.10.2026): Anthropic model table (cached 25.09.2026);
[Mistral pricing overview](https://developer.puter.com/tutorials/mistral-api-pricing/),
[Gemini API pricing](https://www.morphllm.com/gemini-api-pricing),
[OpenAI pricing](https://developers.openai.com/api/docs/pricing.md),
[LLM API pricing comparison](https://www.morphllm.com/llm-api-pricing),
[Ollama on Ubuntu](https://itecsonline.com/post/ollama-ubuntu-nvidia).
