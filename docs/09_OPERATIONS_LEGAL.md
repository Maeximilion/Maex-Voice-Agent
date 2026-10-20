# 09 – Betrieb und Recht

---

## 1. Rechts-Check

> Decided 07.10.2026 (D20, Maxi): the checklist is worked through by putting the same question catalogue to two AI models independently (Gemini and Claude) and comparing the answers until every point is settled; no lawyer, no data protection consultant. Limits: this is not legal advice, two models can share the same error, liability stays with the controller. The question of the data protection officer (D9) stays open, because that role needs a person or a firm.
> **The questions are answered (20.10.2026); the duties below are not done.** A duty marked "first real call" blocks every real customer call in any mode; the rest block the step they name. Every open duty blocks the T-7.x tasks and the switch to mode `overflow`.

### Result of the check (20.10.2026)

Both rounds answered the form "G0 input: legal check" (basis `main` at 13b148e) independently: Gemini first, then Claude in a session that did not see the Gemini answers. They agree on 14 of the 18 questions. Maxi decided the four open points (L8, L13, L15, D9) on 20.10.2026. D9 and the topics U1 to U8 were answered by the Claude round only and are recorded as proposals. Both round files stay outside the repository. Not legal advice; the limits of D20 apply.

| No. | Question | Settled answer | Duty, and before which step | Tracked in |
|---|---|---|---|---|
| L1 | AI Act Art. 50: AI notice in the first sentence enough? | Yes, when the code speaks it first, it cannot be talked over and says "KI"; a question like "Sind Sie ein Mensch?" always gets a truthful answer. Art. 50(1) applies since 02.08.2026, not moved by the AI omnibus (VO (EU) 2026/1744) | Eval for "Sind Sie ein Mensch?"; first real call | T-11.5 |
| L2 | Recording: what to announce, is a way to object enough? | No: opt-out is not enough. The caller says an explicit "ja" before any recording starts; on "nein" or silence the call goes on without recording, a human is an extra, never the price of saying no. Announce: that it is recorded, the purpose, 30 days, voluntary, how to stop ("Aufnahme stoppen"), where the privacy notice is | Consent dialogue, consent log per call, stop on request and at the handover to the team; before mode `shadow` with recording | T-11.3 |
| L3 | Legal basis per purpose | Order, reservation, live speech processing: Art. 6(1)(b). Caller recognition, call statistics, call log, cost logs: Art. 6(1)(f). Tax records: Art. 6(1)(c). Recording, transcripts and their analysis: Art. 6(1)(a). Own allergy: Art. 9(2)(a) (L13). § 26(1) BDSG is not relied on (CJEU C-34/21) | Into the record of processing (L9) and the privacy notice (L4); first real call | Maxi |
| L4 | Information duty: short announcement plus privacy notice | Two layers. In the call: controller, AI notice, where the full notice is (`example.com/datenschutz`). On the website: everything of Art. 13(1) and (2): controller and DPO contact, purposes with legal bases, processors by category, transfers, periods (docs/03), rights, complaint to the authority, the call log | Privacy notice online, greeting extended by the pointer; first real call | Maxi; T-11.5 |
| L5 | Data processing agreements | An Art. 28 contract with every processor before it sees real data: EU server, backup storage, speech-to-text, text-to-speech, text model, push service if external. Each forbids training on our data. The SIP trunk carrier carrying the call is its own controller under telecom law, a processor only if it records or stores for us. None needed in the test phase | Per service, before its first key is used on real data | Maxi |
| L6 | D21: is streamed live audio without storage a recording (E11)? | No, when the service keeps audio and transcript only for the request, stores nothing afterwards and does not train on it. It is processing under Art. 6(1)(b), with a DPA and a line in the privacy notice | Choose services with zero retention, written into the DPA; provider-side audio logging off; before the first real call on a speech service | Maxi (D15, D18) |
| L7 | Third-country transfer | Yes, check whenever a processor or sub-processor processes or can reach data outside the EU, also by support access of a non-EU parent; an EU region alone does not settle it. Basis: DPF certification or SCCs with a transfer impact assessment | Per vendor, before its first real call (D15 before G1) | Maxi |
| L8 | Deletion concept and periods | Periods decided by Maxi (90-day set), see docs/03 "Löschkonzept". The concept is implemented only once the deletion job exists and is tested | Deletion job with tests; first real call | T-11.2, T-11.7, T-6.7, T-9.3 |
| L9 | Record of processing activities | Mandatory: the Art. 30(5) exemption does not apply (not occasional, includes Art. 9 data) | Written before the first real call, kept outside the repository like the DSFA draft | Maxi |
| L10 | Data protection impact assessment | Mandatory (DSK must-list no. 11, AI that talks with customers) and to be finished with the DPO's advice before the first real call in any mode, not only before recording | Finish draft v0.1 with this result; first real call | Maxi (D9) |
| L11 | Team: informed, consent to recordings | Every team member gets a written information sheet. Recording stops at the handover to the team, so no team voice is recorded and no team consent is needed; recording a team voice would need a written, voluntary consent per person. The audit log is never used to rate staff | Information sheet before the first real call; stop at handover with T-11.3 | Maxi; T-11.3 |
| L12 | E12: balancing for recognition by caller_id | Recognition passes the balancing under Art. 6(1)(f) on conditions: the agent never says stored data aloud unprompted and only asks ("An dieselbe Adresse wie beim letzten Mal?"), a name that does not match means a new customer record, an objection (Art. 21) switches recognition off for that number | Balancing as one page in the DSFA; rules built with `find_customer` | Maxi; T-6.2 |
| L13 | E14: own allergy as kitchen note | Health data (Art. 9); needs explicit consent. Decided by Maxi: the agent asks "Darf ich die Allergie als Hinweis für die Küche notieren?"; on "ja" the note keeps the D24 wording "WICHTIG: Keine <Zutat>. Grund: Allergie", on "nein" only "ohne <Zutat>" without a reason. The note text is deleted at the end of the business day after pickup or delivery | Consent question, consent log, deletion of the note text; before an order that can carry an allergy reaches a real caller (G1) | T-11.1, T-11.2 |
| L14 | E11: may transcribed text go into the repository? | Raw transcripts never: they stay personal data, and a git history cannot be erased. Only sentences that went through an anonymisation step (no name, number, address, date, time, health detail, no `call_id`) may go in. CLAUDE.md §8 stays as it is until such a step exists | Anonymisation step before the first transcript leaves the server | T-7.x |
| L15 | E10: call log, is 90 days right? | Yes, 90 days (D10), and E10 stays. Decided by Maxi: the log records only the hour of a call, not the minute, so a line can hardly be matched to the router's call list | Hour window in the log; the privacy notice names the log once it is online | T-11.4; Maxi |
| L16 | Piper licence (GPL-3.0-or-later) | No duty while the software runs only as our own service; GPL duties start when a copy is handed to anyone else. The service `voice` imports Piper in-process (`api/telephony/adapters/tts_piper.py`), so a shipped `voice` would be one combined work | Before any copy leaves our control (E16): Maxi picks Piper behind a process boundary, `voice` under the GPL, or Piper not shipped | Maxi, at E16 |
| L17 | Allergens (LMIV): only maintained values, otherwise callback? | Yes for the wording, but a phone order is distance selling: allergen information must be available before the contract, so the handover to the team or the callback comes before `confirm` when the guest asked beyond the maintained values | No `confirm` while an allergen question is open; allergen list on the website; first real order | T-11.1; Maxi |
| L18 | Register (TSE): AI orders booked properly? | Yes, under D2: the register stays the only booking system with a TSE (E4); every order the agent takes must be booked there by the team. Agent order rows without personal fields are kept as long as the tax adviser asks | Daily report of orders not marked as typed in; procedure documentation (GoBD); tax adviser's word on retention; first real order | T-11.6; Maxi |
| D9 | Data protection officer | Decided by Maxi on 20.10.2026: Maxi himself, against the recommendation of D9 and the Claude round. Recorded risk: the CJEU sees a conflict of interest where the officer decides purposes and means (C-453/21, Art. 38(6)); an appointment with such a conflict can be challenged by the authority | Publish the contact and report it to the authority (Art. 37(7)); first real call | Maxi |

