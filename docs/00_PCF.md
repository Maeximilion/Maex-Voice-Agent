# PCF – Maex Voice-Agent

> **AI Call Intake for Delivery, Pickup, and Reservations**
> Version 1.0 · 11.09.2026 · Status: released (Gate P passed)
> This file is the Single Source of Truth for all project chats. Each chat reads it at the start and ends with a handover block (section 12).

---

## 1. Objective

An AI agent answers calls on the landline of <Pilot Operation>. It correctly handles reservations, pickups, and deliveries, hands off to kitchen, register, and team, and escalates complaints to humans. The interface is simple enough that the team operates it under stress without explanation.

### Guiding Principles (First Principles)

Every call goes through 5 links: **Listen → Understand → Verify → Confirm → Hand off.** An error in one link propagates all the way to the kitchen. From this flow six rules follow:

1. **AI understands, code decides.** Prices, zones, hours, availability, and allergens come only from the database.
2. **Never guess.** Without a definitive menu item ID, no item goes in the order. On uncertainty, follow the understanding ladder; at the end, escalate to a human.
3. **Nothing without confirmation.** Read it back, get a "yes", only then send.
4. **Errors are measured.** Progress only via gates with numbers.
5. **Every outage ends with the team.** No call is lost.
6. **Token-efficient by design.** Smallest context, smallest model that passes evals.

### KPIs

> Note: All numbers in this PCF are targets. They will be calibrated in G0 with the team baseline.

| KPI | Definition | Target |
|---|---|---|
| Accuracy | Transactions without later correction | ≥ Team Baseline, Goal ≥ 99% |
| Unconfirmed transactions | to kitchen or reservation book without "yes" | 0 |
| Guessed items | item without definitive menu ID | 0 |
| Escalation rate | calls that reach the team | decreases stage to stage |
| Abandonment rate | customer hangs up before completion | ≤ Baseline |
| Response latency | silence after customer speaks until AI responds | < 1.5 s |
| Cost | € per call and per order | ≤ Budget from G0 |
| Missed calls | calls not answered | → 0 |

---

## 2. Scope

**In scope**
- Reservations, pickups, deliveries (zone, flat fee, minimum order value)
- Inquiries from configuration: hours, delivery area, wait time, allergens
- Complaint or request for human → handoff or callback task
- GUI for operations (tablet) and admin
- Learning phase (offline shadow mode) and eval suite

**Deliberately out of scope (to be revisited later)**
- Payment over the phone: paid on pickup or delivery
- Changes or cancellations to active orders → team
- Additional languages: German first
- Additional locations: architecture allows for it
- Outbound calls initiated by the AI

---

## 3. Decisions

| # | Decision | Rationale | Status |
|---|---|---|---|
| E1 | **Hybrid:** Telephony and speech processing via an EU-hosted specialist service; logic, DB, and GUI in-house | Heavy lifting (real-time audio, latency) is outsourced, error-critical logic built in-house, components remain swappable | set 11.09.2026 |
| E2 | Stages: Reservations → Pickup → Delivery | Each stage introduces exactly one new challenge | Assumption, confirm in G0 via call mix |
| E3 | Rollout: Shadow → Overflow → Primary | Risk grows only with evidence | Assumption, holds until veto |
| E4 | Register stays booking master (TSE); agent DB for agent data only | Legal certainty, single source of truth for revenue | Assumption, holds until veto |
| E5 | Pilot <Pilot Operation>, number stays; menu, hours, zones from DB | Transferable to other locations later | Assumption, holds until veto |
| E6 | Training = knowledge base + eval suite + offline shadow mode, no model fine-tuning | Cheaper, measurable, legally simpler | Assumption, holds until veto |
| E7 | AI discloses itself as AI at the start of the call | EU AI Act Art. 50, in effect since 02.08.2026 | Mandatory |
| E8 | Complaint, human request, cancellation → team immediately, else callback task | Trust, error containment | Assumption, holds until veto |

---

