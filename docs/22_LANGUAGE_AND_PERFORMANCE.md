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

The existing tool p95 budget is <300 ms with a local database. The voice-to-voice
median <=1.5 s in docs/20 remains **a proposal awaiting Maxi's confirmation**;
do not silently convert it into an approved p95 target.

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

Next steps, one reviewable change each (strict errors outside `api/tests` on
14.11.2026): `api.kitchen`, `api.ops` and `api.services` (already clean) with
the top modules `api.config`, `api.db` and `api.main` (the `api.config`
re-exports), then `api.gui` (50), `printbridge` (3), `sim` (1), `db` (8),
`evals` (28) and `scripts` (97). Count the rest again with `mypy --strict`
before each step.

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