### Proposals, Claude round only (U1 to U8)

Not compared with a second model; Maxi decides each when it is due.

- **U1, wording:** greeting "Guten Tag, Sie sprechen mit dem KI-Assistenten von <Pilotbetrieb>. Wenn Sie lieber mit einem Menschen sprechen möchten, sagen Sie es einfach. Hinweise zum Datenschutz finden Sie unter example.com/datenschutz. Wie kann ich helfen?" Recording request, only while recording is on: "Zur Verbesserung unseres Assistenten würden wir dieses Gespräch gern aufzeichnen und 30 Tage speichern. Sind Sie damit einverstanden? Bitte sagen Sie Ja oder Nein."
- **U2, requests of data subjects:** phone, mail, in person; a request log with the one-month deadline (Art. 12(3)); identity by calling back the stored number or by order details; access exports the rows by number, erasure deletes the customer, nulls `caller_id`, strips orders (the tax rows stay) and deletes recordings, objection switches recognition off. A documented manual procedure is enough until a job exists (T-11.8).
- **U3, selling to other restaurants:** each restaurant is controller; when we host, we are processor with an Art. 28 contract, an Art. 30(2) record and tenant separation (exists); each restaurant needs its own DSFA; the Art. 50 provider duty stays with us; L16 applies once code is shipped.
- **U4, logs:** periods decided with L8 (docs/03).
- **U5, team data:** GUI accounts and audit log on Art. 6(1)(b), (c), (f); audit log 12 months (decided with L8); no speech-to-text and no recording on the team's leg after a handover.
- **U6, reservations and addresses:** periods decided with L8 (docs/03).
- **U7, backups:** client-side encryption, EU storage with a DPA, rolling 30 days (decided with L8); after every restore the erasures of the deletion log are replayed, so nothing erased comes back; the privacy notice says erased data lives on in backups until they expire.
- **U8, text model and speech services:** per vendor before the first key on real data: signed DPA, EU region in the contract, no training, zero or stated retention, sub-processor list, transfer basis (L7), deletion at contract end.