## 4. Architecture (Hybrid)

```text
Caller
  │
  ▼
Landline <Pilot Operation> (existing provider)
  │  Redirect / SIP
  │  Mode: Shadow · Overflow · Primary
  ▼
Voice Platform (EU)
  │  Speech recognition → Model → Voice synthesis
  │  Touch tones · Interruption · Transfer
  │
  │  Tools via HTTPS (hot path)
  ▼
Agent API ─────────► Agent DB (EU)
  │                      ▲
  │  Events              │
  │  (cold path)         │
  ▼                      │
n8n ── Kitchen / Register    │
    ── SMS, Logs, Alarms     │
                         │
GUI (Browser) ───────────┘
  Operations Tablet · Admin

Team Extension ◄── Transfer, Callbacks
EU Server ────────  Transcription of training recordings (nights)
```

### Hot and Cold Path

- **Hot** means: The customer is waiting on the phone. This includes menu search, customer by number, zone check, slot check, and order verification. This must be fast (target < 300 ms per tool) and must never hang → direct queries via a small agent API.
- **Cold** means: Everything after the "yes". This includes receipt/register, SMS, logs, and stats. Can take seconds but needs retry on error → n8n.
- n8n webhooks enter the hot path only if the PoC proves the latency. Measure first, decide later.

### Agent Tools (Draft)

| Tool | Purpose | Deterministic Check |
|---|---|---|
| `get_service_status` | open/closed, delivery on/off, wait time, sold-out dishes | Configuration |
| `find_customer` | name and saved addresses for caller number | only if number provided |
| `search_menu` | top-3 results with ID, price, options | exact number match, else alias table |
| `get_item_details` | options, extras, allergens | DB values only |
| `check_delivery` | zone, flat fee, minimum order value, delivery time | postal code or polygon |
| `check_slot` | table available, else alternatives | capacity per time slot |
| `create_reservation` | reservation as draft | required fields complete |
| `draft_order` | check order, calculate sum, provide read-back text | prices, zone, minimum order, hours |
| `confirm` | after "yes" final → cold path | idempotency: exactly once |
| `create_callback` | callback task with summary | – |
| `transfer_to_team` | transfer to team extension | loop protection |

---

## 5. Stages & Gates

Stages are the timeline, chats are work packages (section 10). Each stage delivers a real, testable end-to-end run.

| Stage | Goal | Gate | Size |
|---|---|---|---|
| 0 Foundation | Facts, rights, budget, vendors | G0 Go/No-Go | M |
| 1 Reservation through-cut | full chain once real | G1 | M |
| 2 Pickup | menu safe to the kitchen | G2 | L |
| 3 Delivery | address and zone without error | G3 | L |
| 4 Learning & shadow measurement | real error rate under conditions, no customer risk | G4 | M |
| 5 Overflow operation | AI only when no one picks up | G5 | M |
| 6 Primary operation & ongoing | AI first, ongoing maintenance | Monthly review | Ongoing |

### Stage 0 – Foundation
**Goal:** Clarify facts, legal framework, budget, and vendors before code begins.
- **C1** Current state capture: register and interface, receipt printer, phone provider and router, call volume and peak times, missed calls, call mix, menu format, reservation workflow, team workflows
- **C1** Measure baseline: errors and complaints per 100 phone orders (2-week tally), missed calls
- **C1** Prepare legal check (section 8), draft announcement texts
- **C2** Vendor shortlist per section 6, cost model, phone path sketch
- **Business case hypothesis:** The biggest lever is missed calls during peak times. Prove it with the call log.

**Gate G0 – Go/No-Go:** Budget approved · Legal framework clarified · Telephony path feasible · Vendor selected for PoC · Stage order confirmed per call mix

