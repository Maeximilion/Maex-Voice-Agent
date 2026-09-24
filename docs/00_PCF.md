# PCF – Maex Voice-Agent

> **AI Call Intake for Delivery, Pickup, and Reservations**
> Version 1.0 · 11.09.2026 · Status: released (Gate P passed)
> This file is the Single Source of Truth for all project chats. Each chat reads it at the start and ends with a handover block (section 12).

---

## 1. Objective

An AI agent answers calls on the landline of <Pilotbetrieb>. It correctly handles reservations, pickups, and deliveries, hands off to kitchen, register, and team, and escalates complaints to humans. The interface is simple enough that the team operates it under stress without explanation.

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
| E5 | Pilot <Pilotbetrieb>, number stays; menu, hours, zones from DB | Transferable to other locations later | Assumption, holds until veto |
| E6 | Training = knowledge base + eval suite + offline shadow mode, no model fine-tuning | Cheaper, measurable, legally simpler | Assumption, holds until veto |
| E7 | AI discloses itself as AI at the start of the call | EU AI Act Art. 50, in effect since 02.08.2026 | Mandatory |
| E8 | Complaint, human request, cancellation → team immediately, else callback task | Trust, error containment | Assumption, holds until veto |

---

## 4. Architecture (Hybrid)

```text
Caller
  │
  ▼
Landline <Pilotbetrieb> (existing provider)
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
| `find_customer` | known customer and number of saved addresses for caller number, never name or address to the model (E13) | only if number provided |
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