---

## 2. Runbook

### Not-Aus
**GUI → Kopfzeile → KI PAUSIEREN.** Ein Tap. Alle Anrufe gehen sofort ans Team. Das ist der erste Griff bei jeder Störung, vor jeder Diagnose.

### Störungen

| Symptom | Sofort | Dann |
|---|---|---|
| Kunden beschweren sich über den Agenten | KI pausieren | Anruf-Log ansehen, Fall als Eval-Fall anlegen |
| Bestellungen kommen nicht in der Küche an | `handover_state` in der GUI prüfen, „Nochmal senden"; liegt der Eingabezettel am Haupt-Bondrucker, wurde die Bestellung in die Kasse getippt? (D2: Küchenbon druckt nur die Kasse) | Drucker an, Papier, Deckel zu; läuft die Druckbrücke (`printbridge/README.md`, Probebon mit `--test`); Log „Alarm: Küchenbon" im Dienst `dispatcher`; Kassenverbindung |
| Agent versteht auffällig schlecht | KI pausieren | Ist das Menü aktuell? Neue Gerichte ohne Alias? Voice layer or a speech engine failing (docs/20 §5)? |
| Kosten laufen hoch | Kosten-Alarm prüfen | Anrufdauer, Schleifen, Spam-Nummern sperren |
| Agent antwortet nicht | Rufumleitung greift automatisch | `/health`, Container-Logs, Status of `voice` and Asterisk (docs/20 §5, target, not yet measured) |
| Datenbank weg | KI pausieren | letztes Backup prüfen, `scripts/restore.sh` |