### Stage 1 – Reservation Through-cut
**Goal:** The chain test number → AI → tool → DB → GUI runs once completely real. Reservation is the vehicle because it needs few data points and touches no money.
- **C2** PoC on test number: latency, caller ID on redirect, touch tones, transfer, interruption
- **C3** Minimal schema: configuration, hours, capacity, reservations, calls, callbacks
- **C4** Reservation dialog, AI disclosure, human request, callback, system prompt v1
- **C5** Tools `get_service_status`, `check_slot`, `create_reservation`, `create_callback`; call log; error workflow with notification
- **C6** Operations GUI v0: reservations today, callbacks, switch "AI pause"
- **C7 parallel** (after legal approval and testing): Begin recording real team calls with consent. Data collection in background from now on.

**Gate G1:** 20 role-play calls (noise, dialect, interruption, human request) → 100% booked correctly or escalated cleanly · latency on target · cost per call measured · outage test: platform gone → call lands with team

### Stage 2 – Pickup
**Goal:** Menu understood reliably, order reaches kitchen correctly.
- **C3** Menu import (numbers, variants, extras, prices, allergens), alias table, search
- **C4** Order dialog, understanding ladder, allergy rule, read-back + "yes"
- **C4** Eval suite v1 (≥ 100 cases from recordings and role-plays), model choice per eval
- **C5** `draft_order`, `confirm`, handoff to kitchen/register (interface or network receipt), idempotency, retry on error
- **C6** GUI: new orders with "OK/Correct", "dish sold out", wait time slider

**Gate G2:** Eval v1 ≥ target accuracy · 0 guessed items · 0 unconfirmed orders · prices identical to register (reconciliation test) · 30 role-play orders error-free to kitchen

### Stage 3 – Delivery
**Goal:** Address and zone without error.
- **C3** Customers, addresses, zones (postal code or polygons from new delivery area calculation), flat fees, minimum order value
- **C4** Address dialog: "Again at …?" per caller ID, spell street names, house number via keyboard, outside zone → offer pickup
- **C5** `check_delivery`, `find_customer`; SMS summary optional (own gate due to cost and brand impact)
- **C6** GUI: delivery orders, switch "delivery pause"
- **C4** Eval suite v2 with address cases

**Gate G3:** Eval v2 ≥ target · Zone check on boundary cases 100% correct · 20 role-play deliveries correct · deletion concept for customer DB implemented

### Stage 4 – Learning & Shadow Measurement
**Goal:** Measure real error rate under live conditions without customer risk.
- **C7** Transcribe recordings (since stage 1) on EU server (Whisper container or EU service with DPA) → AI extracts transaction as JSON → reconcile with register/receipt
- **C7** Error taxonomy: listening · understanding · menu · address · rule · dialog → fixes in C3/C4
- **C4** Each real error becomes a new eval case; aliases and FAQs enriched from live calls

**Gate G4:** ≥ 200 real calls analyzed · AI extraction ≥ team baseline · no open critical error class (allergens, address, price) · legal check approved

> Offline shadow mode measures **understanding** with a fraction of the tech that live monitoring would need. **Conversation management** is measured by role-plays (G1–G3) and overflow operation (G5).

### Stage 5 – Overflow Operation
**Goal:** AI answers real calls, but only when the team doesn't pick up. Team approves each AI order.
- **C8** Phone mode Overflow (after X seconds ringing or during peak times), team training (15 minutes, 1 page), emergency runbook
- **C6** Approval button per order, tone alarm for callbacks
- **C5** Cost alarm, daily report

**Gate G5:** ≥ 2 weeks and ≥ 100 AI orders · Accuracy ≥ target · Complaints ≤ baseline · Cost ≤ budget · Team feedback positive → approval button can come off

### Stage 6 – Primary Operation & Ongoing
- AI picks up first, team stays reachable via extension
- Before each change to prompt, menu logic, or model, eval suite runs as regression test
- Monthly review: KPIs, costs, new error classes, vendor pricing

---

## 6. Vendor Criteria (C2)

**Must**
- Data processing in the EU, DPA available
- Good German speech recognition and voices
- Tool calls via HTTPS to own logic
- German number or SIP attachment · transfer · touch tones · caller ID to tools
- Customer can interrupt the AI, latency on target
- Recording controllable (consent only)

