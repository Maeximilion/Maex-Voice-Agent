# Go-live checklist per customer (#575)

> One copy per restaurant, filled in on a second device or printed. Filled copies stay out of the repository (they name the restaurant and the people). Every step names a command, a screen or a document section that exists today; a step that is not built yet says so with its issue number.
> Hard rule 5 applies at every step: if anything fails, the phone rings at the team. Do not go on to the next stage while the rollback of section 4 is untested.

| Field | Entry |
|---|---|
| Restaurant (`<Pilotbetrieb>` in drafts) | |
| Customer key (stack folder, Compose project `maex-<key>`) | |
| Done by, started on | |

---

## 1. Before go-live (technical)

### 1.1 Stack and tenant

- [ ] Stack created on the EU server: `python3 scripts/new_stack.py <customer.json> --domain <domain> --dry-run`, then without `--dry-run`, outside service hours (the shared Caddy is recreated, docs/13 §8 "What this means for the other issues"). Customer file shaped like `deploy/customer.example.json`, kept with the stack on the server, never in the repository.
- [ ] Per-customer installer with a self-test: not built yet: #574 (`new_stack.py` above is the stack part, #573).
- [ ] Inside the new stack's api container: `python -m api.jobs.provision check` lists what the tenant still lacks (menu, zones, print bridge). It must report nothing missing.
- [ ] Mode is `shadow` after provisioning (`provision create` writes it that way); confirm in the tablet header ("KI hört mit").
- [ ] Team number: `TEAM_PHONE` in the stack's `.env` is E.164 and is the phone that rings at the team; extra numbers in `TRANSFER_TARGETS` (docs/13 §0a, `.env.example`).
- [ ] Access: each tablet signed in with the setup link of the first login, restaurant password and team PIN set (docs/13 §0a, `docs/23_ACCESS_SECURITY.md`).
- [ ] Dashboard checks that run these items automatically: not built yet: #571.

### 1.2 Menu and allergens

- [ ] Menu CSVs prepared per `docs/14_MENU_IMPORT_FORMAT.md`; from a register export: `python -m scripts.kasse_to_csv imports/kasse --out imports --allergens-confirmed-by <Name>` (docs/14 "Quelle Kasse").
- [ ] Dry run clean, warnings read (dishes without alias, aliases leading to two dishes): `python -m scripts.import_menu imports/ --dry-run` (docs/14 "Prüfregeln des Importers"), then the same command without `--dry-run`.
- [ ] Allergens: every dish either has a list confirmed by a named person (`--allergens-confirmed-by`) or stays "no information" (empty row in `item_allergens.csv`). Letters without a confirmation are an error by design; no dish carries a claim in its name without a confirmed list (docs/14, check 1 "Claim without allergens").
- [ ] Prices checked against the register: `python -m scripts.price_check imports/kasse --dry-run` (docs/14 "Preisabgleich").
- [ ] Menu reviewed by the restaurant on the tablet (dishes, options, aliases); name of the reviewer in the sign-off table.

### 1.3 Hours, zones, service configuration

- [ ] Weekly hours and seats written by `provision create` from the customer file; checked on the tablet or against the customer file.
- [ ] Delivery zones: `python -m scripts.seed_zones imports/zones.csv --dry-run`, then without `--dry-run` (columns in docs/14 `zones.csv`). Skip if the restaurant does not deliver.
- [ ] Service configuration read once: waiting times, `order_approval_above_cents`, `overflow_needs_approval` (docs/02 §4, docs/09 §4).

### 1.4 Print bridge

- [ ] Bridge installed on the register PC and its settings entered (`MAEX_SERVER_URL`, `MAEX_KITCHEN_TOKEN`, `MAEX_TENANT_ID`, `MAEX_PRINTER`): `printbridge/README.md` "Settings" and "Setting it up on the register PC".
- [ ] Test slip printed and photographed: `python -m printbridge --test` (`printbridge/README.md` "Commissioning"); then `python -m printbridge --once` against the stack.
- [ ] The tablet shows no red bar "Druckbrücke meldet sich nicht".

