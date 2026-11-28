# 22 – Language choices and measured performance

Assessment of `fbbdadd90249cf23e35d35ccc327b6f9027d44a0` (05.11.2026). This is
source inspection, not a new latency benchmark. Python remains the backend default.
TypeScript is the direction for browser code: Maxi chose the gradual path on
09.11.2026 (#553), and the toolchain exists since 10.11.2026 (section "Browser
code in TypeScript" below).

## What this repository actually runs

Python 3.12 coordinates FastAPI, SQLAlchemy, the conversation loop and the voice
session. Jinja/HTMX render the UI; handwritten JavaScript handles browser events,
including the floor plan; it moves to TypeScript file by file (#626). Python does not
execute those interactions in the browser.

The expensive engines are not all Python: llama.cpp serves the model separately;
Whisper uses CTranslate2 (native C++/CUDA), Piper uses ONNX, and Pydantic's validation
core is Rust. A different orchestration language does not make the same model or
GPU intrinsically faster. There is no evidence that Python was selected because
Claude can write it better than other languages.

Source-level candidates to **measure**, not established defects:

- `api/agent/llm.py` receives a complete model response; the conversation loop may
  make several model/tool hops. Separate model time from Python CPU time.
- The Whisper adapter waits about 700 ms for an utterance end. Reducing this trades
  latency against cutting off the caller; existing audio evals must still pass.
- `api/telephony/session.py` joins speech chunks before playback. Evaluate time to
  audible output and confirmation/read-back guarantees before changing buffering.
- Voice sessions use a worker and queue per call. Measure queuing, CPU contention
  and memory at 1–3 simultaneous simulated calls; do not mistake this architecture
  for proven event-loop blocking or a proven GIL bottleneck.
- Database queries, repeated serialization, network waits and hardware/model
  settings can dominate even when the application code is efficient.

The historical model results in [18](18_MODEL_SELECTION.md) and speech results in
[20](20_VOICE_LAYER.md) describe particular configurations. They are neither fresh
measurements nor the end-to-end caller delay. For example, accelerating a 30 ms
Python step tenfold in a 3,000 ms turn saves only 27 ms overall.

## Language comparison

Qualitative tendencies for comparable implementations, not universal rankings:

| Language/runtime | CPU work and latency | Strength here | Costs and safety limits |
|---|---|---|---|
| Python/CPython | Pure Python loops often slower; native extensions can be fast; synchronous waits still wait | Existing domain/evals, speech/ML ecosystem, rapid maintenance | Gradual typing needs enforcement; CPU-bound Python threads can contend; validate external data |
| TypeScript/Node.js | Runs as JavaScript; JIT can perform well; CPU work can block the event loop | Typed browser logic and shared API shapes | Build step, runtime validation still needed; types are erased, no inherent speed gain over equivalent JS |
| Go | Compiled, good concurrent services; GC and scheduling still affect tails | Small network services if profiling justifies a boundary | New runtime/build and integration; GC is not hard real-time |
| Rust | Native performance, no tracing GC; strong memory/data-race checks in safe code | Narrow proven CPU/audio hotspot or native extension | Higher implementation effort, FFI/unsafe boundaries and integration need care |
| Java/JVM | Strong optimized throughput; warm-up and GC matter | Mature service tooling and concurrency | Memory/runtime operations and a second ecosystem |
| C#/.NET | JIT or AOT, strong throughput; GC characteristics matter | Typed services with mature tooling | Additional runtime/toolchain; no demonstrated project advantage yet |
| C++ | Native performance and precise resource control | Existing inference engines; specialist native libraries | Manual memory/lifetime hazards and maintenance cost; not the default application rewrite |

No language removes authentication, logic, SQL, dependency or deployment risks.
TypeScript and Python both need runtime boundary validation; Rust's memory safety
is valuable but does not prevent a wrong price or an unconfirmed order.

Benchmarks Game compares particular algorithm implementations; TechEmpower compares
web stacks under specified workloads. Neither predicts this voice pipeline's
model, audio, database or concurrency delay. Do not turn a synthetic speed ratio
into a promised customer latency saving. Reproduce project workloads instead.

## Measurement and release gate

[Issue #550](https://github.com/Maeximilion/Maex-Voice-Agent/issues/550), T-10.14,
measures p50/p95 from **end of caller speech to first audible output**. Record full
SHA, environment/hardware/model, scenario/sample counts, failures, cold/warm runs
and 1, 2 and 3 concurrent simulated calls. Attribute turn detection, STT, each
LLM/tool hop, Python CPU versus native work, DB/network waits, queueing, TTS and
audio transport. Preserve span overlap instead of adding durations twice. Reuse
#400's audio evals and coordinate with #56/#398's model/engine comparisons.

The existing tool p95 budget is <300 ms with a local database. The end-to-end
target is **p50 1.5 s and p95 2.5 s** from the end of the caller's speech to the
first audible answer (Maxi, 27.11.2026, by popup on #550).

### Result of 28.11.2026 (#550)

`python -m evals.latency` (the harness, its stages and what it does not model are
in its docstring) in the image of `voice` on the workbench, commit
`ee21403dd9cac42cd21928990d831a20c4b3c21f`: AMD Ryzen 9 7950X3D (32 threads),
one RTX 4080 (16 GB, driver 621.01) shared by the ear and the model, Python 3.12.15,
faster-whisper 1.2.1 with CTranslate2 4.8.3 (large-v3-turbo on the card), Piper
1.8.0 with onnxruntime 1.31.0 (the agent's default voice on the CPU), the model
`qwen3.6-35b-a3b` (`Qwen3.6-35B-A3B-UD-IQ4_XS.gguf`) on llama.cpp `b1-51ce9c1` with
`--parallel 1`, `reasoning_effort` none. Every call is the product's path:
`CallSession` with the service's ear and mouth, `CallHandler` with the model and the
tools on a scratch database with the eval menu. Synthetic only: six eval cases
(`abholung_0001`, `_0058`, `_0076`, `lieferung_0005`, `reservierung_0001`, `_0036`),
their caller lines spoken by `de_DE-kerstin-low` through the modelled line, streamed
on real time with line noise between them. Six rounds per level, one run (24 min).

| Simultaneous calls | Turns answered / not heard | p50 / p95 total | Model share of a turn (p50) | Without the model p50 / p95 | Model per hop p50 / p95 | Turns over 10 s |
|---|---|---|---|---|---|---|
| 1 | 27 / 3 | 5.22 / 7.80 s | 72 % | 1.48 / 1.65 s | 1.99 / 3.51 s | 0 |
| 2 | 52 / 1 | 7.08 / 10.85 s | 78 % | 1.50 / 2.24 s | 2.69 / 4.10 s | 6 |
| 3 | 68 / 3 | 7.60 / 11.44 s | 81 % | 1.41 / 2.29 s | 3.24 / 5.09 s | 15 |

The stages, p50 / p95 in seconds; they are consecutive time stamps on the critical
path and add up to the total, so nothing is counted twice:

| Calls | Turn end | Wait for the ear | Ear | Core: model | Core: tools | Core: Python and DB | Mouth (whole sentence) | Line to first audible frame |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.77 / 0.79 | 0 / 0 | 0.53 / 0.62 | 3.87 / 6.11 | 0.009 / 0.027 | 0.004 / 0.007 | 0.08 / 0.21 | 0.06 / 0.29 |
| 2 | 0.77 / 0.80 | 0 / 0 | 0.50 / 1.28 | 5.44 / 9.43 | 0.011 / 0.030 | 0.004 / 0.008 | 0.10 / 0.25 | 0.06 / 0.28 |
| 3 | 0.77 / 0.81 | 0 / 0.13 | 0.34 / 1.34 | 5.98 / 9.99 | 0.009 / 0.025 | 0.004 / 0.007 | 0.09 / 0.28 | 0.08 / 0.30 |

The handoff from the ear to the core and from the mouth to the line took under 5 ms.
A turn makes 1.9 to 2.0 model requests on average (one to four).

- **The target is missed at every level, and the model is the dominant part, with
  high confidence.** Not one of 147 answered turns was within 2.5 s. The model's
  requests, timed around `next_turn` (HTTP and the client's parsing included), take
  72 to 81 % of a turn. With two and three calls the one slot of the model server
  (`--parallel 1`) queues the calls, and a request likely finds the slot's cache
  holding the other call's conversation (inferred, not measured per request): the
  time per request grows from 2.0 to 3.2 s, and 10 of the 30 calls at two and three went to
  the team, 9 of them because a model request ran past the 10 s turn deadline
  (`model failed, handing over to the team`; rule 5 held, every call ended with the
  team or the agent). The workbench model is not the operation's (#920, docs/18;
  the measured model figures are posted there).
- **Without the model, the path already takes the target's median.** Turn end
  0.77 s (the ear's 0.7 s of silence by design, plus its 32 ms windows), the ear 0.3
  to 0.5 s, the mouth and the line about 0.15 s: 1.41 to 1.50 s p50. The target
  leaves almost nothing for the model unless the turn end or the ear gets shorter
  too (#1104). The ear's p95 grows with simultaneous calls while the wait for its lock stays
  at most 0.13 s: likely the graphics card shared with the model (15.7 of 16 GB
  used), not profiled on the card.
- **Python is not the bottleneck.** The core's own work outside the model and the
  tools is 4 ms p50 (8 ms p95), the tools 9 to 11 ms p50 (under 30 ms p95, inside
  the 300 ms budget), the CPU time of the call's thread 10 ms per turn (20 ms p95).
  The whole process used 0.4, 0.7 and 0.9 cores on average of 32 at one, two and
  three calls, at most 1.7 GB resident; its short peaks of 18 to 22 cores are the
  thread pools of the native engines. No queue, lock contention, memory pressure or
  repeated serialization showed in the session or the core. **No rewrite**: no
  Python hotspot exists to profile, and #551 stays blocked.
- **Joining a sentence before it plays costs little.** The mouth's first piece came
  after 0.05 s, the whole sentence after 0.08 to 0.10 s (p50): streaming the
  pieces would save about 0.04 s.
- **Cold and warm.** A fresh process with a warm model answered its first turn in
  3.47 s, inside the warm first turns (p50 4.76 s at one call): loading the engines
  at the start leaves no penalty. A model that had slept (llama.cpp
  `--sleep-idle-seconds 600` on the workbench) is a different case: twice, at 05:15
  and 05:53 UTC after more than ten idle minutes, the first request took 10.0 s
  while the model loaded again, ran into the 10 s turn deadline, and the call
  went to the team (11.4 and 11.5 s to the handover sentence). The first caller
  after a quiet spell on the workbench reaches the team, not the agent (#1105).
- **Limits.** One run on Maxi's PC with its local model, not the server of G1; the
  hop from Asterisk to the caller's telephone (network, the router, the phone's
  jitter buffer) is not modelled; one synthetic caller voice and six scenarios on
  the eval menu at one instant; 7 of 154 caller lines ("Nein, danke.", "Auf Kaya.",
  "Ja, passt so.") were not heard as an utterance by the ear, a recognition limit
  of short lines known from the audio evals (docs/20 §11, 29.10.2026), counted as
  not heard and left out of the times.

Rerun it on the workbench (the speech kit's model files, the model on port 8080):

    docker compose --profile voice run --rm --no-deps --user "$(id -u):$(id -g)" \
      -e HOME=/tmp -v "$PWD:/app" -w /app \
      -e LLM_BASE_URL=http://host.docker.internal:8080/v1 -e GIT_SHA="$(git rev-parse HEAD)" \
      voice python -m evals.latency --run-dir evals/reports/latency [--calls 1,2,3] [--rounds 6]

[Issue #551](https://github.com/Maeximilion/Maex-Voice-Agent/issues/551), T-10.15,
is low priority, carries `status: blocked`, and is natively blocked by #550.
Before starting even a comparative backend implementation, require all of:

1. An agreed end-to-end target missed reproducibly on representative workloads.
2. A named Python/runtime hotspot with quantified contribution, separated from
   model, SQL, network, native engine and hardware delays.
3. A fair assessment of simpler algorithms, queries, batching, concurrency,
   streaming where safe, and native libraries within the current architecture.
4. Maxi's recorded decision to release a **targeted** comparison, including
   correctness/eval, fallback, maintenance and deployment acceptance criteria.

Closing #550 alone does not release #551. Keep the blocked label and task status
until that evidence and decision are recorded. No evidence means no rewrite;
a native extension or one isolated service may suffice. This gate affects the
language research task, not unrelated merges or normal Python fixes.

## Type checking and browser interfaces

[Issue #552](https://github.com/Maeximilion/Maex-Voice-Agent/issues/552), T-0.12,
builds on closed #249: mypy already runs, but its non-strict configuration does
not check unannotated function bodies by default. Baseline the gaps, then add
annotations and stricter module-level checking in small changes. Do not hide the
baseline with blanket `Any`, ignores or missing-import suppression. Keep runtime
validation and regression tests. Medium priority, absent a concrete severe bug.

**Baseline and first step (11.11.2026, mypy 2.4.1, 511 source files).** Measured
on `main` at ef1c4fc before any change:

| Setting | Errors | Where |
|---|---|---|
| the old configuration | 0 | – |
| `--check-untyped-defs` | 544 in 75 files | only `api/tests`: 258 `union-attr` (attribute access on an Optional row), 86 `operator`, 86 `arg-type`, 61 `index` |
| `--strict` | 9600 in 297 files | about 282 outside `api/tests`; most of all are `no-untyped-def` (5738) and `no-untyped-call` (2728) |

The first step, all in `pyproject.toml` `[tool.mypy]`, which CI and `make lint`
both read:

- `check_untyped_defs` is on for every path. The 75 test modules that still fail
  it are listed by name as the baseline; a module leaves the list in the change
  that makes it pass, and a new module never joins it.
- `api.core`, `api.domain.calls` and `api.domain.delivery` are checked with the
  per-module flags of `--strict`, named one by one: a `strict = true` in an
  override section applied to the whole run in the test of 11.11.2026. Getting
  there needed only the three bare `dict` annotations of `api/core/tool_log.py`.
- No `Any`, no `type: ignore` and no new missing-import suppression were added.

**Second step (11.11.2026): the test baseline is empty.** All 75 test modules
pass `check_untyped_defs`, so its override section is gone and every path is
checked. Most fixes narrow an Optional with `assert x is not None` where the test
already relied on the value; test doubles now match the protocol or class they
stand in for. One production annotation was wrong: `api.db.get_db` returns a
`Generator`, not an `Iterator`. Six `type: ignore` with a named code and two
`cast` were added, each where a test deliberately breaks a type: a bare string
for a card number, a settings field named by a parameter, a method patched on an
instance, an attribute set on a double, a stub call object that only carries
`.state`.

**Third step (11.11.2026): the inner layers are strict.** Counted first with
`mypy --strict` (9064 errors in 296 files, about 280 outside `api/tests`), then
`api.domain` as a whole, `api.models`, `api.schemas`, `api.events`,
`api.agent` and `api.tools` (already clean) joined `api.core` in the strict
list, so the agent's whole path from tool call to table is strict. 75 errors, nearly all
annotations: a JSONB column or payload is `dict[str, Any]` (or a list of
those), the honest type of a JSON value and narrower than the bare `dict` it
replaces; a tuple of SQL filters is `tuple[ColumnElement[bool], ...]`; a value
read with `getattr` or out of a JSON payload is bound to an annotated name
before it is returned. Behaviour stays the same. The strict list also turns on
`possibly-undefined`, which `--strict` leaves off: during this step a
misindented `return` after an `if` passed every other check and failed only
in the evals, and this flag reports it. `no_implicit_reexport` moved
five imports in tests and one in `api/gui/orders_view.py` to the module that
defines the name. One `type: ignore[no-untyped-def]` was added: the wrap
serializer in `api/schemas/menu.py` must stay without a return annotation,
because Pydantic reads it and would replace the documented schema with a free
`dict` (review PR #139). `provision.missing` keeps `Any` for a mapped class that
carries `tenant_id`, which `GuiAccount` does without the `TenantScoped` mixin.

**Fourth step (14.11.2026): the jobs, the operator dashboard and the voice
layer are strict.** `api.jobs`, `api.dashboard` and `api.telephony` joined the
strict list; 37 errors in 18 files. Most were annotations: `array[int]`,
`dict[str, Any]`, `Mapped[Any]`, `CursorResult[*tuple[Any, ...]]` for the
`rowcount` of an UPDATE or DELETE, and a typed name for a value that comes as
`Any` (a JSON body, the `run` that argparse's `set_defaults` stores). Three were
more: `wave.open` gets its default mode `"rb"` spelled out, which selects the
typed reader; `telephony/router.py` takes the `Starlette` app, whose `state`
is typed, so its `type: ignore` went; and `speech_spike.main` binds
`menu_text` to `None` before the branch that reads a recorded run, where it was
unbound but never read. `no_implicit_reexport` moved 16 references in six tests to the
home module of the name (`os`, `httpx`, `SpikeError`, `token_hash`,
`start_call`); `monkeypatch.setattr(os, "getuid", ...)` patches the same module
object as `spike.os` did. The speech spike's tests give the kit its own copy of
`os`, so its root test patches the name by its string path instead. Behaviour
stays the same.

**Fifth step (15.11.2026): `api.kitchen`, `api.ops`, `api.services` and the
top modules `api.config`, `api.db` and `api.main` are strict.** Five errors:
`/health` returns `dict[str, str]`, and `api.config` names its four imports
from `api.core.tokens` as `X as X`, so the ten modules and tests that import
them from there keep doing so under `no_implicit_reexport`.

**Sixth step (15.11.2026): `printbridge` and `sim` are strict.** Four
`no-any-return` errors on a JSON value went to a typed name, and
`api/tests/test_sim_replay.py` reads `end_call` and `ConversationLoop` from
their home modules (`api.domain.calls`, `api.agent.loop`) instead of from
`sim.session`, which only imports them; the patches by name on `sim.session`
stay.

**Seventh step (15.11.2026): `api.gui`, `db`, `evals` and `scripts` are
strict, and so is the package root `api`.** Measured on `main` at 69ef367 with
the new strict list: 222 errors in 37 files (48 in `api/gui`, 8 in `db`, 28 in
`evals`, 97 in `scripts`, 41 in `api/tests`). Most were annotations again: a
JSON value or page context is `dict[str, Any]`, a `gh` runner is
`Callable[[str], list[dict[str, Any]]]`, a value that comes as `Any` (a JSON
body, `scalar_one()`, `getsockname()`, numpy's `tobytes()`) is bound to an
annotated name before it is returned. `_RevalidatedStatic.file_response` spells
out the signature of Starlette's `StaticFiles.file_response`. `_done_when`
asserts `done_at`, which `list_history` only returns when set; the old code
failed on `None` in `to_local` as well. `api/gui/access.py` imports
`same_origin` from `api.core.origin` instead of through `api.gui.session`, and
the 41 test errors were all `no_implicit_reexport`: the tests patch `os`,
`subprocess`, `time`, `SimCall` and `resolve_tenant` on their home modules and
import `is_yes` from `api.agent.consent` and `ProjectError` from
`scripts.project_report`, the same objects as before. One `type: ignore` went
(`scripts/call_log.py`, no longer needed). Behaviour stays the same; the whole suite ran
green against a scratch database before the change was opened.

Every path in `files` is now in the strict list except `api/tests`, which stays
at `check_untyped_defs`: test doubles, monkeypatched callables and fixtures
without annotations are the bulk of the remaining `--strict` errors there, and
annotating them would add noise without catching production bugs. That ends
T-0.12; a new package joins the strict list in the change that adds it.

## Browser code in TypeScript

**Decision (Maxi, 09.11.2026, [#553](https://github.com/Maeximilion/Maex-Voice-Agent/issues/553), T-3.17):**
the gradual path. New browser logic is written in TypeScript and the handwritten
JavaScript is migrated file by file; Jinja2, HTMX and SSE stay, and the backend
stays Python. A separate TypeScript interface that replaces the templates was not
chosen; it can be looked at again with a measured need. TypeScript adds static
checks and compiles to JavaScript: it does not make the same code run faster, and
`any` or a type assertion switches a check off.

**Standing rule (Maxi, 10.11.2026):** what runs in the browser is TypeScript from
now on, and no language moves back. A task that needs or uses controls writes
their logic in `api/gui/ts/`; no new handwritten `.js` file, no new inline script.
New logic in a file not migrated yet migrates that file first or in the same pull
request; a small fix may stay JavaScript. Jinja2 and HTMX keep rendering the
pages, the backend stays Python. The convention is `CLAUDE.md` section 8, the
check is `api/tests/test_browser_code_language.py`, whose list of JavaScript
files not yet migrated only shrinks.

**Toolchain (built 10.11.2026):**

| Part | How |
|---|---|
| Sources | `api/gui/ts/<name>.ts`, one file per script |
| Compiler | `typescript` pinned to an exact version in `package.json` and `package-lock.json`; no bundler, no framework, no other package |
| Build | `npm ci` once, then `npm run build` (`tsc`, settings in `tsconfig.json`) writes `api/gui/static/<name>.js`; `npm run check` only checks types |
| Output | ES modules for `target` ES2020, loaded by `<script type="module">`; one module imports another by its relative URL (`./raumplan-geometry.js`) |
| Committed output | The compiled `.js` is in git. The server image carries no Node, so `make up`, the Dockerfile and the deployment did not change, and a developer who does not touch browser code needs no Node |
| CI | Job `browser` (part of the required check `test`) runs `npm ci` and `npm run build` and fails on a type error or when the committed output differs from a fresh build |
| Strictness | `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`: the set `tsc --init` writes |
| Caching | `/gui/static/` answers with `Cache-Control: no-cache` (`api/gui/__init__.py`): the browser asks again on every use and gets a 304 while the file is unchanged, so a page never runs the script of an older build. the page's address of `raumplan.js` carries `?v=2` once, because a copy cached before this header existed would have been run as a module; the import of the geometry inside it names no number, since TypeScript cannot resolve a specifier with a query (nothing is deployed outside the workbench, so no browser holds an older copy) |
| Updates | Dependabot, ecosystem `npm`, monthly; a compiler bump that changes the output fails CI until `npm run build` ran |

**The rule at the boundaries.** A type describes only what the compiler was told.
Data that comes from outside the program (JSON from `fetch`, SSE payloads, `data-*`
attributes, cookies) enters typed code as `unknown` and passes a small hand-written
check that names what is wrong; no validation library. What cannot be read is
refused whole instead of being drawn from `undefined`. The first case is `toClient`
in `raumplan-geometry.ts`: it reads the server's room document field by field and
refuses a fixture of an unknown kind, which the next save would otherwise delete on
the server. Prices, confirmations and permissions stay decided on the server
(hard rules 1 and 3); a check in the browser never replaces the server's.

**Migrating a file:** move it to `api/gui/ts/`, type it, build, load it as a module
in its template, add its output to `.gitattributes`, and keep the behaviour: a pure
part runs against the old file on random input before the old file goes (done for
the geometry on 10.11.2026: 20,000 rounds over every exported function, identical).

| File | State |
|---|---|
| `raumplan-geometry` | TypeScript since 10.11.2026 |
| `base` | TypeScript since 10.11.2026 (#626), loaded as a module by `base.html` |
| `raumplan` | TypeScript since 10.11.2026 (#626), loaded as a module that imports the compiled geometry |
| `app` | TypeScript since 10.11.2026 (#626), loaded as a module by `betrieb/index.html`. What htmx and the server's `HX-Trigger` hand over in an event's detail is read as `unknown` and checked field by field; a notice that is no text is not shown, and a question whose request cannot be released sends nothing and shows the red bar. Old and new file ran the same 47-step session in a headless browser against a mocked server: page, focus, scroll positions and tones identical in the 45 steps with real events; the two steps with a deliberately empty detail differ as intended (the old file threw and drew an empty note) |
| `dev-console` | TypeScript since 10.11.2026 (#626): the red bar of the simulator console, an inline script before; loaded as a module by `dev/console.html` |
| `passkey` | TypeScript from its first line (16.11.2026, #571): the passkey ceremonies of the operator dashboard, loaded as a module by its sign-in, step-up and factors pages. The options are the server's JSON and go through the browser's own parser; nothing is decided in the browser |

No handwritten script is left in `api/gui/static/`; the list in
`api/tests/test_browser_code_language.py` is empty. No template carries a script
of its own either: the inline script of the dev console (`dev/console.html`,
`ENV=dev` only) is `api/gui/ts/dev-console.ts` since 10.11.2026 (Maxi's word,
#626), so that list is empty too.

**Claude's choices, 10.11.2026** (Maxi asked for the most reliable way): the output
is committed instead of built in a Node stage of the Dockerfile, which would put
Node on every developer's machine because the dev stack mounts the checkout; ES
modules instead of globals, so files import each other without a bundler; ES2020
as the oldest syntax the output uses, so the tablet's browser must be from 2020
or later (to confirm on the pilot's tablet in T-3.5).

## Review signals for both reviewers

Both Claude Code and Codex reviewers inspect affected code for evidenced latency,
CPU/concurrency, memory, serialization, typing and runtime/library constraints.
Separate Python CPU from model/native-engine time, audio buffering, SQL and network
waits. Start with the root cause and a fix within the current architecture; a
language preference or hypothetical speedup is not an inline defect. Apply the
existing severity, concrete-scenario and finding-count rules unchanged.

The working agent collects credible unmeasured signals in measurement issue #550
(location, triggering workload and proposed measurement), rather than creating an
issue or full audit after every push. A measured Python limitation may justify a
targeted comparison only through the blocked task #551 and the evidence/owner
release gate in `docs/22_LANGUAGE_AND_PERFORMANCE.md`. Closing #550 alone does not
release it. TypeScript UI strategy (#553) and gradual mypy hardening (#552) are
separate decisions, not an instruction to rewrite Python. Follow-up review scope
and the flexible three-round rule stay unchanged. Hosted review instructions
cannot guarantee what a bot executes internally.

The rule above is the authoritative one (moved unchanged from `CLAUDE.md`
section 6 on 12.11.2026, #695); `AGENTS.md` and the requests in
[21](21_REVIEW_AUTOMATION.md) refer to it. Inspect affected paths for evidenced
CPU saturation, excessive work/allocations, blocking, queue/concurrency problems,
serialization, typing gaps and runtime/library constraints. The working agent
collects credible unmeasured signals in #550 with location, scenario and proposed
measurement. Reuse that issue rather than opening one per push.

A concrete regression still gets a short English inline finding under the
existing severity and count limits. A language preference or hypothetical speedup
is not a defect. Examples: a measured Python CPU hotspot can justify profiling;
a slow SQL query calls for query evidence; model wait calls for model/hop analysis;
TTS buffering calls for audio timing and safety checks. None alone authorizes a
rewrite. Follow-up reviews keep the existing scope and flexible review-loop rules.
Hosted bot instructions are guidance, not a guarantee of internal execution or cost.

## References

- [TypeScript: erased types](https://www.typescriptlang.org/docs/handbook/2/basic-types.html)
- [Node.js: do not block the event loop](https://nodejs.org/en/learn/asynchronous-work/dont-block-the-event-loop)
- [CTranslate2](https://github.com/OpenNMT/CTranslate2), [Pydantic core](https://github.com/pydantic/pydantic-core)
- [Benchmarks Game](https://benchmarksgame-team.pages.debian.net/benchmarksgame/fastest/python3-node.html)
- [TechEmpower benchmarks](https://www.techempower.com/benchmarks/)
