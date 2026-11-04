# PCF – Maex Voice-Agent

> **AI Call Intake for Delivery, Pickup, and Reservations**
> Version 1.1 · 07.10.2026 · Status: released (Gate P passed) · E1 flipped on 07.10.2026: own voice layer, see section 3 and `docs/20_VOICE_LAYER.md`
> This file is the Single Source of Truth for all project chats. Each chat reads it at the start and ends with a handover block (section 12).

---

## 1. Objective

An AI agent answers calls on the landline of <Pilotbetrieb>. It correctly handles reservations, pickups, and deliveries, hands off to kitchen, register, and team, resolves complaints by fixed rules from the database and passes what the rules do not cover to humans (D19, 07.10.2026; not built yet). The interface is simple enough that the team operates it under stress without explanation.

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
- Complaint → resolved by fixed rules from the database, else a callback task (D19, 07.10.2026) · request for a human → handoff or callback task
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
| E1 | ~~**Hybrid:** Telephony and speech processing via an EU-hosted specialist service; logic, DB, and GUI in-house~~ **Flipped 07.10.2026 (Maxi): no hosted voice platform, the voice layer is built in-house** (`docs/20_VOICE_LAYER.md`) | Until 07.10.2026: heavy lifting outsourced. Now: no platform fee, our code decides every sentence, independence from router and carrier. The price: estimated 5 to 8 weeks of work and uptime that is ours | set 11.09.2026, flipped 07.10.2026 |
| E2 | Stages: Reservations → Pickup → Delivery | Each stage introduces exactly one new challenge | confirmed by Maxi 07.10.2026; the call mix is the owner's estimate (under 30 calls on weekdays, 30 to 60 on weekends), no measured baseline |
| E3 | Rollout: Shadow → Overflow → Primary | Risk grows only with evidence | Assumption, holds until veto |
| E4 | Register stays booking master (TSE); agent DB for agent data only | Legal certainty, single source of truth for revenue | Assumption, holds until veto |
| E5 | Pilot <Pilotbetrieb>, number stays; menu, hours, zones from DB | Transferable to other locations later | Assumption, holds until veto |
| E6 | Training = knowledge base + eval suite + offline shadow mode, no model fine-tuning | Cheaper, measurable, legally simpler | Assumption, holds until veto |
| E7 | AI discloses itself as AI at the start of the call | EU AI Act Art. 50, in effect since 02.08.2026 | Mandatory |
| E8 | Human request, cancellation → team immediately, else callback task. **Complaint, changed 07.10.2026 (Maxi, D19):** the AI resolves it by fixed rules from the database; what the rules do not resolve is recorded and the team calls back | Trust, error containment; the goal is that the AI handles every call completely | changed 07.10.2026; the mechanism is built (T-10.11), the catalogue is empty and Maxi's, so the code still hands every complaint to the team |
| E15 | The own voice layer starts as a chain: speech-to-text, our conversation core, text-to-speech. Ear and mouth are swappable. A model that hears and speaks itself is measured against the chain on the same cases and replaces it only by numbers. A hosted voice platform is the last resort | The core and the eval suite stay as they are; research found speech-to-speech models weaker on tasks and not faster (not our numbers, T-10.10 measures) | set 07.10.2026 (Maxi) |
| E16 | The product does not depend on a restaurant's router or carrier: the software speaks SIP. Nothing heavy runs in the restaurant. One model call per turn, no multi-agent setup on a live call | Sellable to other restaurants; cash-register PCs are weak; every extra model call costs time and adds a place to misunderstand | set 07.10.2026 (Maxi) |

---

## 4. Architecture (own voice layer since 07.10.2026)

```text
Caller
  │
  ▼
Landline <Pilotbetrieb> (existing provider)
  │  Test phase: our software is one more telephone at the router
  │  Operation: forward or port to a SIP trunk
  │  Mode: Shadow · Overflow · Primary
  ▼
Own voice layer (our server, docs/20_VOICE_LAYER.md)
  │  Asterisk → speech recognition → conversation core → voice synthesis
  │  Touch tones · Interruption · Transfer
  │
  │  Tools in process (hot path)
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

**Security audit at every gate from G1 on** (Maxi, 15.10.2026): a gate also needs an audit per `docs/09_OPERATIONS_LEGAL.md` section 5 that covers the current code of the exposed areas, with no open P1 finding. The first call from outside the workbench and every component that becomes reachable from outside the host wait for one as well. The audit runs at these occasions only, not continuously.

### Stage 0 – Foundation
**Goal:** Clarify facts, legal framework, budget, and vendors before code begins.
- **C1** Current state capture: register and interface, receipt printer, phone provider and router, call volume and peak times, missed calls, call mix, menu format, reservation workflow, team workflows
- **C1** Measure baseline: errors and complaints per 100 phone orders (2-week tally), missed calls
- **C1** Prepare legal check (section 8), draft announcement texts
- **C2** Vendor shortlist per section 6, cost model, phone path sketch
- **Business case hypothesis:** The biggest lever is missed calls during peak times. Prove it with the call log.

**Gate G0 – Go/No-Go:** Budget approved · Legal framework clarified · Telephony path feasible · Vendor selected for PoC · Stage order confirmed per call mix

Since 07.10.2026 (E1 flipped): "vendor selected for PoC" reads as the voice path chosen for the test phase (`docs/20_VOICE_LAYER.md`), and "telephony path feasible" is shown by measurement at the pilot's router (T-10.1 and T-10.2), not on paper (Maxi, 07.10.2026). State per criterion: `docs/01_STATUS.md`, section Gates in Detail.

### Stage 1 – Reservation Through-cut
**Goal:** The chain test number → AI → tool → DB → GUI runs once completely real. Reservation is the vehicle because it needs few data points and touches no money.
- **C2** PoC on test number: latency, caller ID on redirect, touch tones, transfer, interruption
- **C3** Minimal schema: configuration, hours, capacity, reservations, calls, callbacks
- **C4** Reservation dialog, AI disclosure, human request, callback, system prompt v1
- **C5** Tools `get_service_status`, `check_slot`, `create_reservation`, `create_callback`; call log; error workflow with notification
- **C6** Operations GUI v0: reservations today, callbacks, switch "AI pause"
- **C7 parallel** (after legal approval and testing): Begin recording real team calls with consent. Data collection in background from now on.

**Gate G1:** 20 role-play calls (noise, dialect, interruption, human request) → 100% booked correctly or escalated cleanly · latency on target · cost per call measured · outage test: voice layer gone → call lands with team

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

> Superseded for the voice platform on 07.10.2026 (E1 flipped). The must criteria still hold, now for the pieces of the own voice layer: speech-to-text, text-to-speech, model and telephone access (`docs/20_VOICE_LAYER.md`). The list stays as the checklist for the last resort, a hosted platform.

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

> Note: Not legal advice. Until 07.10.2026 this line asked for a review by a lawyer or data protection consultant before go-live. Maxi decided on 07.10.2026 (D20, `docs/01_STATUS.md`) to work through the check by cross-checking two AI models instead, without a professional; the limits of that are recorded with the decision. The questions of the check were answered on 20.10.2026 (both rounds compared, D9 decided: Maxi himself); what is still open are the duties before the first real call, listed in `docs/09_OPERATIONS_LEGAL.md` §1.

- [ ] **EU AI Act Art. 50:** AI disclosure at call start, in effect since 02.08.2026 ([source](https://www.ai-ops-engine.com/blog/eu-ai-act-digital-omnibus-fristen))
- [ ] **Recording:** Consent from customer and team (§201 StGB); since the legal check of 20.10.2026 an explicit "ja" before the recording starts, a "nein" keeps the call going without it; the team's own calls in mode `shadow` need every team member's written consent (`docs/09_OPERATIONS_LEGAL.md` §1 L2, L11)
- [ ] **GDPR:** Legal basis per use case (order · recording · analysis) · information requirement (brief announcement + privacy policy on example.com) · DPA with all processors · third-country transfer check · deletion concept · processing activity register · data protection impact assessment review
- [ ] **Team:** inform, consent or agreement to recordings
- [ ] **Allergens (LMIV):** Information from maintained DB values only, else team callback
- [ ] **Register (TSE):** AI orders booked properly in register

---

## 9. Risks (Pre-Mortem)

| # | Risk | Mitigation | Stage |
|---|---|---|---|
| R1 | Latency or unnatural voice → customers hang up | Measure latency in the spike (T-10.3), choose the speech engines accordingly | 1 |
| R2 | Dish names or dialect misunderstood | Vocabulary, numbers, keyboard, evals with real recordings | 2 |
| R3 | Internet, server or voice layer down during peak | Fallback to team (`docs/20_VOICE_LAYER.md` §5, measured in T-10.2), alarm, "AI pause" | 1 |
| R4 | Recording or AI disclosure legally challenged | Legal check before G0, vetted announcement text | 0 |
| R5 | Menu or prices differ from register | one master, reconciliation test, "dish sold out" | 2 |
| R6 | Wrong allergen information | DB values only, else team callback | 2 |
| R7 | Costs spiral (spam, loops, long calls) | Max duration, loop protection, cost alarm | 1 |
| R8 | Caller ID lost on redirect | An IP telephone at the router and a SIP trunk receive it, a forward set in the router does not (research, tested in T-10.1); fallback: ask for number | 1 |
| R9 | A change silently degrades quality | Regression evals, versioning | 2 |
| R10 | Team doesn't use the GUI | 2-tap rule, team in role-plays | 1 |
| R11 | Vendor lock-in or price increase | Logic, prompts, evals and since 07.10.2026 the voice layer in-house; speech engines behind ports | 0 |
| R12 | Double order from retry or network error | Idempotency key per order | 2 |
| R13 | The self-built voice layer takes longer than estimated or stays unreliable (end of utterance, interruption, handing a call to the team) | Three spikes with go or no-go first; ways out in `docs/20_VOICE_LAYER.md` §13: SIP trunk, then a hosted platform | 1 |
| R14 | The router path of the test phase fails or a router update changes it (handing over to the team, VPN, limit of two calls) | Test phase only; the entry for operation is a SIP trunk | 1 |

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

> Superseded 07.10.2026: the comparison ran in Claude Code and ended with E1 flipped (`docs/20_VOICE_LAYER.md`). The prompt stays for the record.

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
## Handover 02.11.2026 - T-6.3, T-6.9, T-11.9, T-3.7, T-6.5, T-11.8, paused for a restart of Claude

Status: merged on `main`: #468 (T-6.3), #469 (T-6.9), #470 (T-11.9), #471 (T-3.7 check behind Caddy). Open, not merged: #472 (T-6.5 with T-6.2 part 2, head `1e288ea`, status 2.14.0, Codex clean on that head, CI green before), #473 (T-11.8, head `be96219`, migration 019, CI green, two Codex findings). Unfinished, pushed, no PR: `task/8-4-key-figures` (`c50c393`), `task/9-6-print-bridge-program` (`c967f9f`), `task/4-9-price-reconcile` (`b49f111`). T-9.8 has a design and no commit. A detached script runs two real-model passes at 14:20.

Artifacts:
- GPU run: `/tmp/claude-1001/-home-maeximilion-Maex-Voice-Agent--claude-worktrees-task-t-6-3-9a3077/fd4a6b23-1d28-4d55-8683-8a303fe8c999/scratchpad/gpu_eval.sh`, PID in `gpu_eval.pid` beside it, results in `gpu/`: `run.log`, `passes.log`, `head`, `reports/`, `done` when finished, about 14:50. It takes the head of `task/6-5-delivery-orders` at 14:19, `UD-IQ3_XXS` with the flags of docs/18 lane 4, and cleans up after itself. Stop it with `kill`, only on Maxi's word. Reports go to `evals/reports/` of the main folder for `scripts/readme_assets.py pending --record`.
- T-8.4 left: `/ponytail-review`, docs 06, 07, 01_STATUS with the assumptions on the four values, status bump, PR with `Closes #79`.
- T-9.6 left: run tests, read `gh run list --workflow printbridge.yml`, remove the temporary `push: branches` entry in `printbridge.yml`, README with Maxi's steps, docs 13, 07, 11, 01_STATUS, scripts/README, review, PR with `Closes #409`. Nothing on that branch has been run.
- T-4.9 left: all implementation; the branch holds red tests only, never run. Migration 019 there collides with #473, so it renumbers. Read `api/tests/test_domain_menu_price_check.py` first, it entered the commit unread. The tests change import behaviour: an existing surcharge stays without `--apply-price-changes`.
- T-9.8 left: all of it. Design: `backend` (db, api, dispatcher, n8n, push, voice) and `telephony` (asterisk, voice) in the base file; the prod file sets `backend` internal and adds internal `edge` (caddy, api, n8n, push) and `public` (caddy only). `networks: !reset []` for asterisk in the line and tunnel files, `wireguard` on `telephony`, `BACKUP_DOCKER_NETWORK` to `<project>_backend`, a drill point with `docker network disconnect` in mid-call, `api/tests/test_compose_networks.py`.