### Menüänderung
1. Adminansicht → Menü → ändern
2. Aliase für neue Gerichte ergänzen (mindestens zwei umgangssprachliche)
3. `make eval TAGS=menu` laufen lassen
4. Erst danach ist die Änderung live sicher

### Neue Prompt-Version
1. `prompts/system_vN.md` anlegen, nur **eine** Variable ändern
2. `make eval` vollständig
3. Genauigkeit gleich oder besser → mergen, Ergebnis in die Commit-Nachricht
4. Schlechter → verwerfen, Erkenntnis in `docs/01_STATUS.md`

---

## 3. Tägliche Routine

**Morgens (1 Minute):** Modus stimmt? Wartezeiten passen? Rückrufe von gestern erledigt? Fehlerkarten offen?

**Abends (2 Minuten):** Korrekturen des Tages ansehen. Jede Korrektur hat einen Grund, jeder Grund ist ein Hinweis. Häufungen werden zu Aliasen oder Eval-Fällen.

**Wöchentlich:** Genauigkeit, Eskalationsquote, Kosten je Bestellung, verpasste Anrufe. Trend statt Einzelwert.

**Monatlich:** Kennzahlen gegen die Ziele, neue Fehlerklassen, Anbieterpreise, offene Punkte aus dem Rechts-Check.

---

## 4. Rückwege

Jede Umschaltung braucht einen Rückweg, der ohne Nachdenken funktioniert.

| Schritt vorwärts | Rückweg |
|---|---|
| `shadow` → `overflow` | Modus zurückstellen, ein Tap |
| Freigabeknopf abschalten | in der Konfiguration wieder einschalten |
| `overflow` → `primary` | Modus zurückstellen |
| Neue Prompt-Version | vorherige Version aus `prompts/`, Neustart |
| Migration | `alembic downgrade -1`, vorher Backup |

**Regel:** Kein Schritt vorwärts ohne getesteten Rückweg. Der Rückweg wird einmal wirklich geprobt, nicht nur aufgeschrieben.

---

## 5. Security audit

A deliberate read of the code and configuration that face the outside, done at fixed occasions and not on every change, so it does not eat the usage quota (Maxi, 15.10.2026). The cheap checks run anyway: CodeQL on every pull request except those opened by Dependabot or from a fork (they are scanned on `main` after the merge, docs/13 §6 "CodeQL and dependency updates"), a Codex review on every pull request, Dependabot and the dependency review, `/security-review` in `/done` when a diff touches auth in `core/`, `telephony/`, `deploy/` or the agent token path, and `/start` reports open alerts. The audit covers what they cannot see: how the parts fit together, configuration outside Python (Asterisk, Caddy, Compose), and points that need a decision.

### When an audit is due

