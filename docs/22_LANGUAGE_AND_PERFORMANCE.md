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
| Caching | `/gui/static/` answers with `Cache-Control: no-cache` (`api/gui/__init__.py`): the browser asks again on every use and gets a 304 while the file is unchanged, so a page never runs the script of an older build. `raumplan.js` and `raumplan-geometry.js` carry `?v=2` once, because copies cached before this header existed would have been run as modules |
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
| `raumplan.js` | JavaScript, already a module that imports the compiled geometry (#626) |
| `app.js` | JavaScript, classic script (#626) |

**Claude's choices, 10.11.2026** (Maxi asked for the most reliable way): the output
is committed instead of built in a Node stage of the Dockerfile, which would put
Node on every developer's machine because the dev stack mounts the checkout; ES
modules instead of globals, so files import each other without a bundler; ES2020
as the oldest syntax the output uses, so the tablet's browser must be from 2020
or later (to confirm on the pilot's tablet in T-3.5).

## Review signals for both reviewers

The authoritative rule is in `CLAUDE.md`; `AGENTS.md` and the requests in
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