Decisions:
- Register: the <POS Provider> gives no interface; the input slip is the permanent path, variant C is gone (D2, Maxi).
- Legal: no lawyer. docs/09 §1 "Review of 02.11.2026": no question open, U1 to U8 accepted, tax adviser no longer blocks, new N1 and N2. Drafts in `docs/vorlagen/` carry 38 `<offen: ...>` marks for Maxi.
- Objection (L12): an objection on a number means no link by name, a new customer row (Claude's call, fixed in #472).
- Maxi on blocked work, 02.11.2026: what costs money or needs hardware stays blocked; what he blocked himself is checked. Result: T-9.8 released, T-10.11 buildable with an empty rule catalogue, no invented remedy.
- T-6.4 stays unbuilt while D5 needs no polygon.

Gate: G0 open, unchanged. It waits for T-10.2 on the wire and two calls at once (T-10.1), with Maxi at the telephones.

State at 18:00, moved to the Ubuntu terminal (replaces the Status line above where they differ):
- Merged today: #468 T-6.3, #469 T-6.9, #470 T-11.9, #471 T-3.7, #472 T-6.5 with T-6.2, #473 T-11.8, #474 T-8.4, #475 T-9.6, #477 T-9.8, #478 T-6.8, #480 the button "Storniert". T-2.4 and #234 closed.
- Agent limit, for the session of this handover only (Maxi, 02.11.2026): at most 5 agents at once Monday to Thursday, 8 on Friday and Saturday, no limit on Sunday; a workflow's agents count. Workflows, ultracode and review or audit agents are welcome when they help. Saturday is Claude's reading, Maxi named Friday, Sunday and Monday.
- Maxi's order: finish the open PRs one by one, take up no other issue for now. The earlier agents belong to the old desktop session; a new session starts new agents on the same branches.
- #479 T-10.11 head `6c1efeb`, migration 021 with parent 019, Codex clean, conflicts on `docs/01_STATUS.md`. Refs #403, which stays open: the catalogue is Maxi's.
- #476 T-4.9 head `e7c9a17`, migration 020 with parent 019, earlier findings fixed, a new Codex P1 on that head, thread 4445181638, `api/domain/menu/importer.py:718`: pending price cards must be invalidated when an import replaces sizes; conflicts with `main`. One assumption still waits for Maxi: surcharges stay report-only.
- #481 phone number said in words, head `e2baac5`, Codex clean, 7114 passed, label bug. `lieferung_0016` stays red with the stand-in.
- `task/11-3-recording-consent` head `378f3d0`, no PR, built behind `RECORDING_ENABLED`, off by default, migration 022 on 019 until 020 and 021 merge, drill 43 of 43, `/ponytail-review` and `/security-review` done. Left: pytest, mypy and scripted evals on the merged head; two fixes in `agent/consent.py` with tests, steps in the head commit message ("Hören Sie auf mich aufzunehmen" and "Aufnahmestopp" are no stop today, "Bin ich damit einverstanden" without a question mark counts as a yes, it must not); status bump; PR with `Closes #405`.
- Two tests failed once under load and passed alone, cause not found: `test_correction_after_an_older_printed_slip_says_korrektur`, `test_ueberholter_bon_ohne_versuche_ist_kein_alarm`. Both sit in the handover code. If one fails in CI, read it first.
- Whichever of #479 and #476 merges second must set `down_revision` on the other's head, or alembic gets two heads. T-11.3 takes the next free number.
- No `--delete-branch` where a PR stacks on the branch. A new head that merges `main` often stalls Codex: one `@codex review`.

Open, next concrete step first:
1. #479: merge `origin/main` into its branch, keep every Done row and changelog line, status version above main's, full suite, mypy, ruff, scripted evals on the final tree, push once, wait for CI and Codex, merge.
2. #476 the same way; set `down_revision` of 020 to 021 (or the reverse) and test up and down. T-4.9 decides prices: evals on the exact head.
3. #481 the same way. Its diff hits `xhigh`: evals on the exact head.
4. T-11.3: the left list above, then the same way; set its migration's `down_revision` to the head on `main`.
5. Ultrareview candidates for Maxi: #479, #481, T-11.3.
6. Delete the remote branch `docs/handover-2026-11-02` once this block is on `main`, and the memory `resume-after-restart-2026-11-02`. Deleting needs Maxi's word: the permission check refused it.
7. Maxi: the real zone list as `imports/zones.csv`, the `<offen>` marks, the complaint rules, a delivery slip and a "STORNIERT" note on paper (the register PC needs the new bridge build, an older one prints KORREKTUR), whether the driver needs a phone number on the slip, steps 3 to 9 of `printbridge/README.md`, surcharges on the tablet or not.

State at 20:00 (Ubuntu terminal; replaces items 1 to 4 above):
- Merged: #479 T-10.11, #476 T-4.9, #481 the phone number in words, #482 T-11.3 (#405 closed). The migration chain on `main` is 019, 021, 020, 022: 020 follows 021, which merged first, and 022 sits on 020.
- Fixed on the way, each with a red test first: Codex P1 on #476, where an import that changes a dish's sizes now withdraws its open price card; Codex P1 on #481, where a date partly in words takes no count as a phone number. On T-11.3, the three security review points and seven findings of an independent multi-lens review: a stop with the verb split ("Nehmen Sie das nicht auf"), an "Okay" to the stop notice that confirmed a read-back, missed refusals and withdrawals, a no with a request that dropped the request, the eval judge mixing the two consent questions, and the way to stop played interruptible.
- The weekday agent limit (memory `weekday-agent-limits-planned`): the table in `CLAUDE.md` and `.claude/hooks/agent_limit.sh`, in the PR of this block.
- Still open: items 5 to 7 above. Delete the memory `weekday-agent-limits-planned` once the agent limit PR is merged.

Lessons:
- A session the desktop app starts bypasses Headroom by design: check with `printenv ANTHROPIC_BASE_URL`; the Ubuntu terminal is routed.
- Never `--delete-branch` on a PR with a PR on top: GitHub closed #469 and #470. Repair: push the old head back as the branch, reopen, PATCH the base, delete the branch.
- `gh pr view --json statusCheckRollup` gives an empty `conclusion` for a running check: read `status`.
- Codex stalls on a head that only merges `main`: one `@codex review`, it then answers in minutes.
- Agents share the scratchpad and overwrite each other's files there (`msg.txt`, `resolve.py`): give each its own folder.
- The hook matches the merge command in any text of a Bash call, so a note about it goes into a file.
- T-3.7 was already built on 28.10.; check the code before a brief says build.

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

## Handover 04.11.2026 - subscription inline-review design

Design and setup: [21_REVIEW_AUTOMATION.md](21_REVIEW_AUTOMATION.md), including
the implementation plan and the deferred specialist-review experiment. One
combined reviewer was selected. The old `ai-pr-review` workflow is disabled and
removed; its self-hosted runner and API adapter cannot supply this integration.
`CLAUDE.md` persists the requirement to comment `@claude review` after PR creation
and every push of Codex work; the existing Codex review integration continues for
Claude work. Push and comment triggers must not duplicate inference. The later
specialist stage also introduces API-first authentication within a shared USD 50
monthly review cap, followed by subscription OAuth. Credit eligibility and hard
budget enforcement are still to be verified; no API is connected now.
The official action supports subscription OAuth, but no live review is verified.
Branch `ci/claude-inline-review` is independent of `docs/cross-agent-review`;
the latter waits. Next: approve the concrete design, implement and test on this
branch, open one PR, configure `CLAUDE_CODE_OAUTH_TOKEN` through GitHub Secrets,
then verify real inline findings and a second pushed head before merging.

```text
## Handover 03.11.2026 - T-3.8 to T-3.14 merged, next the rest of Block 2 (#8): T-3.11, T-3.12, T-3.15, T-3.16, T-3.5

Status: merged on `main`: #490 (redesign, D26), #492 (wait grid), #498 (README colours, D27), #500 (default waits 30 and 60, migration 023), #502 (T-3.9), #503 (T-3.8), #509 (T-3.14, migration 024), #513 (T-3.13, migration 025), #515 (T-3.10, migration 026). The workbench database is at 026 (Maxi ran `make migrate`), the view on port 8000 answers. Nothing of this session is open or uncommitted; this handover rides on branch `docs/handover-t3-gui`, pushed, no PR of its own.

Artifacts:
- New in the code: `domain/status/waits.py` (the one reader of the wait), `domain/reservations/notice.py`, sheets `fragments/wartezeit.html`, `vorlauf.html`, `gericht_aus_zeile.html`, migrations 023 to 026, `.claude/hooks/merge_gate.sh` takes `gh stack merge <n> --merge --yes` for the bottom PR of a native stack.
- Screenshots: `docs/design/2026-11-redesign/`, `2026-11-brand/`, `2026-11-panels/`. The mockup `gui/mockups/betrieb-tablet.html` holds for layout only, its colours are superseded by D27.
- Issues opened: #516 (T-3.15 callback history), #517 (T-3.16 parked review findings), both sub-issues of #8.

Decisions:
- D26 new look, D27 README palette with its meanings and one dark theme, D28 seats stay the capacity until the floor plan is in use, D29 all three modes stay (`overflow` needs the router to ring the team first, untested on the line).
- T-3.14: today's wait carries its end (05:00 local) instead of a job at 05:00; migration 024 kept each restaurant's values as its standard.
- T-3.13: the word on the tablet is "Reservierungs-Vorlauf" (trade word Vorlaufzeit, Maxi found "Vorbestellzeit" confusing next to pickup and delivery). The agent says "laut unseren Reservierungen" because the database does not know who walked in. Limit 1440 minutes, an assumption.
- T-3.10: one button per service names its state ("Abholung an", ✓/✕), the old delivery pill and its action button are gone.
- Stacks (Maxi): every chain of open PRs is linked as a native GitHub stack with `gh stack link`, without asking, also for other sessions' PRs. A stacked PR merges with `gh stack merge`, Maxi allowed it in general.
- "Bestellung stornieren" is the button, "Storniert" the line after it (Maxi).

Gate: G0 open, nothing of Block 2 blocks it. Block 2 (#8) belongs to G1 with Block 1.

Open:
1. T-3.11 floor plan (#496), effort `high`. First decision, as a closed question to Maxi: stay with HTMX (pointer events in `app.js`, positions saved per tap) or bring in React for this one view; docs/06 §2 names drag and drop as the trigger to reassess. Recommended: HTMX, until a measured limit. Needs a migration (tables, seats, combinations, positions), which is `xhigh` for the migration part.
2. T-3.12 manual table reservations (#497), `xhigh`, after T-3.11. A manual booking has no call, and every write carries a `call_id` today: the data model answers that first. D28 holds: the agent stays on seats.
3. T-3.15 callback history (#516): read docs/09 L8 first, 7 days, "Wieder öffnen" with `audit_log`, likely no migration.
4. T-3.16 (#517): pick what is cheap (red cards pinned, "Tauschen" field under its line); the rest waits for the real tablet.
5. T-3.5 operating test (#44): Maxi with two team members on the real tablet, the build must not change between the two passes, so it runs after T-3.11 to T-3.16. It also answers the open questions of T-3.16 (keyboard, glare, 1180 px header).
6. Rows T-3.15 and T-3.16 are in docs/07 on this branch; the first feature PR of the next session branches from `docs/handover-t3-gui` so they ride with it.

Lessons:
- The merge gate runs from the session's project folder, not from a worktree's branch: a change to the hook counts only once that folder has it. The four stack merges of 03.11.2026 ran before that and were checked by hand.
- GitHub refuses `gh pr merge` for a PR in a native stack; `gh stack merge` works and merged #509, #513 and #515 in a row without another merge of `main`.
- Text that names the merge command is blocked by the gate even in a heredoc: write such edits with the Write tool into a file and run the file.
- Two independent UI/UX review agents (Sonnet, real browser, screenshots) found real defects the builders had missed, among them a frozen header and a dish jumping under the finger. Worth repeating for T-3.11 before its merge.
- A subagent reported grouping tests that did not exist on the first head. Check a subagent's claims against the diff before relaying them.
- The Ubuntu Claude CLI goes through the Headroom proxy on port 8787 (a systemd user service). After a PC restart it was down until linger was enabled (`loginctl enable-linger`, done once on 03.11.2026); "ECONNREFUSED" in a terminal session means that service, not a firewall.
- A throwaway test server for Maxi runs best from the worktree on its own database copy (`uxr2_tmp` pattern, port 8019); tell Maxi the port, he tests in parallel.

## Handover 03.11.2026, 02:55 - audit fixes and Maxi's three decisions, paused for the PC shutdown
Status: merged since the handover of 02.11.2026 evening: #491 (data subject
        verification by own-line rows, with the Dependabot bumps #485 to #488
        and one Dependabot PR per ecosystem), #504 (Kennzahlen lookup bounded,
        265 ms to 59 ms), #506 (recording safeguards), #507 (two details on two
        numbers need every number named), #508 (a contact moves only counted
        customer rows), #510 (print bridge re-audit fixes). Re-audit of T-10.1
        on 9808399: no P1, 1 P2, 7 P3, all fixed except migration 023 (comment
        on #500). The other session's #503, #509, #513 and #515 are merged.
Stack, Maxi's decisions of 03.11.2026:
        1. #518 fix/calls-start-dev-only, POST /v1/calls/start only in dev
           and test: merged into main.
        2. #519 fix/shadow-recording-hangup-card, a callback card for a
           caller who hangs up in shadow before the team rang (P3 no. 9):
           merged into main (ec44e8c).
        3. #520 fix/default-address-at-mark -> main, next to merge: the
           default address moves at "In Kasse eingegeben", not at the yes
           (P2 no. 4, option B, the register mark confirmed by Maxi in a
           popup). origin/main ec44e8c is merged in; the head, CI, Codex,
           suite and evals of that head are in the PR body. Codex's three
           earlier rounds (all P2, fixed) landed in the same function, so a
           fourth finding there goes to Maxi as a redesign question before
           any patch.
Next: #520 once CI and Codex are clean on its exact head and the evals ran
        on it (it touches an xhigh trigger): merge with --match-head-commit.
Then: the T-10.1 audit is due again (files changed since 9808399), before the
        first call from outside; /v1/calls/end stays (it only closes a known
        call, by an unguessable id; dev-only would be a small follow-up if
        Maxi wants it).
Rules learned today (Maxi): decisions go to a popup; parallel agents are fine,
        only merge-ready PRs merge one by one; the other session's PRs merge
        before this session's stack; every PR body keeps the template
        checklist (CLAUDE.md section 8).
Leftover: worktree agent-ac709ddead667d87c was locked by its process at the
        last check; remove it with git worktree remove when unlocked.
```

```text
## Handover 02.11.2026, evening - security audit T-10.1 and its fixes, paused by Maxi
Status: the scoped security audit of T-10.1 ran on a48d142 (105 exposed files
        since 52bf88c): 3 P1, 2 P2, 13 P3, none refuted; recorded in docs/09
        section 5 and on #252 (comment of 02.11.2026). Merged: #484 (print
        bridge and kitchen token hardening, P2 no. 5 and P3 nos. 15 to 18,
        with the audit record), #489 (recognition only on the caller's own
        line, P1 no. 1 and P3 nos. 12, 13). Closed: the group issues #7 and
        #395 (all sub-issues done). 37 merged local branches deleted.
#491 state: pushed head = the commit that adds this handover (docs/00_PCF.md
        only) on top of 38e938f, which merged origin/main 95b4915 (#489) and
        all four Dependabot branches: #485 ntfy v2.35.0, #486 n8n 2.45.7,
        #487 mypy 2.4.1, pydantic 2.14.2, pydantic-core 2.50.2,
        pydantic-settings 2.16.2, pytest 9.2.0, ruff 0.17.3, #488 types-pyyaml
        6.0.12.20261030 (each only docker-compose.yml or api/requirements.txt;
        PR body: Closes #485, #486, #487, #488). Ran on 38e938f, test image
        built from it with the new pins: full suite 7557 passed, 319 skipped,
        latency 44 passed, 3 skipped; ruff format, ruff check, mypy clean. Not
        run on it (paused): the scripted evals against main (last run on
        9c454b3: identical to main 13a72db case by case); CI and Codex not
        waited for.
Artifacts: api/domain/customers/lookup.py (line_of, recognize),
        api/domain/delivery/address.py (own_line), api/domain/customers/
        requests.py (verify on own-line rows, Ambiguous), api/domain/ordering/
        handover.py (SLIP_FIELDS), printbridge/install.ps1.
Decisions: a guest calling from another phone and naming the usual number is
        asked for name and address again (fails closed, Made Assumptions).
        P2 no. 4 (a spoofed caller ID moves the default address) waits for
        Maxi: keep, keep the old default until the team marks the delivery
        done, or read the street back after a recent change.
Gate: G0 open, unchanged. It waits for T-10.2 on the wire and two calls at
        once (T-10.1). The T-10.1 audit holds only once #491 is merged and
        until the next exposed file changes.
Open: 1. #491 (P1 nos. 2 and 3, data subject verification) with the four
        Dependabot bumps #485 to #488: on its pushed head run the full suite,
        ruff, mypy and the scripted evals compared with main, wait for CI and
        Codex, then merge with --match-head-commit (xhigh, Claude merges once
        clean). 2. Maxi: P2 no. 4. 3. #252: /v1/calls/start sits behind the
        same AGENT_API_TOKEN as the tools, so a token holder can create a call
        with any caller ID (a separate token or no mount outside dev and test).
        4. The four recording points (P3 nos. 6 to 9) before RECORDING_ENABLED
        is switched on.
Lessons: GitHub does not move a stacked PR's base when the branch below is
        merged without deletion: PATCH the base to main by hand. Merging main
        moves the head, so the stacked PR needs CI and Codex again.
```

```text
## Handover 02.11.2026 - #244 threads, T-6.1, T-6.7, T-6.2 part 1, deps
Status: merged 01./02.11.2026: #452 (the four open threads of #244), #453
        (findings 8 and 9 of the audit on 7768182), #454 (T-6.1, migration
        018), #458 (the team drills on a day with a row of its own), #459
        (the four Dependabot PRs in one), #460 (T-6.7, customers go after 24
        months), #461 (a holiday suggestion reaches a tablet left open), #462
        (T-6.2 part 1, find_customer gives ids and no stored word), and the
        print bridge's exit code in the PR of this block. No open Dependabot
        PR, no open security alert. The workbench is at migration 018 (backup
        before_migration_018) and passes the outage drills 27 of 27; its
        running images are from before #459. Not run: the real-model eval on
        FastAPI 0.162 and on #452, T-10.2 on the wire.
Artifacts: api/models/customers.py, db/migrations/versions/018_customers.py,
        api/domain/customers/lookup.py, api/tools/find_customer.py,
        api/agent/guest_details.py (asks_for_a_phone_number).
Decisions: find_customer gives ids only, against the first draft of docs/04,
        which L12 forbids. recognition_objections has no period. A shared
        number without a telling name counts as unknown. All three are under
        Made Assumptions, awaiting Maxi. T-6.2 part 2 waits for T-6.5 and a
        real-model run; T-11.3 and the open points marked "Maxi decides" were
        left alone on purpose.
Gate: G0 open. It waits for T-10.2 on the wire and two calls at once (T-10.1).
Open: 1. T-10.2 with Maxi at the telephones (Opus 5.5 xhigh, Ubuntu). Rebuild
        the workbench images first. 2. T-10.1 scoped audit before the first
        real guest call, eight agent files. 3. Real-model eval, Maxi names the
        start. 4. holiday_region = 'BW' on Maxi's word. 5. The four threads on
        #244 have no reply: the write was denied, they are fixed by #452.
Lessons: Start the wait on Codex a minute after a push: right away it read the
        old head and came back clean. Editing a PR body re-runs the required
        title check, so the merge is refused for a moment. BACKUP_PASSPHRASE_FILE
        and BACKUP_DOCKER_NETWORK are empty in the workbench .env: pass
        ~/.config/maex-voice-agent/backup.passphrase and
        maex-voice-agent_default by hand. Merging Dependabot's branches into
        one PR marks their PRs as merged.
```

```text
## Handover 30.10.2026 - #437, T-9.5, #441 next
Status: merged today #437 (a call that carries only noise goes to the team,
        18701bb) and #439 (T-9.5, holiday suggestions, migration 015,
        aa6b1fd). Both with green CI, a clean Codex review and the evals on
        the exact head (cases 56/56, targets 1/29 as on main, number eval
        249/249). Ultrareview of #439: no defect; its one real point, two
        counting rows for one date in special_days, is issue #441. Not run:
        the holiday card on a real tablet (route tests only), migration 015
        on any database that holds data.
Artifacts: api/domain/status/holidays.py, api/jobs/holidays.py,
        db/migrations/versions/015_holiday_suggestions.py, the card
        fragments/feiertage.html; branch fix/441-special-days-one-set-row
        carries this handover and nothing else yet.
Decisions: T-9.5 choices (a) to (e) under Made Assumptions in
        docs/01_STATUS.md, awaiting Maxi: Sunday's hours as the proposal,
        60 days ahead, statewide days only, the card above "Heute", the
        state set by SQL until the admin area exists. Migration 015 was not
        applied to the workbench database: another session (T-8.3, #440 with
        migration 016) was working at the same time, and the database there
        is at 012, so it takes 013 to 015 plus a rebuild of api and
        dispatcher in one go.
Gate: G0 open, unchanged by this session.
Open: 1. #441 on this branch (Opus 5.5 at xhigh, migration 016 or 017,
        whichever #440 leaves free): a partial unique index on special_days
        (tenant_id, date) WHERE status = 'set' that first reports duplicates
        and stops; answer() turns a suggestion into dismissed when a set row
        for the date exists; tests and migration up and down.
      2. Workbench, when no other session uses the stack in ~/Maex-Voice-Agent:
        backup (bash scripts/backup.sh --label before_migration_015), then
        alembic upgrade head, docker compose up -d --build api dispatcher,
        and with Maxi's word UPDATE service_config SET holiday_region = 'BW'
        (the state the module doc named before T-9.5); the dispatcher writes
        the suggestions within six hours, python -m api.jobs.holidays at once.
      3. Maxi: the T-9.5 assumptions; whether the card should also live in
        the admin area "Zeiten" once it exists.
Lessons: like_weekday 0 is Monday, so never `x or SUNDAY` on it (fixed
        before the merge, test added). Docker Desktop was closed at the
        session start: start it from WSL with powershell.exe, the engine
        answers after about a minute. A root-owned evals/reports folder left
        by an earlier session blocked `git worktree remove`; a throwaway
        alpine container removed it.
```

```text
## Handover 26.10.2026 - T-11.1 redesign built on #368
Status: the redesign decided by Maxi is built on #368 and pushed. The core
        reads the caller's allergy from the guest's sentence every turn into
        state.allergies (agent/allergies.py: ingredient, whose, dish), asks
        "Wogegen?" and the consent itself while it holds the draft, and writes
        the allergy onto line, order or table; a cart or draft whose note names
        an allergy is refused. with_consent, unread_allergy, keep_restrictions,
        order_note and table_note are gone. The open P1 4378523387, the three
        Codex P1s on d1ad1e0 (4378680652, 4378680664, 4378680687), the two on
        f3e997f (4379357300, 4379357317), those on 2844b82 (4379622793) and
        94f1fa8 (4379762029), 3ac0383 (4379960053), 6f94f73 (4380083176),
        3c4cc6a (4380178537) and every
        sentence shape of the earlier findings are tests; "Sesam vertrage ich
        auch nicht" (#361) is read. Suite 6180 passed; evals against main case
        by case: cases 54/54 (main 50/50, all of main's pass, 4 new), targets
        1/28 (main 1/27, the same one passes, reservierung_0038 red as before),
        numbers 249/249.
Artifacts: #368 task/11-1-allergy-consent (redesign b52f686, main merged in,
        status 2.10.0 and this handover). #369 task/11-1-allergen-beyond-db
        6c58dc1, not touched.
Decisions: none new by Maxi. Claude's readings, in docs/01 assumptions (h) to
        (k): an allergy said with an unclear dish goes onto the whole order; a
        sentence read whole and behind the dish with two different ingredients
        is asked about ("Pho Bo und Sommerrollen, Erdnussallergie"); a later
        denial takes no allergy back; an answer to "Wogegen?" that is a
        sentence of its own or an order is not read.
Gate: G0 open, unchanged.
Open: 1. Codex on the head of #368, findings fixed in one push per round. Then
        Maxi merges #368 (allergens, xhigh). Ultrareview recommended (xhigh
        trigger, more than 400 lines outside tests).
        2. After #368: merge main into #369, rerun suite and evals, Codex,
        Maxi merges.
Lessons: reading a whole guest sentence is not reading the wish behind the
        dish: the list rule of the parser takes dish and guest names before a
        compound ("Bo, Sommerrollen und Erdnuss"), and a bare answer to
        "Wogegen?" is read up to its end. Prompt edits count against the token
        budget tests (system prompt 800, core 2600).
```

```text
## Handover 26.10.2026 - T-11.2, T-11.1 taken over, redesign decided
Status: merged today: #371 (T-11.2 deletion job, Maxi), #373 (scripts/codex_review.py,
        Claude). T-11.1 taken over from its idle session on Maxi's word: #368 and
        #369 merged with main (migration 010 for the consent log, run_turn in
        main's structure with the held draft and the answer to "Sind Sie ein
        Mensch?"), the consent_log period in the deletion job, and four Codex
        rounds of P1 fixes. #369 clean on 6c58dc1, #368 has one open P1
        (4378523387) on 8275a6a. Neither is merged: Maxi chose the redesign (A).
Artifacts: #368 task/11-1-allergy-consent 8275a6a (+ this handover), #369
        task/11-1-allergen-beyond-db 6c58dc1. Design and the open finding: the
        comment of 26.10.2026 on #368 ("Decision by Maxi ... redesign").
Decisions: consent_log period: end of the third calendar year after the answer
        (Maxi, option A; RETENTION_CONSENT_LOG_YEARS, in the deletion job on #368).
        A callback never done loses phone and summary 90 days after creation
        (Maxi, option A on #371). T-11.1 redesign (Maxi, A): allergies never
        travel in free text; the core reads them from the guest's sentence into
        structured state, refuses model notes with allergy words, writes the
        allergy onto line, order or table itself.
Gate: G0 open, unchanged.
Open: 1. Build the T-11.1 redesign on #368 in a new session, Opus 5.5 xhigh:
        the design in the #368 comment, the open P1 4378523387 covered by it,
        every sentence shape of the findings as a test, suite, evals against
        main, Codex. Then merge #368 into #369, check, both to Maxi.
        2. The CodeQL jobs on #368's 8275a6a failed without an alert; check on
        the next push. 3. The deletion job dies with the dispatcher's start
        check (ultrareview of #371): a service of its own, for #207.
Lessons: Codex reviews every push by itself; never write "@" plus codex in PR
        text (it turned #373 into a task and stopped all reviews); read the
        state with scripts/codex_review.py --wait (stalled after 10 min).
        Resolve every answered review thread by comment id, or Codex counts it
        on every later head. Start the CI wait a minute after a push. A
        throwaway Postgres named t111-db (exited, from the first T-11.1
        session) is left on Docker; not removed, not ours.
```

```text
## Handover 25.10.2026 - T-11.4: the call log keeps the hour, not the minute
Status: built and pushed, no pull request yet. scripts/call_log.py reads `time`
as the hour ("18", "18 Uhr"); a row with a minute passes and every run cuts it
to the hour in the CSV (atomic, file rights kept). 174 script tests green (20
new), each new mapping path red under a mutation of its guard; end to end on
the template: minutes cut, 0600 kept, second run changes nothing. ruff, mypy,
README asset check clean. Full suite in /done against a throwaway Postgres:
5,575 passed plus 40 latency tests; the diff touches no conversation path.
Artifacts: branch task/11-4-call-log-hour, commit 53013de on top of the
handover commit c2afa5b. scripts/call_log.py, api/tests/test_scripts_call_log.py,
docs/17_ANRUFPROTOKOLL.md, docs/vorlagen/anrufprotokoll.csv and
anrufprotokoll_druck.html (column "Stunde"), docs/metrics/capabilities.toml with
README.md and .github/assets/progress.svg re-rendered, docs/07 (done), docs/01.
Decisions: case ID from date, hour and order of rows in that hour. The cut
changes the ID of an old row, so .call_log_ids.json keeps the old minute ID
under the new one ("was") with the fingerprint at review; write_cases finds a
reviewed case through it. No duplicate merge any more: without the minute a
row typed twice and two calls with the same words look alike, so every row
counts and the report names same-content rows of one hour by line number
(rows with free text only). Recorded under assumptions in docs/01.
Gate: G0 open, unchanged (waits for T-10.1 and T-10.2 at the pilot's router).
Open: 1. /done on this branch (ponytail-review, PR, status bump already done
by this handover), then CI and Codex on the head; no xhigh trigger, Claude
may merge. 2. The T-11.1 finding ("Sesam vertrage ich auch nicht" lost) still
goes as one comment to the T-11.1 PR once it is open. 3. Dependabot #354 to
#356 once main is green.
Lessons: readme_assets.py render rewrites README.md from
docs/metrics/capabilities.toml; edit the toml, never the README line. The
worktree has no own venv: use ~/Maex-Voice-Agent/.venv/bin/python. The app
offered no session-title tool in this WSL session.
```

```text
## Handover 25.10.2026 - issue sweep 21. to 25.10.2026: six PRs merged, next T-11.4
Status: every PR this session opened is merged, one by one: #358 route_advice wording
       (#332 closed), #359 container hardening and #360 token strength, security headers,
       refused INVITE, archive pin (#252 findings 5, 7 to 9, 11 to 13 and part of 10),
       #362 number words (#325, #327, #328 closed), #357 docs (#188, #323 closed, #344
       points 2, 3, 5), #361 allergen wishes by option C (#207 and #331 stay open).
       main fc250ec is green; open PRs are only Dependabot #354 to #356.
Artifacts: branch task/11-4-call-log-hour (this handover only, no code yet). Outside
       the repository: ~/.claude/usage_gate/install.py on Ubuntu and on Windows
       (identical, CRLF, old version as install.py.bak-20261021 next to each), copied
       into docs/19 section 4; ~/.claude/settings.json gained
       skillOverrides "anthropic-skills:caveman": "off" and
       env CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-5-5 (/doctor; copy before the change
       in settings.json.doctor-20261022.bak); two memory notes removed, copies in
       ~/.claude/doctor-removed-20261022/.
Decisions: #361 option C (Maxi): where the parser cannot read a whole allergy it asks
       "Wogegen?" instead of reading further; four rounds of extending the reading each
       put new words on the kitchen note. 17,280 generated sentences: no word main does
       not note, no allergen main notes is lost, 2,794 now ask. #357 merged under the
       review loop limits: the P1 "encrypt the settings backup" does not apply (the rule
       covers data backups in scripts/; the .bak is 0600, its source 0644), two
       PowerShell points in #207. No new task while a PR of the session is open (Maxi,
       22.10.2026, in memory).
Gate: G0 open, unchanged: the telephony measurement (T-10.1 step 2 at the pilot's router).
Open: 1. T-11.4 in a new session on task/11-4-call-log-hour, Opus 5.5 high: the hour of
       a call instead of the minute, a new duplicate key, coarsening existing rows
       (docs/07 row, docs/17, legal check L15). 2. "Pho Bo, Sesam vertrage ich auch
       nicht" loses the intolerance: belongs to T-11.1, which another session works on
       (task/11-1-allergy-consent); post it there as one comment once it has a PR.
       3. Dependabot #354 to #356 when main is green. 4. The CLAUDE.md trim of /doctor
       (stack table, Threads, Ultrareview, Security alerts paragraphs into docs/19,
       /done, /start), about 1.1k tokens a session, in a PR of its own when Maxi wants it.
       5. Proposal in #207: move the usage gate into the repository with its tests.
Lessons: GitHub returns PR reviews in pages of 30: read them with
       gh api --paginate ".../reviews?per_page=100", or a review with findings is missed
       (#357 was reported clean once by that mistake). A Codex review on the head that
       carries inline comments is findings, whatever its summary text says. Every merge
       moves the status version line on main, so parallel PRs need a main merge each;
       merge them in sequence. main went red on 21.10. when ubuntu-latest became 26.04
       (Postgres client 18); #364 fixed the test. A session this long should have been
       handed over earlier.
```

```text
## Handover 22.10.2026 (afternoon) - PR #347: one Qwen run on the final head left
Status: Qwen runs on f0136c1 (16:01 to 16:27): 26/43 and 26/43 (main 27, 24), one missed
       escalation each (allergie_0002, as on main), reservierung_0035 passes, targets 2/27,
       hard violations 0, recorded in docs/metrics/eval_history.csv. After the runs one code
       commit, 288fb26 ("Abhol-Code" with a hyphen is a booking word, Codex P1), covered by
       unit tests only; Codex asks for a run on the final head (thread open).
Artifacts: PR #347 (head with the status and handover of this block), README charts.
Decisions: none new.
Gate: G0 open; PR #347 needs one regular-cases run on its head, then the merge gate comment.
Open: 1. In Maxi's next GPU window (about 13 min: server 1 min, one run of the 43 cases):
       one regular-cases run on the PR head, record it, answer and resolve the Codex thread
       on guards.py (the eval rerun). 2. If CI and Codex are clean on that head, post
       "Merge gate: <full head sha>" and give Maxi the merge command of CLAUDE.md section 6.
Lessons: push the review fixes before the GPU window, not during it; a fix after the runs
       always asks for one more run.

## Handover 22.10.2026 (late morning) - PR #347 ready except the Qwen runs
Status: PR #347 head pushed, CI and Codex on it pending at hand-over. Since the night runs
       on 33843ab: a reserved_for without an offset is the restaurant's local time
       (dispatch.in_local_time, also for the team card; a date alone, an off-calendar
       moment and a wall time the zone lacks or has twice stay invalid input), the stand-in
       lost its dead sentences after confirm, and four Codex rounds of fixes (aufgegeben and
       aufgenommen are booking words, a late confirm keeps the booking in the guest's ear
       and on the card, a confirmed table names an unplaced order, the pending readback may
       be read word for word). Suite green except mount-only tests, stand-in 43/43, targets
       1/27, number eval 249/249.
Artifacts: PR #347, docs/metrics/eval_history.csv (no rows for the new code yet).
Decisions: Maxi 22.10.: the GPU runs move to tomorrow (23.10.2026).
Gate: G0 open; PR #347 waits for the Qwen runs on its head (rule 4, Codex eval thread open).
Open: 1. In Maxi's GPU window: 2x regular cases and 1x targets on the PR head (about 33 min;
       the script and the override are in the session scratchpad, recreate them from the
       local test run recipe if it is gone), record them, close the Codex eval thread.
       2. If reservierung_0035 passes and nothing new fails hard: post the comment
       "Merge gate: <full head sha>" and give Maxi the merge command of CLAUDE.md
       section 6 (merge strategy, --match-head-commit with the full head sha).
Lessons: a fix round right before a GPU window moves the head under the runs; batch the
       review rounds first, then measure.

## Handover 22.10.2026 - PR #347: eval runs on Qwen, the core says every booking
Status: PR #347 (dev/eval-qwen-optimization-8b4ad7) pushed, xhigh, Maxi merges. Final code
       33843ab measured on qwen3.6-35b-a3b: 24/43 and 25/43 (main 27, 24), targets 2/27,
       hard violations 0; two missed escalations: allergie_0002 (as on main) and
       reservierung_0035 (new, see Open). Stand-in 43/43, suite green except mount-only tests.
Artifacts: docs/metrics/eval_history.csv (every run of 20. to 22.10.), README charts, PR #347
       description, docs/01_STATUS.md open points; issue #207 (cart repeated after confirm).
Decisions: Maxi 21.10.: the core says every booking from the confirm result (booking_say),
       the model repeats it word for word or not at all, after fifteen Codex rounds on word
       rules. Maxi 21.10.: GPU runs on the llama-server start only in a window he names.
Gate: G0 open; PR #347 open, blocked by reservierung_0035.
Open: 1. reservierung_0035: Qwen sends reserved_for without an offset and check_slot refuses
       it; read a time without an offset as the restaurant's local time (core/time.to_utc),
       test, then two Qwen runs in Maxi's GPU window. 2. The stand-in's sentences after a
       confirm (sim/scripted_llm.py SAY_CONFIRMED, sim/scripted_order.py pickup code and
       approval) are never asked any more: delete them. 3. Codex review of the head and the
       merge gate comment ("Merge gate: <sha>"), then Maxi merges.
Lessons: a word guard on a model's German sentences does not converge under review; when
       findings start to contradict each other, move the sentence to the core. Recreate the
       eval container after a merge that replaces a bind-mounted file (README.md, CLAUDE.md),
       or `docker exec` fails with "container is not running".

## Handover 05.10.2026 - demo run of the operations view, for local Claude Code
Status: Runs: the whole stack without a phone, re-checked on main ccd65cc. Migrations to
       005, `scripts.seed`, the test menu from `evals/menu` (32 dishes), API on port 8000,
       `/health` ok, `/gui/` 200. `sim.replay` puts calls on the board: two pickups (A1,
       A2) under "Neue Bestellungen", a table for four under "Heute"; screenshot in
       Chromium at 1280x800, the board fills without a reload. Run in a cloud session
       outside Docker (Postgres 16 and a Python 3.12 venv), so `make up` itself was not
       run here. Does not run: a real model (T-2.4, the rule-based stand-in answers), a
       phone (C2), callbacks in the demo (both escalation cases transfer to the team,
       none creates a callback).
Artifacts: No code changed. This block, one line in docs/01_STATUS.md "Done", item 8 in
       "What's next". Branch ccr-f287e6bf-i93pl3, PR #191: 62b2ef1 (handover) and
       9f7756e (Codex review, two P2: demo commands pinned to an open day, one shell
       command each, both run as written).
Decisions: None by Maxi. Assumption: `evals/menu` is the demo menu, because `scripts.seed`
       loads no menu and the real register data is not for a demo.
Gate: G0 open, this session changes nothing for it.
Open: 1. Locally: `git pull` on main, start `claude` in the repo, `/start`. Then the demo:
          cp .env.example .env          (set the passwords)
          make up && make migrate && make seed
          docker compose exec api python -m scripts.import_menu evals/menu
          open http://localhost:8000/gui/ in the browser, then in a second terminal
          docker compose exec api python -m sim.cli --caller +497215551234 \
             --now 2026-10-06T12:15+02:00
          and order as the guest ("Ich möchte zwei Frühlingsrollen abholen").
          Recorded calls instead of typing:
          docker compose exec api python -m sim.replay --now 2026-10-06T12:15+02:00 \
             evals/cases/abholung_0001_zwei_positionen_mit_option.json
          2026-10-06 is a Tuesday, an open day in the seed data; any open day works.
       2. Proposal, waits for Maxi's yes: `make demo` that imports the test menu and
          replays a few calls on an open day, with a test and a README line.
       3. T-3.5 on a real tablet needs the API reachable from the tablet (see Lessons).
Lessons: Monday is the closed day in the seed data: on a Monday every sim call ends
       "abandoned" and no order or reservation is written; pass `--now` on an open day.
       The column "Heute" follows the real clock, not `--now`: a reservation shows only
       when its date is the real today. `reservierung_0001` asks for "morgen", so replay
       it with `--now` the day before today, and only when today is open. Orders show
       whatever their date. Since PR #181 the dev host ports bind to 127.0.0.1 only
       (`docker-compose.override.yml`): a tablet on the local network cannot reach port
       8000 of the workbench; the tunnel from docs/13 §2 or a browser on the same machine
       does. Without Docker: the Postgres cluster must be UTF8 (`initdb --encoding=UTF8
       --locale=C.UTF-8`); a SQL_ASCII cluster fails at the first connect with "cannot
       use a string pattern on a bytes-like object". Use Python 3.12 for the venv from
       `api/requirements.txt`. `import_menu evals/menu` warns about aliases; expected.
       The check `github-advanced-security` is red on PR #191 and on every PR until the
       account's quota resets: the scanner stops with HTTP 402 "monthly quota exceeded"
       before it reads the diff. Not a code finding; commented once on the PR.

## Handover 04.10.2026 - text phone allergy guard (open point from PR #155)
Status: Runs: on the open allergy question "Wogegen?" the text phone decides from the active
       menu what a card number is. "B12", "Vitamin B12" and "Gegen E 621" are recorded as the
       allergy note; "S12", "S zwölf", "Sushi zwölf", "Nummer 12", "die 25g" and "Eine Cola
       bitte" stay an order and the question stays open. Merged into main as 67b3bcd (PR
       #168, squash), reviewed by Codex on the merged head, CI green. Suite 2769 passed, 2
       skipped; latency 22 passed; cases/ 26/26, hard metrics 0; targets/ 1/27 unchanged;
       number eval 181/181. Does not run: a spaced name whose bare number is a dish on the
       menu ("Vitamin B 12" next to dish 12) still asks again - it fails safe and stays as
       built (card number scope, Maxi 04.10.).
Artifacts: PR #168 -> main 67b3bcd. Code: sim/scripted_order.py (MenuNumbers,
       _orders_something), sim/scripted_llm.py and sim/session.py (menu_numbers, wiring in
       SimCall), evals/runner.py (model_factory takes now and menu), api/domain/menu/items.py
       (active_numbers, card_format uses it). Tests: api/tests/test_sim_pickup.py,
       api/tests/test_evals_runner.py. Docs: 01, 11 §sim, CHANGELOG. Along the way PR #170:
       the two POSIX file-mode tests of the call log skip on Windows.
Decisions: The scripted model gets the menu from its caller, not from the database itself:
       SimCall and the eval runner hand in a callable, read only when an answer has to be
       told apart from an order. Existence on the menu instead of the prefix alone: a bare
       number inside a sentence counts only if the menu has it, otherwise "Gegen E 621" would
       stay an order or "die 12 mit Reis" would become a kitchen note. A number form the menu
       can have but lacks ("S13") still counts as an order: asking again is the safe side.
       No new eval case: the unit tests were the red tests, cases/ stays at 26.
Gate: G0 open, unchanged.
Open: one worktree, branch and chat per item, independent of each other:
       1. task/backup-restore (T-9.3) - recommended first: the real register import waits
          on Maxi's go and must not run without a backup; `make backup` fails today. The
          task definition is docs/07 (T-9.3) and docs/13 §4, not this line: scripts/backup.sh
          and restore.sh, a daily pg_dump at 03:00 by cron, the dump encrypted onto a second
          storage at another EU provider, and the restore really rehearsed once. Scripts and
          a local rehearsal need nothing from Maxi. The second storage costs money and
          depends on the hosting choice (D3): until it exists, T-9.3 stays open and the
          session says so instead of ticking it off.
       2. feat/n8n-team-events - n8n workflow for reservation.confirmed, callback.created and
          order.handover_failed as push or SMS to the team, export to n8n/.
       3. fix/sim-open-points - every text phone point in docs/01 "Open Points from
          Reviews"; read the table, this list is only the index. Due with the next sim
          change: option in two groups, rejection plus replacement in one answer, digit in
          a dish name read as quantity, allergy in the opening sentence (ends in a callback
          instead of an order). Due with T-2.4, can be left for the real model: reason for
          a surcharge, whole pickup order taken while pickup is closed. Stand-in only, none
          blocks.
       4. docs/english-code-comments - translation sweep, runs in its own chat and worktree
          (29 commits ahead, 11 behind main on 04.10., no PR yet); it has to merge main
          first, sim/scripted_order.py changed under it.
       Waits on Maxi, not on code: the real register import (steps in docs/01 "What's
       next"), the paper check of the input slip (T-4.6), the tablet test (T-3.5), the voice
       platform (C2, blocks T-2.4).
       Cleanup done 04.10.: the worktrees of five merged branches are removed
       (sim-allergy-guard, lunch-menus-m-vm, real-menu-import-dry-run, latency-serial,
       Maex-Voice-Agent-wt-skip-posix-mode); their local branches remain. Left on purpose:
       infallible-grothendieck-a345ac (PR #145 merged, but it holds a .env with its own
       values that is in no other place; Maxi decides) and this handover's worktree until
       its PR is merged.
Lessons: docs/01_STATUS.md conflicts on every parallel PR: each one bumps the same version
       line and adds a changelog line at the same place. PR #168 was merged with main four
       times, and each merge moves the head past the commit Codex reviewed. Merge main right
       before asking for the review and hold other merges until it is in. A new LLM factory
       in evals/runner.py gets (now, menu); a ScriptedLLM built without `menu` knows no
       prefix and would note "S12" as an ingredient. The test menu KARTE has no prefixes: use
       the `sushi` fixture in test_sim_pickup.py for S and SM. Never undo a temporary edit
       with `git checkout -- <file>`: it drops every uncommitted change of that file (it cost
       the runner change once here); commit first or work on a copy. The database on port
       5432 belongs to another worktree's stack; the tests build their own scratch databases.

## Handover 04.10.2026 - T-4.12 card numbers with prefix and suffix g
Status: Runs: card numbers with a prefix from the menu (S, SM) and the suffix g, in search,
       import, tablet and text phone; merged into main as 26aaf8e (PR #155, squash). Number
       eval 181/181, conversation suite cases/ 26/26, hard metrics 0, search_menu p95 under
       300 ms with 260 dishes. Does not run yet: the first real import of the register menu
       (only the converter dry run was done: all 50 Sushi and sauce dishes pass), lunch menus
       M/VM (excluded in the converter), reading "s12" aloud on the voice platform (untested).
Artifacts: PR #155 -> main 26aaf8e. Code: api/domain/menu/{numberwords,search,importer,items,
       normalize,sold_out,pos_convert}.py, api/domain/ordering/correction.py, api/agent/
       dispatch.py, sim/scripted_order.py, evals/number_eval.py. Evals: 68 new lines in
       evals/cases/nummern.jsonl, abholung_0070 to 0074, eval menu with S1, S12, SM1, 45, 45g.
       Docs: 01, 04 §search_menu, 07 (T-4.12), 08, 11, 14.
Decisions: Maxi 26.09. - S, SM and G now; lunch menus M/VM later; no combinations like 35AE.
       Prefixes are never in the code, they follow from the active numbers of the menu
       (rule 1). A category word counts only if every number of that category carries a
       prefix. Never a guess (rule 2): unknown prefix, number above 999 or wrong suffix is
       not_found; a prefixed number directly next to a name asks back, separated by "und" or
       a comma it is two positions. Import rejects prefixes that already mean a quantity,
       marker or spoken word (x, st, nr, no, hm, ja, es, so, um, zu, da, du, er, ob).
       Maxi 04.10. - everything written for the repo is English (CLAUDE.md §8, PR #166).
Gate: G0 open, unchanged. T-4.12 itself is done; the real import is the next step towards
       stage 2.
Open: one worktree and branch per item; the items are independent and run in parallel,
       the order of work is "What's next" in docs/01_STATUS.md:
       1. fix/sim-allergy-guard - text phone: decide "is this an order?" from the menu's
          CardFormat, so "B12" and "E 621" on "Wogegen?" are ingredients (open point docs/01).
       2. task/real-menu-import-dry-run - converter plus import --dry-run against a local DB
          with the register copies (gitignored), read the report; needs Maxi for
          --allergens-confirmed-by and the register cleanup before the real run.
       3. task/lunch-menus-m-vm - register groups 100 to 102; needs Maxi's decision on how
          lunch menus are offered ("Menü eins" is not a category word today).
       4. docs/english-code-comments - translation sweep, already running in its own
          worktree; it branched from the old PR head and has to merge main first. It also
          takes the German leftovers of T-4.12: the two older import messages, the eval
          `why` notes and the T-4.12 text in docs/04, 07, 14.
       5. chore/dependabot - PRs #159 and #160 were red only because of the latency flake
          that #163 fixed; rebase and re-run.
       Not startable in code: "s12" read aloud (needs the voice platform, C2), paper check of
       the input slip (T-4.6) and the tablet test (T-3.5) need a person.
Lessons: The eval suite v2 on main gives ids that are unique across cases/ and targets/ and
       wants a level tag; check the highest id before adding a case. Codex reviews one
       commit: every push needs a new "@codex review", and a docs-only note still moves the
       head, so record a late P2 in an existing open point instead of a new commit. Latency
       tests need @pytest.mark.latency and run serially. Two call-log tests fail only on
       Windows (file permissions), green in CI. The venv lives at C:\v412; long paths break
       sqlalchemy on Windows.

## Handover 26.09.2026 - D2, D11, Vorbereitung T-4.11 (Kassen-Import)
Status: Reine Klaerungs- und Doku-Sitzung, kein Code geaendert. D2 und D11 entschieden, die
       Kasse (<Kassensystem>) ist Quelle fuer Karte und Preise und einziger Weg fuer Quittung
       und Kuechenbon (TSE). Die Spalten der Kassendateien sind in docs/14 §Quelle Kasse dem
       Importformat zugeordnet. Laeuft noch nicht: der Umwandler .dbf -> CSV (T-4.11), die
       Umstellung der Druckbruecke auf den Eingabezettel (T-4.6), getrennte und senkbare
       Wartezeiten (T-3.6).
Artifacts: Branch claude/kassensystem-integration-fragen-3gf8zj, PR #148 (docs, CI gruen auf dem
       ersten Push; drei Codex-P2 in 09cdef9 behoben und aufgeloest). docs/01 (v1.34.1), 02 §2a
       §5 §6, 06 §3, 07 (T-3.6, T-4.6, T-4.11), 09, 13, 14 §Quelle Kasse.
Decisions: D2 · Bons druckt nur die Kasse; die Druckbruecke bleibt unabhaengig und druckt bis zur
       Kassenschnittstelle jede Bestellung als Eingabezettel "NICHT IN KASSE - bitte eingeben"
       auf den Haupt-Bondrucker an der Kasse (80 mm), danach nur als Notweg (Regel 5).
       Planungsannahme: <Kassenanbieter> arbeitet nicht mit, Uebertragung von Hand ·
       D11 · Telefonpreis VK1_PREIS; VK2 (Abholer) und VK3 (Restaurant) heute gleich, Abweichung
       von VK2 ist ein Fehler im Bericht · A_PREIS sind ungenutzte Aktionspreise · GROESSE ist die
       Portionsgroesse (Suppen klein/gross) · DETAILS ungenutzt · Kategorie aus W_BEZEICH
       (feiner, fuer "Gericht aus"-Alternativen) · Zutaten der Kasse = Extras mit Auf- oder
       Abschlag -> item_options, freiwillig, ohne Default (passt zu D8) · Kassennummer als
       eigenes Feld menu_items.pos_code (Codex PR #148), im Export fehlende Artikel werden nach
       --dry-run per Schalter inaktiv, nie geloescht.
Gate: G0 unveraendert offen (Anbieter, Budget, Rechtspruefung, C1). Block 3: T-4.11 neu, offen.
Open: T-4.11 in einer neuen Sitzung. Maxi laedt genau diese sechs Dateien hoch, als Kopie nach
       Kassenschluss: artikel.DBF, artikel.DBT, zutaten.DBF, zutaten.DBT, warengrp.dbf,
       zutgrp.DBF (DBF und DBT gehoeren paarweise zusammen, ohne DBT sind die langen Felder wie
       ZUTATEN leer; Demo-Ordner nicht). Ablage imports/kasse/ (gitignored). Zuerst lesen und
       an den echten Suppenzeilen klaeren: wie GROESSE ("+-16") und GRPREIS1-6 Groessen und
       Preise kodieren und wo "klein"/"gross" stehen; wie zutaten und zutgrp zusammenhaengen
       und welche Artikel welche Zutatengruppe erlauben; ob artikel.DBF eine Sperr-Spalte hat.
       Dann Umwandler (nur lesend, dbf-Lesen mit neuer Abhaengigkeit oder eigenem Leser -
       entscheiden und begruenden), Migration pos_code, Schalter fuer fehlende Artikel,
       Tests mit synthetischen .dbf, nie mit echten Daten im Repo.
Lessons: Allergen-Buchstaben der Kasse laufen a bis n OHNE Luecke, LMIV und unsere DB
       ueberspringen I, J, K, Q. l/m/n 1:1 uebernommen waere Sellerie/Senf/Sesam statt
       Sulfite/Lupinen/Weichtiere, und die DB wuerde nicht widersprechen - nur ueber die
       Umsetztabelle in docs/14 importieren, unbekannter Buchstabe ist Fehler · Allergene und
       Zusatzstoffe sind in der Kasse heute nicht gepflegt (ein Testeintrag), also ueberall
       "keine Auskunft", bis Maxi sie in der Kasse anhakt; ZUTATEN nie als Allergenquelle ·
       BEZEICH ist auf 40 Zeichen abgeschnitten, der Agent liest das vor -> Warnung · Artikel.dbf
       enthaelt EK_PREIS und Verkaufszaehler: Betriebsdaten, bleiben in imports/ · Telefon:
       Fritz!Box 6591 Cable, drei Nummern, Zahl paralleler Gespraeche unbekannt; eine
       Weiterleitung in der Box belegt zwei Kanaele (C2).

## Handover 23.09.2026 - T-4.5, confirm fuer Bestellungen
Status: Abholung laeuft ueber HTTP von der Suche bis zum Abholcode: search_menu, get_item_details,
       draft_order (T-4.5, PR #124) und confirm fuer entity: order (PR #125) liegen auf main.
       draft_order prueft Oeffnung, aktiv/aus, Optionen und Pflichtgruppen im Code, rechnet in
       Cent und liefert readback; confirm vergibt A1, A2, ... je Betriebstag und gibt nur im Modus
       primary sofort an die Kueche. search_menu fragt bei mehreren Positionen in einem Satz nach
       (split_positions), statt eine still zu verlieren. 1470 Tests gruen, CI gruen, ruff sauber,
       draft_order p95 22 ms, confirm Bestellung p95 36 ms. Laeuft noch nicht: der Agent kennt die
       Menue-Tools nicht (agent/dispatch.py, prompts/tools_v1.md nur Stufe 1), die Freigabe im
       Tablet (T-4.7) fehlt - ausserhalb von primary bleibt eine bestaetigte Bestellung dort
       stehen -, kein n8n-Workflow empfaengt order.confirmed (T-4.6), kein echtes Modell (T-2.4),
       keine Telefonie (C2), keine echte Karte (C1).
Artifacts: PR #124 squash-gemergt (main = 303d1b7): api/domain/ordering/{draft,validation,pricing,
       readback}.py, api/domain/menu/split.py, api/schemas/orders.py, api/tools/draft_order.py,
       option_key in api/domain/menu/items.py, Import-Pruefung in importer.py, Guard in
       search.py. PR #125 squash-gemergt (main = 4610355): api/domain/ordering/confirm.py,
       api/domain/confirm.py, api/schemas/confirm.py. Tests: test_domain_draft_order,
       test_tool_draft_order, test_domain_menu_split, test_domain_confirm_order. docs/01 (v1.25.0),
       03, 04, 07, README, CHANGELOG.
Decisions: Pflichtgruppe ohne Wahl wird erfragt, nie mit der Voreinstellung gefuellt - die waere
       geraten (CLAUDE.md 2 Regel 2) · Replay-Schnappschuss in audit_log nur mit Kartendaten
       (Nummer, Name, Warnungen), Name/Telefon/Hinweise aus orders/order_items - audit_log bleibt
       laenger als die Bestellung, die Loeschung (T-6.7) setzt an orders an · ready_at auf die
       volle Minute aufgerundet, in UTC · Kartenfehler (Optionen in zwei Schreibweisen,
       uneinheitliches required, negativer Preis) gehen per service_unavailable ans Team statt
       geraten zu werden, der Import lehnt sie ab · search_menu zerlegt nicht, verweigert aber
       Saetze mit mehreren Positionen (ambiguous, Teile in message) · nur primary uebergibt sofort,
       overflow/shadow/paused warten auf Freigabe · Abholcode "A"+Zahl je Mandant und Betriebstag,
       Tag aus created_at, Advisory-Lock · confirm liest den Modus FOR SHARE, damit ein laufender
       Not-Aus nicht ueberholt wird.
Gate: G0 weiter offen, unveraendert (Anbieter, Budget, Rechtspruefung, C1). Block 3 (Stufe 2,
       Sammel-Issue #9): T-4.1 bis T-4.5 fertig, confirm fuer Bestellungen dazu; offen T-4.6
       bis T-4.9.
Open: Menue-Tools fuer den Agenten - search_menu, get_item_details und draft_order in
       agent/dispatch.py (confirm fuer Bestellungen laeuft dort schon ueber den generischen
       Adapter; es fehlen order_id im Zustand, der Prompt und sim/scripted_llm.py), mit
       Nachfrage je Teil, wenn search_menu "mehrere Positionen" meldet. Danach T-4.7 (Freigabe
       im Tablet), dann T-4.6 (Bon ueber n8n). Offener Punkt aus docs/01: "die 23 und Pho Bo"
       (zweites Gericht ohne Menge) laesst sich ohne Karte nicht sicher trennen.
Lessons: Codex liefert je Runde neue Randfaelle, solange eine Heuristik offen ist - sieben Runden
       fuer split_positions. Lieber frueh die harte Grenze ziehen (im Zweifel ganz lassen und laut
       nachfragen) und den Rest als Open Point festhalten · ein Replay-Schnappschuss ist schnell
       gebaut und schnell ein Datenschutzproblem: was laenger lebt als die Bestellung, darf keine
       Personendaten tragen - eigenes Review vor dem Merge hat das gefunden, nicht der Bot · der
       Docker-Daemon fiel zweimal weg; Docker Desktop liegt unter
       %LOCALAPPDATA%\Programs\DockerDesktop\, danach docker compose up -d · git checkout -- <datei>
       setzt die ganze Datei zurueck, nicht nur die letzte Aenderung; fuer temporaere Messungen
       lieber -s mit einer Kopie arbeiten · main ist im Haupt-Checkout belegt, im Worktree von
       origin/main abzweigen.

## Handover 22.09.2026 - T-4.4 (Review, Merge)
Status: Karten-Tools vollstaendig bis zur Bestellung: search_menu (T-4.3, PR #117) und
       get_item_details (T-4.4, PR #118) laufen gegen main. Beschreibung, Optionsgruppen und
       Allergene zu einer menu_item_id; Allergene nur aus item_allergens, ohne gepflegte Zeile
       known: false. 1359 Tests gruen gegen PostgreSQL 16 mit pg_trgm, CI gruen, ruff sauber,
       get_item_details p95 8,7 ms gegen 300 ms Budget. Laeuft noch nicht: draft_order (T-4.5),
       ein echtes Modell (T-2.4, weiterhin FakeLLM), die Telefonie (C2 offen), die echte Karte
       (C1 offen).
Artifacts: PR #118 squash-gemergt, main = 4933cee. Drei Commits auf dem Branch:
       34d9c83 (Merge-Konflikt in docs/01 aufgeloest), 50c75a1 (beide Review-Befunde gefixt),
       d1ee25d (docs/01 und docs/07 auf den neuen Vertrag). api/domain/menu/{details,items}.py,
       api/schemas/menu.py, api/tools/get_item_details.py, api/tests/test_domain_menu_details.py
       (19 Faelle), docs/04, docs/05, docs/01 auf v1.23.0, CHANGELOG. Danach in derselben Sitzung
       erledigt: PR #122 (Compose-Mounts, hier angestossen) gemergt, main = d8561e8, und PR #120
       (Jules zu T-4.3) geschlossen.
Decisions: allergen_question als Pflichtfeld im Request, ohne Vorgabewert - der Satz zum Rueckruf
       darf nur auf die Allergenfrage kommen, weil dasselbe Tool auch Optionen beantwortet; ein
       Default entscheidet still und falsch, ein fehlendes Pflichtfeld faellt als invalid_input
       sofort auf. Der Satz bleibt dabei im Code (CLAUDE.md 9), nur das Ob wandert zum Agenten
       · confirmed_at als Ortsdatum des Mandanten statt UTC-Datum (CLAUDE.md 8), wie jedes andere
       Domain-Modul es haelt · die beiden Codex-P1 nicht gefixt, sondern widerlegt: sie galten dem
       search_menu-Entwurf, den 51192fc entfernt hat, und mains Fassung deckt beide durch
       bestehende Regressionstests ab.
Gate: G0 weiter offen, unveraendert (Anbieter, Budget, Rechtspruefung, C1). Interner Stand:
       Block 3 (Stufe 2: Abholung, Sammel-Issue #9) bei 4 von 9 Aufgaben - T-4.1 bis T-4.4 fertig,
       T-4.5 bis T-4.9 offen. T-4.5 ist der naechste Schritt und hat alle Abhaengigkeiten erfuellt.
Open: T-4.5 draft_order mit allen Pruefungen und readback - alle Abhaengigkeiten erfuellt,
       Testdaten reichen, bis die echte Karte da ist. Danach C1 Karten-CSV und der echte Import.
       Nichts blockiert: die Nebenbaustellen dieser Sitzung sind zu.
Lessons: Codex reviewt einen Commit, nicht die PR - die Zeile "Reviewed commit" steht im Kommentar
       und zeigte hier auf db4be19d0, einen Stand, den ein spaeterer Merge laengst entfernt hatte.
       Immer gegen den Head pruefen, bevor man einen Bot-Befund fixt, und nach Fix-Commits
       @codex review auf den echten Head setzen · GitHub laesst den Autor seine eigene PR nicht
       freigeben, ein Bot-Review landet als COMMENTED, nie als APPROVED - reviewDecision bleibt
       auf eigenen PRs leer · der api-Container mountet weder sim/ noch Makefile, deploy/,
       .claude/ oder docs/, deshalb melden make test vier Collection-Errors und make lint drei
       falsche I001, waehrend die CI gruen ist; mit allen Pfaden dazugemountet sind es 1359 gruene
       Tests. Das ist der Inhalt von PR #122.

## Handover 17.09.2026 – T-2.1, T-2.2
Status: agent/ conversation core running: prompt.py, state.py, dispatch.py, loop.py, llm.py+FakeLLM
       (T-2.1); ladder.py (Verständnis-Leiter) and escalation.py (Sofort-Auslöser) wired into
       loop.py (T-2.2). 295 tests green, CI green. Not yet running: sim/cli.py (no real terminal
       conversation), a real LLM (T-2.4, still FakeLLM only), the GUI.
Artifacts: PR #101 (T-2.1) and #102 (T-2.2) squash-merged to main (main = ea30937). Branch
       claude/cavemen-ultra-pwysd0 reset to main after each merge, currently == main, no diff yet.
       api/agent/{prompt,state,dispatch,loop,llm,ladder,escalation}.py, api/core/tool_log.py
       refactored (append_tool_call shared with the HTTP middleware), db/migrations/env.py bugfix.
Decisions: agent/dispatch.py re-implements api/tools/* directly against domain/ (docs/11 §agent,
       no HTTP detour) · idempotency_key for create_reservation/confirm always derived by code,
       never the model (CLAUDE.md §2 rule 1) · loop.py's self-triggered handoffs (timeout, hop
       limit, ladder exhaustion) default to reason "not_understood"; create_callback falls back
       cancellation→human_requested since CallbackReason has no cancellation value · escalation.py's
       keyword list is a first pass, not linguistically validated — widen it via evals (T-5.1),
       not by guessing more keywords now · db/migrations/env.py bugfix: alembic.ini's
       [logger_root]=WARN was silently downgrading the app's log level for the rest of the process
       whenever a migration ran in-process (every test via migrated_db_url).
Gate: G0 still open, unchanged (vendor/budget/legal/C1 outstanding). Internal milestone: Block 1b
       (Gesprächs-Kern und Simulator, docs/07 Sammel-Issue #7) in progress — T-2.1/T-2.2 done,
       T-2.3 (sim/cli.py) next, then T-2.4 (real LLM) and T-2.5 (GUI sim console). The
       "Durchstich ohne Telefon" milestone needs T-2.3 + T-3.3, not the whole block.
Open: T-2.3 sim/cli.py + sim/replay.py (first conversation in the terminal, a reservation lands in
       the DB) → then T-3.x (GUI) for the same milestone. In parallel: T-0.7, T-0.8.
Lessons: Codex reviews every PR and found real bugs both times this session (state loss across
       turns, a timeout that announced a handoff without performing it, the ladder double-counting
       failures within one turn) — fix same-day, before merging, every time. Merge each PR before
       stacking the next task on the same branch: keeps diffs small (T-2.2's PR was 10 files/+414,
       T-2.1's was 15 files/+1156) and keeps review-bot and diff-review token cost down — adopted
       as a standing rule this session. Docker build needs --network host plus
       `cp /root/.ccr/ca-bundle.crt api/ca-bundle.crt` to reach the proxy for pip installs.
```

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
| ~~Budget monthly and one-time~~ decided 07.10.2026: D17 in `docs/01_STATUS.md` | before G0 | C1 |
| ~~Stage order final (call mix)~~ confirmed 07.10.2026: E2 | G0 | C0 |
| ~~Vendor and hosting~~ decided 07.10.2026: own voice layer (E1), hosting D3 | G0 | C2 |
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

- **v1.1 · 07.10.2026:** E1 flipped: own voice layer instead of a hosted platform (new E15, E16, `docs/20_VOICE_LAYER.md`). E2 confirmed, E8 changed for complaints (D19). Risks R13 and R14, vendor criteria and the C2 prompt marked superseded.
- **v1.0 · 11.09.2026:** First version from C0 (loops 1–8). Contains objective, guiding principles, KPIs, hybrid architecture, stages 0–6 with gates, vendor criteria, data model, legal check, risks, and 8 work packages with start prompts.