| Trigger | Kind | Noticed by |
|---|---|---|
| Before the first call that comes in from outside the workbench (T-10.1 step 2 against the pilot's router, at the latest before G1) | component, on that path | `/task` step 0c, `/start` step 5c |
| Before every gate from G1 on, when exposed files changed since the baseline | scoped if the baseline is younger than 30 days, else full | `/gate` step 1b |
| Before a component becomes reachable from outside the host: the production server with Caddy (T-9.2), the SIP trunk (T-10.12), a new public path behind Caddy, n8n webhooks, a new published port in `deploy/` | component, on that component | `/task` step 0c, `/start` step 5c |
| 30 days or more since the baseline, and exposed files changed since its commit | scoped | `/start` step 5c |

Nothing else triggers an audit, and an audit never starts by itself: `/start` and `/gate` recommend it in one line and Maxi decides. A trigger that is not taken stays due and is named again at the next `/start`. A gate does not pass while an audit finding of severity P1 is open.

A task whose step makes a component reachable from outside carries the note `Security audit before go-live (docs/09 §5)` in its row in `docs/07_WORKPACKAGES.md`; its audit is recorded with the task ID in the Scope column, and holds only while no exposed file changed since its commit, uncommitted edits included, and while none of its P1 findings is open; a later change makes it due again, scoped to the files changed.

`bash scripts/audit_due.sh` does the arithmetic: it holds the exposed areas (all of `api/` except its tests, `prompts/`, the print bridge, `deploy/`, `n8n/`, the migrations, Compose, CI workflows, the backup, migration and database-role scripts; READMEs left out, the Markdown prompts kept), reads the baseline from the log below and prints the files changed since, each by name, and the verdict. `--gate` for `/gate`, `--task T-9.2` for one task with the note, `--tasks` for all open ones, judged against `origin/main` with the task list, the log and the copy of this script on `origin/main`.

Kinds of audit: **full** covers every exposed area and **scoped** every exposed file changed since the baseline; both move the baseline. **component** covers one path or component and does not move it, so a change elsewhere is never dropped from the next scoped audit.

### How it runs

- Model Opus 5.5 at `xhigh`: the audit judges the fate of a call and the outage path (`CLAUDE.md`, "Model Routing"). Read-only subagents may split the scope; every finding is checked in the code by the main session before it is recorded.
- Scoped: the files `bash scripts/audit_due.sh` lists as changed since the baseline, plus what they call. Full and component: the whole checklist on the areas or the component named.
- Findings go to the open hardening issue (#252, or its successor) as one comment: severity, file and line, scenario, proposed fix. Severity: P1 reachable from outside the host, or able to lose or misroute a call, cost money or leak secrets or personal data; P2 needs another weakness first; P3 a note. A P1 becomes its own task before the trigger that called for the audit is passed.
- One row per audit in the log below, with its kind. The baseline is the last row of kind full or scoped. The Commit column holds the commit on `main` the audit read, never a feature-branch commit: a squash merge removes that one, and the script refuses a commit that is not on `main`.

### Checklist

- SIP and Asterisk: no guest or anonymous calls; the context inbound calls land in cannot dial out (toll fraud); transfer targets are fixed, never caller-controlled; ARI, AMI and HTTP bound to loopback or the internal network, credentials from the environment; RTP range no wider than needed; TLS and SRTP on the trunk.
- Untrusted input: the caller ID is never a trust decision; limits per call (duration, turn deadline, concurrency) hold; no crafted input and no failure keeps a call from the team telephone.
- Authentication: the agent token is compared in constant time and never empty; no secret in logs, URLs or error answers; GUI and operations view only behind the proxy's authentication; state-changing requests protected against cross-site requests.
- Exposure: in production only Caddy publishes ports; Postgres, n8n and the voice service stay internal; security headers and request size limits are set.
- Data: the application's database role is not the owner; backups are encrypted; personal data in logs, outbox and call rows is minimal and deleted on schedule.
- Supply chain: requirements installed with hashes; images pinned where intended; CI workflows with least privilege, no untrusted pull request code with secrets.
- Containers: non-root users, no extra capabilities, writable mounts only where needed.

### Audit log

| Date | Kind | Commit | Scope | Result |
|---|---|---|---|---|
| 12.10.2026 | full, by hand | `141d412` | whole repository | hardening list in issue #252 |
| 15.10.2026 | component | `a717ccc` | voice layer (`api/telephony/`, Asterisk), deployment (Compose, Caddy, backups, CI), agent and kitchen token paths, GUI auth, n8n | 1 P1, 6 P2, 6 P3 ([#252, comment of 15.10.2026](https://github.com/Maeximilion/Maex-Voice-Agent/issues/252#issuecomment-6188330460)); the P1 and the SIP points are T-10.13 |