### 1.5 Backup and restore rehearsal

- [ ] Backup run by hand in the stack's folder: `bash scripts/backup.sh`; cron line in place (`deploy/backup.cron`, docs/13 §4).
- [ ] Restore rehearsed on that dump: `bash scripts/restore.sh <dump file> --check` reports table count and schema revision and drops the scratch database again (docs/13 §4 "Restore").
- [ ] Before any later production migration: backup first (CLAUDE.md §8).

### 1.6 Phone line and outage drills

- [ ] Test phase: the AI telephone is an IP telephone at the restaurant's router on a number customers do not call, a team telephone assigned to the same number (docs/20 §3). Never the main number before operation.
- [ ] Operation entry (SIP trunk, one number per restaurant): not built yet: T-10.12 (#404).
- [ ] Outage map tried on that line, every row rings the team within 30 s (docs/20 §5 and the handover "T-10.2, the outage map on the wire", §10). Software drill without a line: `python3 -m scripts.voice_drill` (docs/20 §10, "T-1.11, outage drills").
- [ ] Push notifications reach the owner and the team devices (docs/09 §2 "Alarme für Maxi", `n8n/README.md`).

### 1.7 Security audit trigger

- [ ] `bash scripts/audit_due.sh` run; a component that becomes reachable from outside for this customer (new public path behind Caddy, SIP trunk, new published port) has a recorded audit before go-live (docs/09 §5 "When an audit is due"). Maxi decides; an open P1 finding blocks.

### 1.8 Legal items, per restaurant

Pointers only; the texts are Maxi's (CLAUDE.md §10).

- [ ] Every duty in the table "Where every duty stands" of `docs/09_OPERATIONS_LEGAL.md` §1 is read for this restaurant. A duty marked "first real call" blocks every real call in any mode; the others block the step they name.
- [ ] The drafts the restaurant fills in or signs are in `docs/vorlagen/` (privacy notice, record of processing, data-protection officer notice, delivery terms, team information, procedure documentation, allergen list). Who fills in what: the "Whose" column of that table.
- [ ] Recording stays off (`RECORDING_ENABLED=false`) until the team consents and L2 is met (docs/09 §1, L2 and L11).

---

## 2. Staged switch-on

Each stage ends with evidence in the sign-off table. Advance only after the previous stage's evidence exists. Mode is switched in the tablet header ("KI hört mit" = `shadow`, "KI springt ein" = `overflow`, "KI nimmt an" = `primary`); a step towards more AI asks once, a step back is one tap (docs/09 §2 "Modus", docs/02 §4).

| Stage | What | Evidence to collect |
|---|---|---|
| 1. `shadow` | The AI takes no call; the team phones as before. Calls reaching the AI line are handed to the team without a word. Recording only if switched on and consented. | Test calls from a number outside the restaurant ring the team telephone (call log entries on the tablet); outage rows of section 1.6 tried; the day report of the next morning (docs/09 §2) arrives; no alarm of `ops` unexplained. |
| 2. `overflow` | Team rings first, the AI takes over when nobody answers; every AI order needs "Passt" on the tablet (`overflow_needs_approval`). Who rings first is the router's job until the trunk exists (docs/02 §4, docs/09 §2 "Modus"). | A set of test calls through the real line (pickup, delivery, reservation, a complaint, an allergy question) logged in `docs/17_ANRUFPROTOKOLL.md` format; slips print; every order approved by a person and entered at the register; number of calls the team had to take over after the AI started. |
| 3. `primary` | The AI answers first, the team stays reachable. | Same evidence over a service period, red cards and open callbacks at the end of the day zero; cost of the day within `ALARM_COST_PER_DAY_CENTS`; Maxi's decision recorded. Turning off the approval step is its own decision (docs/09 §4, after G5). |

The operating test and the outage tests stay with the evals of `docs/08_EVALS.md`: a change of prompt or code in the conversation path since the last run needs a new eval run before the stage moves (CLAUDE.md §2 rule 4).

---

## 3. The team

- [ ] Briefing sheet held: `docs/vorlagen/team_einweisung.md` (T-8.5); everyone present tapped "KI pausieren" and "KI einschalten" once; sign-off line on the sheet filled in; Maxi's number written in.
- [ ] Team information sheet and written consents where recording or the team's own calls are involved: `docs/vorlagen/team_information.md`, docs/09 §1 L11 and N3 (AI literacy: briefing held).
- [ ] Who is called when the agent fails: the team telephone first, always (`TEAM_PHONE`, outage map docs/20 §5); the number for the tech contact (Maxi) is on the briefing sheet; named person per shift: ____________.
- [ ] Operating test at the new site (T-3.5, protocol `docs/vorlagen/bedientest_protokoll.md`): what repeats per customer is section 0 of the protocol (real tablet in its place, signed in, sound on, phone ringing nearby) and one pass by a team member of this restaurant, 5 minutes, without explanation. The isolated test stack (`python3 -m scripts.bedientest_fixture up`) is a workbench tool; the whole two-tester protocol of T-3.5 is not repeated unless the tablet layout or the board changed since the last run. Filled sheets stay out of the repository.
- [ ] Every morning, one minute (sheet section 4): mode, waiting times, callbacks, red cards.

---

## 4. Rollback in one step

Goal: calls ring the team's phone again and the AI takes nothing, at any stage.

1. **First grab, at the tablet:** header, "KI PAUSIEREN" (mode `paused`). Every call goes to the team at once, before any diagnosis (docs/09 §2 "Not-Aus"). "KI einschalten" reverses it.
2. **Back to listening:** set the mode back to "KI hört mit" (`shadow`): one tap, no confirmation (docs/09 §4 "Rückwege"). In `shadow` a call that reaches the AI line is transferred to the team without a word (docs/02 §4).
3. **Take the line away from the AI (test phase):** at the router, remove or unassign the AI telephone from the test number; the team telephones assigned to the same number ring as before (docs/20 §3, row "When the software is off"). Stopping the AI telephone does the same: `... --profile voice rm -sf voice asterisk wireguard` with the compose files of the T-10.2 handover (docs/20 §10).
4. **Operation with a SIP trunk:** switch the restaurant's forwarding back in the carrier's network: not built yet: T-10.12 (#404). Until then no restaurant runs on the trunk, so there is nothing to switch.
5. **Software versions:** previous prompt version from `prompts/` and restart; migration: backup first, `alembic downgrade -1` (docs/09 §4).
6. **Database lost:** "KI pausieren", then `scripts/restore.sh` (docs/09 §2 "Störungen", docs/13 §4).

- [ ] Rollback tried once for real before stage 2: pause on the tablet, a test call rings the team within 30 s, "KI einschalten", then the same for step 3 (rule of docs/09 §4: no step forward without a tested way back). Time measured: ____ s.

---

## 5. Sign-off

| Item | Who | Date | Evidence |
|---|---|---|---|
| Stack created, `provision check` clean (1.1) | | | output saved with the stack |
| Menu imported, reviewed by the restaurant (1.2) | | | dry-run report, reviewer name |
| Allergens confirmed or "no information" (1.2) | | | `--allergens-confirmed-by` name, claim check report |
| Hours, zones, service configuration (1.3) | | | |
| Print bridge test slip (1.4) | | | photo of the slip |
| Backup run and restore rehearsal (1.5) | | | `restore.sh --check` output |
| Outage map tried on the line (1.6) | | | drill report, call log |
| Security audit trigger checked (1.7) | | | `audit_due.sh` verdict, audit log entry |
| Legal duties of docs/09 §1 read for this restaurant (1.8) | Maxi | | status table, drafts signed or open (named) |
| Team briefing held (3) | | | sheet section 6 |
| Operating test pass at the site (3) | | | verdict line in the status note |
| Rollback tried (4) | | | time measured |
| Stage 1 `shadow` done (2) | | | |
| Stage 2 `overflow` done (2) | | | |
| Stage 3 `primary` decided (2) | Maxi | | |