**Should**
- Custom vocabulary for dish names (keyword boost)
- Model choice flexible, prompt caching
- Transparent cost per minute, low fixed fee
- Enough concurrent calls for Sunday peak
- Export transcripts and call metadata via API
- Test or simulation mode

**Method:** Points matrix → Top 2 → same PoC with same 20 test calls → decision per measurement.

---

## 7. Data Model (Draft for C3)

| Area | Tables |
|---|---|
| Operations | `service_config`, `opening_hours`, `special_days`, `capacity` |
| Menu | `menu_items`, `item_options`, `item_allergens`, `item_aliases` |
| Customers | `customers`, `addresses` |
| Delivery | `delivery_zones` |
| Transactions | `reservations`, `orders`, `order_items`, `callbacks` |
| Quality | `calls`, `eval_cases`, `eval_runs` |
| Audit | `audit_log` |

**Rules**
- Prices in cents as integer, phone numbers in E.164 format
- Each transaction carries a `call_id`
- Recordings and transcripts get a deletion date
- Menu master is the register if exportable; else agent DB plus weekly reconciliation check
- Recommended DB tech: PostgreSQL in the EU (decision in C3)

---

## 8. Legal Check (C1, before first real call)

> Note: Not legal advice. Have a lawyer or data protection consultant review before go-live.

