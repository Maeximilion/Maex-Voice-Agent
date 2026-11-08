# 22 – Language choices and measured performance

Assessment of `fbbdadd90249cf23e35d35ccc327b6f9027d44a0` (05.11.2026). This is
source inspection, not a new latency benchmark. Python remains the backend default.
TypeScript is the requested direction for future browser interfaces; its rollout
is a decision still to make with Maxi and Claude (#553).

## What this repository actually runs

Python 3.12 coordinates FastAPI, SQLAlchemy, the conversation loop and the voice
session. Jinja/HTMX render the UI; handwritten JavaScript handles browser events,
including the floor plan. There is no TypeScript toolchain yet. Python does not
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

[Issue #553](https://github.com/Maeximilion/Maex-Voice-Agent/issues/553), T-3.17,
keeps both approaches available for Maxi and Claude:

| Option | Benefit | Cost / decision needed |
|---|---|---|
| New browser logic in TypeScript; gradually migrate handwritten JS while retaining Jinja/HTMX | Smaller migration, keeps server rendering and existing deployment | Adds build/type checking, temporary mixed code, boundaries still validated |
| Separate TypeScript UI, gradually replacing templates | Component tooling and complex client state can be easier to manage | API/auth/SSE contracts, accessibility, deployment, migration and rollback work |

A third option may emerge during that decision. No framework is selected and no
backend rewrite is implied. TypeScript supplements JavaScript with static checks
and compiles to JavaScript; `strict` helps catch mistakes but assertions and `any`
can bypass it. It does not make identical JavaScript execute faster. Compare
customer/tablet needs, responsiveness, browser support, accessibility, maintenance
and rollout cost before choosing a pilot. Current GUI behavior stays unchanged.

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