- [ ] **EU AI Act Art. 50:** AI disclosure at call start, in effect since 02.08.2026 ([source](https://www.ai-ops-engine.com/blog/eu-ai-act-digital-omnibus-fristen))
- [ ] **Recording:** Consent from customer and team (§201 StGB); announcement and way to object
- [ ] **GDPR:** Legal basis per use case (order · recording · analysis) · information requirement (brief announcement + privacy policy on example.com) · DPA with all processors · third-country transfer check · deletion concept · processing activity register · data protection impact assessment review
- [ ] **Team:** inform, consent or agreement to recordings
- [ ] **Allergens (LMIV):** Information from maintained DB values only, else team callback
- [ ] **Register (TSE):** AI orders booked properly in register

---

## 9. Risks (Pre-Mortem)

| # | Risk | Mitigation | Stage |
|---|---|---|---|
| R1 | Latency or unnatural voice → customers hang up | Measure latency in PoC, select vendor accordingly | 1 |
| R2 | Dish names or dialect misunderstood | Vocabulary, numbers, keyboard, evals with real recordings | 2 |
| R3 | Internet or platform down during peak | Fallback to team, alarm, "AI pause" | 1 |
| R4 | Recording or AI disclosure legally challenged | Legal check before G0, vetted announcement text | 0 |
| R5 | Menu or prices differ from register | one master, reconciliation test, "dish sold out" | 2 |
| R6 | Wrong allergen information | DB values only, else team callback | 2 |
| R7 | Costs spiral (spam, loops, long calls) | Max duration, loop protection, cost alarm | 1 |
| R8 | Caller ID lost on redirect | Test in PoC; fallback: ask for number | 1 |
| R9 | A change silently degrades quality | Regression evals, versioning | 2 |
| R10 | Team doesn't use the GUI | 2-tap rule, team in role-plays | 1 |
| R11 | Vendor lock-in or price increase | Logic, prompts, evals in-house, tools via webhook | 0 |
| R12 | Double order from retry or network error | Idempotency key per order | 2 |

---

## 10. Chats & Start Prompts

| Chat | Work Package | Stages | Delivers |
|---|---|---|---|
| C0 | Steering | all | Plan, gates, decisions, PCF updates |
| C1 | Current state & legal | 0 | Facts, baseline, legal to-dos, announcement texts |
| C2 | Architecture, telephony & vendors | 0–1, 5 | Vendor choice, cost model, routing, PoC |
| C3 | Data model & agent API | 1–3 | Schema, import, hot-path tools |
| C4 | Dialog, prompts & evals | 1–4 | Conversation flows, system prompt, eval suite |
| C5 | n8n integration | 1–5 | Kitchen/register, callbacks, logs, alarms |
| C6 | GUI | 1–5 | Operations tablet, admin |
| C7 | Learning & quality | 1–4 | Recordings, local transcription, shadow measurement |
| C8 | Rollout & operations | 5–6 | Runbook, training, monitoring, review |

**Sequence:** C1 + C2 parallel → G0 → per stage C3 → C4 → C5 → C6 · C7 runs in background from stage 1 · C8 from stage 5

### How to Work with the Chats
1. New chat in this Claude project → insert start prompt for the package
2. Chat reads the current PCF from Google Drive, or you attach it
3. At the end, chat delivers a handover block → insert into section 13, version +0.1
4. Gates and new decisions go to C0 → C0 updates sections 3 and 5
5. If a chat gets long: Generate handover block and continue in fresh chat

### C1 – Current State & Legal
```text
Project Maex Voice-Agent · Chat C1 – Current State & Legal (Stage 0)
Read the current "PCF – Maex Voice-Agent" first (Google Drive or attachment). E1 and E7 are set, all other decisions hold until veto.
Workflow: code-autopilot · closed questions with marked recommendation, one per interrupt · researchable items researched by you.

Goal: all facts and legal foundations for gate G0.
1. Register: manufacturer, interface or export, network receipt printer?
2. Telephony: provider, PBX or system, lines, redirect possible?
3. Calls: per day, peak times, missed calls, mix of order, reservation, question, complaint
4. Menu: source and format, numbering, variants, allergens maintained?
5. Reservation: current workflow, capacity
6. Team: who picks up? Extension or mobile for transfers?
7. Baseline: measurement plan for errors per 100 phone orders (2 weeks)
8. Legal: work through PCF section 8, draft announcement texts (AI disclosure, recording consent), collect open items for lawyer or data protection consultant
9. Budget: ceiling per month ongoing and one-time

Output: Facts list · baseline measurement plan · announcement texts · legal to-dos
End: handover block per PCF section 12.
```

### C2 – Architecture, Telephony & Vendors
```text
Project Maex Voice-Agent · Chat C2 – Architecture, Telephony & Vendors (Stage 0–1)
Read the current "PCF – Maex Voice-Agent" first (Google Drive or attachment). E1 Hybrid is set.
Workflow: code-autopilot · closed questions with marked recommendation · contracts, costs, and API keys only after approval.

Goal: choose voice platform and get first test call through.
1. Research current vendors and score per PCF section 6 → Top 2
2. Cost model: calls per day × avg minutes × € per minute + fixed costs (numbers from C1)
3. Phone routing: modes shadow/overflow/primary, team extension, loop protection, outage fallback, recording path
4. Hosting in EU for agent API, DB, and n8n
5. PoC on test number: latency, caller ID on redirect, touch tones, transfer, interruption

Output: Scoring matrix · cost model · routing sketch · PoC log
Gate: Vendor and hosting approved (part of G0)
End: handover block per PCF section 12.
```

### C3 – Data Model & Agent API
```text
Project Maex Voice-Agent · Chat C3 – Data Model & Agent API (Stage 1–3)
Read the current "PCF – Maex Voice-Agent" first (Google Drive or attachment), especially sections 4 and 7.
Workflow: code-autopilot build loop · ideally implementation in Claude Code so everything runs real.

Goal: agent DB and fast tools for hot path, stage by stage.
Stage 1: configuration, hours, capacity, reservations, calls, callbacks · decision on DB tech
Stage 2: menu, options, allergens, aliases · import from register or menu · search
Stage 3: customers, addresses, delivery zones · zone check (postal code or polygon)
Rules: prices in cents · E.164 · audit_log · deletion dates · versioned migrations · backup before each change

Gate per stage: tool response < 300 ms · tests for boundary cases green
End: handover block per PCF section 12.
```

### C4 – Dialog, Prompts & Evals
```text
Project Maex Voice-Agent · Chat C4 – Dialog, Prompts & Evals (Stage 1–4)
Read the current "PCF – Maex Voice-Agent" first (Google Drive or attachment). The guiding principles in section 1 are binding.
Workflow: code-autopilot prompt workshop · one variable per iteration · each prompt version with eval run.

Goal: conversation flows, system prompt, tool descriptions, and eval suite that pass G1 through G3.
Dialog: greeting with AI disclosure · intent recognition · required info per transaction · understanding ladder (ask → spell → keyboard → SMS → callback) · read-back + "yes" · allergens from DB only · complaint, human request, cancellation → team · max duration
Tokens: short system prompt · menu index not full menu · details per tool · compact order status not history · smallest model that passes evals
Evals: case = transcript → expected JSON · metrics from PCF section 1 · regression before each change
Decision in stage 1: voice natural or synthetic

End: handover block per PCF section 12.
```

### C5 – n8n Integration
```text
Project Maex Voice-Agent · Chat C5 – n8n Integration (Stage 1–5)
Read the current "PCF – Maex Voice-Agent" first (Google Drive or attachment), especially section 4.
Workflow: code-autopilot · n8n self-hosted via Docker · Python only where n8n doesn't reach.

Goal: cold path runs reliably. Confirmed transactions land in kitchen and register, callbacks with team.
Contents: platform webhooks · kitchen/register (interface or network receipt) · idempotency · retry and error workflow with notification · cost and outage alarm · daily report · SMS optional (own gate)

Gate per stage: end-to-end test · outage test (platform, DB, or n8n gone)
End: handover block per PCF section 12.
```

### C6 – GUI
```text
Project Maex Voice-Agent · Chat C6 – GUI (Stage 1–5)
Read the current "PCF – Maex Voice-Agent" first (Google Drive or attachment).
Workflow: code-autopilot · ideally implementation in Claude Code.

Goal: one surface the team operates under stress without explanation.
Operations (tablet): new orders with OK/Correct · reservations today · callbacks with tone · switches: AI pause, delivery pause, wait +15/+30, dish sold out
Admin (PC): menu and aliases · hours and special days · zones · call log · KPIs and costs · eval results · delete data
Rules: each action ≤ 2 taps · large font · no jargon · live updates

Gate: tech decision in stage 1 · one team member operates the operations view 5 minutes without explanation
End: handover block per PCF section 12.
```

### C7 – Learning & Quality
```text
Project Maex Voice-Agent · Chat C7 – Learning & Quality (from stage 1, measurement in stage 4)
Read the current "PCF – Maex Voice-Agent" first (Google Drive or attachment). Start only after legal approval (section 8).
Workflow: code-autopilot · processing only on EU server, nothing on Maxi's PC.

Goal: learn from real calls and measure real error rate without customer risk.
Pipeline: recording with consent → transcription on EU server → AI extracts transaction as JSON → reconcile with register/receipt → error taxonomy → new aliases, FAQs, and eval cases → delete after period

Gate G4: ≥ 200 calls · AI ≥ team baseline · no open critical error class
End: handover block per PCF section 12.
```

### C8 – Rollout & Operations
```text
Project Maex Voice-Agent · Chat C8 – Rollout & Operations (Stage 5–6)
Read the current "PCF – Maex Voice-Agent" first (Google Drive or attachment). Prerequisite: G1 through G4 passed.
Workflow: code-autopilot · each switch with a way back.

Goal: safe transition from overflow to primary and stable operations.
Contents: mode switch and time windows · team training (15 minutes, 1 page) · emergency runbook (platform, internet, DB gone) · monitoring and KPIs · remove approval button after G5 · monthly review

Gate: G5, then monthly review
End: handover block per PCF section 12.
```

---

## 11. Repo & Versioning

```text
maex-voice-agent/
├── api/             tools in hot path
├── db/migrations/   versioned schema changes
├── n8n/             workflow exports with date and version
├── prompts/         system prompt per version
├── evals/           test cases and scoring
├── gui/
├── docker-compose.yml
├── .env.example
└── README.md
```

- Conventional commits, `main` stays runnable
- `.env`, real recordings, and customer data never go in repo
- Each prompt version gets an eval run, result in commit

---

## 12. Handover Block (Template for Each Chat End)

```text
## Handover <Date> – C<Nr> <Topic>
Status: <what runs, what doesn't>
Artifacts: <files, links>
Decisions: <E-Nr · decision · rationale>
Gate: <passed | open + what's missing>
Open: <next concrete step first>
Lessons: <learnings>
```

---

## 13. Handovers (newest first)

```text
## Handover 16.09.2026 – T-0.1 through T-0.6, T-1.1 through T-1.4
Status: Stack running (Postgres, API, n8n), schema 001 with ten stage-1 tables, seed idempotent,
       tools get_service_status and check_slot with p95 10–14 ms; 89 tests green, CI green.
       Write tools (create_reservation, confirm, create_callback, transfer_to_team), call log,
       prompt, agent core, and GUI still missing.
Artifacts: PR #1 and #2 merged to main (main = df7c071). Branch claude/new-session-c3waic = main.
       api/core, api/db.py, api/models, api/domain/{status,reservations}, api/tools, db/, scripts/seed.py.
Decisions: placeholders instead of names (CLAUDE.md §1) · recommendations adopted directly (§6) ·
       operations day starts at 05:00 · domain errors HTTP 200 in envelope, auth only 401 · capacity without dwell,
       slots = seat turns, drafts count · seed values placeholders until C1 · seed never overwrites live switch.
Gate: G0 open. Missing: vendor (C2), legal check, budget, current state (C1).
Open: T-1.5 create_reservation (draft, readback, idempotency, core/ids.py) → T-1.6 confirm with audit_log
       and outbox → T-1.7 create_callback → T-1.8 transfer_to_team → T-1.9 call log. In parallel: T-0.7, T-0.8.
Lessons: Claude Code web sandbox: Docker daemon doesn't start automatically and dies with shell;
       start with setsid nohup dockerd. Local .env not in repo, credentials maex/maex/maex_agent.
       Local tests need DATABASE_URL on localhost. Docker Hub rate limit possible (docker login).
       str(URL) in SQLAlchemy masks the password. Codex reviews every PR automatically, findings good.
```

---

## 14. Open Questions (asked when needed)

| Question | When | Chat |
|---|---|---|
| POS system and interface | Stage 0 | C1 |
| Phone provider, PBX, redirect | Stage 0 | C1 |
| Budget monthly and one-time | before G0 | C1 |
| Stage order final (call mix) | G0 | C0 |
| Vendor and hosting | G0 | C2 |
| DB tech | Stage 1 | C3 |
| GUI tech | Stage 1 | C6 |
| Voice: natural or synthetic | Stage 1 | C4 |
| SMS summary yes/no | Stage 3 | C5 |

---

## 15. Glossary

- **Through-cut:** smallest version that runs once through all layers, from phone to GUI
- **Gate:** checkpoint with measurable criteria; can't proceed to next stage without passing
- **Eval suite:** test cases with expected results that automatically measure accuracy
- **Regression test:** run evals again after each change to prevent silent quality degradation
- **Hot / cold path:** work while customer waits, versus work after the call
- **Touch tones (DTMF):** number entry via phone keypad, robust with poor reception
- **Interruption (barge-in):** customer can speak over the AI's message
- **Idempotency:** sent twice, executed once; prevents double orders
- **DPA:** Data Processing Agreement per GDPR with each processor of customer data
- **PoC:** Proof of Concept, technical feasibility test

---

## 16. Changelog

- **v1.0 · 11.09.2026:** First version from C0 (loops 1–8). Contains objective, guiding principles, KPIs, hybrid architecture, stages 0–6 with gates, vendor criteria, data model, legal check, risks, and 8 work packages with start prompts.
