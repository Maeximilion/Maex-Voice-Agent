# 09 – Betrieb und Recht

---

## 1. Rechts-Check

> Decided 07.10.2026 (D20, Maxi): the checklist is worked through by putting the same question catalogue to two AI models independently (Gemini and Claude) and comparing the answers until every point is settled; no lawyer, no data protection consultant. Limits: this is not legal advice, two models can share the same error, liability stays with the controller. The data protection officer (D9) was left out of D20 then, because that role needs a person or a firm; Maxi decided it on 20.10.2026 (row D9 below), and publishing the contact and reporting it to the authority are still open.
> **The questions are answered (20.10.2026); the duties below are not done.** A duty marked "first real call" blocks every real customer call in any mode; the rest block the step they name. Every open duty blocks the T-7.x tasks and the switch to mode `overflow`.

### Result of the check (20.10.2026)

Both rounds answered the form "G0 input: legal check" (basis `main` at 13b148e) independently: Gemini first, then Claude in a session that did not see the Gemini answers. They agree on 14 of the 18 questions. Maxi decided the four open points (L8, L13, L15, D9) on 20.10.2026. D9 and the topics U1 to U8 were answered by the Claude round only and are recorded as proposals. Both round files stay outside the repository. Not legal advice; the limits of D20 apply.

| No. | Question | Settled answer | Duty, and before which step | Tracked in |
|---|---|---|---|---|
| L1 | AI Act Art. 50: AI notice in the first sentence enough? | Yes, when the code speaks it first, it cannot be talked over and says "KI"; a question like "Sind Sie ein Mensch?" always gets a truthful answer. Art. 50(1) applies since 02.08.2026, not moved by the AI omnibus (VO (EU) 2026/1744) | Eval for "Sind Sie ein Mensch?"; first real call. Built 26.10.2026: the core answers the question itself, before the model, and offers the team (evals `eskalation_0017`, `eskalation_0018`; docs/05 §3) | T-11.5 |
| L2 | Recording: what to announce, is a way to object enough? | No: opt-out is not enough. The caller says an explicit "ja" before any recording starts; on "nein" or silence the call goes on without recording, a human is an extra, never the price of saying no. Announce: that it is recorded, the purpose, 30 days, voluntary, how to stop ("Aufnahme stoppen"), where the privacy notice is. In mode `shadow` the AI takes no call (docs/02 §4), so the voice layer asks the question before the call rings the team, and its first sentence still names the AI (L1); on "nein" or silence the team phone rings without recording | Consent question, consent log per call, scheduled expiry of recordings, stop on request; before mode `shadow` with recording | T-11.3 |
| L3 | Legal basis per purpose | Order, reservation, live speech processing: Art. 6(1)(b). Caller recognition, call statistics, call log, cost logs: Art. 6(1)(f). Tax records: Art. 6(1)(c). Recording, transcripts and their analysis: Art. 6(1)(a). Own allergy: Art. 9(2)(a) (L13). § 26(1) BDSG is not relied on (CJEU C-34/21) | Into the record of processing (L9) and the privacy notice (L4); first real call | Maxi |
| L4 | Information duty: short announcement plus privacy notice | Two layers. In the call: controller, AI notice, where the full notice is (`example.com/datenschutz`). On the website: everything of Art. 13(1) and (2): controller and DPO contact, purposes with legal bases, processors by category, transfers, periods (docs/03), rights, complaint to the authority, the call log | Privacy notice online, greeting extended by the pointer; first real call. The pointer is built (26.10.2026, T-11.5): the greeting names `service_config.privacy_notice_address` and leaves the sentence out while it is empty, so the address goes in once the notice is online (docs/03, `PRIVACY_NOTICE_ADDRESS` for a new tenant) | Maxi; T-11.5 |
| L5 | Data processing agreements | An Art. 28 contract with every processor before it sees real data: EU server, backup storage, speech-to-text, text-to-speech, text model, push service if external. Each forbids training on our data. The SIP trunk carrier carrying the call is its own controller under telecom law, a processor only if it records or stores for us. None needed in the test phase | Per service, before its first key is used on real data | Maxi |
| L6 | D21: is streamed live audio without storage a recording (E11)? | No, when the service keeps audio and transcript only for the request, stores nothing afterwards and does not train on it. It is processing under Art. 6(1)(b), with a DPA and a line in the privacy notice | Choose services with zero retention, written into the DPA; provider-side audio logging off; before the first real call on a speech service | Maxi (D15, D18) |
| L7 | Third-country transfer | Yes, check whenever a processor or sub-processor processes or can reach data outside the EU, also by support access of a non-EU parent; an EU region alone does not settle it. Basis: DPF certification or SCCs with a transfer impact assessment | Per vendor, before its first real call (D15 before G1) | Maxi |
| L8 | Deletion concept and periods | Periods decided by Maxi (90-day set), see docs/03 "Löschkonzept". The concept is implemented only once the deletion job exists and is tested | Deletion job with tests; first real call | T-11.2, T-11.7, T-6.7, T-9.3 |
| L9 | Record of processing activities | Mandatory: the Art. 30(5) exemption does not apply (not occasional, includes Art. 9 data) | Written before the first real call, kept outside the repository like the DSFA draft | Maxi |
| L10 | Data protection impact assessment | Mandatory (DSK must-list no. 11, AI that talks with customers) and to be finished with the DPO's advice before the first real call in any mode, not only before recording | Finish draft v0.1 with this result; first real call | Maxi (D9) |
| L11 | Team: informed, consent to recordings | Every team member gets a written information sheet. The recordings of mode `shadow` are the team's own calls, so every team member who answers the phone gives a written, voluntary, revocable consent first, with the written promise that refusing has no consequence and that recordings and the audit log never rate staff (the Gemini line; the Claude round's "stop at the handover, no team consent" holds only for a call the AI takes). A team member without consent answers no recorded call | Information sheet before the first real call; the team's consents before mode `shadow` with recording; recording of an AI call stops at the handover (T-11.3) | Maxi; T-11.3 |
| L12 | E12: balancing for recognition by caller_id | Recognition passes the balancing under Art. 6(1)(f) on conditions: the agent never says stored data aloud unprompted and only asks ("An dieselbe Adresse wie beim letzten Mal?"), a name that does not match means a new customer record, an objection (Art. 21) switches recognition off for that number | Balancing as one page in the DSFA; rules built with `find_customer` | Maxi; T-6.2 |
| L13 | E14: own allergy as kitchen note | Health data (Art. 9); needs explicit consent. Decided by Maxi: the agent asks "Darf ich die Allergie als Hinweis für die Küche notieren?"; on "ja" the note keeps the D24 wording "WICHTIG: Keine <Zutat>. Grund: Allergie", on "nein" only "ohne <Zutat>" without a reason. The caller's "ja" counts for themselves and for their own child; for another adult it is no consent of that person, so only "ohne <Zutat>" is stored. The same holds for a reservation note. The note text is deleted at the end of the business day of the order (the night after its `ready_at`; no field records the handover to the guest), and for a reservation the night after its date | Consent question, consent log, deletion of the note text; before an order that can carry an allergy reaches a real caller (G1). **Built 25.10.2026 (T-11.1, first PR):** question and log (`consent_log`, migration 010), the two forms of the note for order lines, the whole order and a table, another adult's allergy without the reason; the deletion of the note text is T-11.2 | T-11.1, T-11.2 |
| L14 | E11: may transcribed text go into the repository? | No. Raw transcripts stay personal data, and a git history cannot be erased; free text can identify a caller through details no checklist lists. So no real caller wording goes into the repository, also after names and numbers are removed: only synthetic sentences written after the pattern of a real call (the Gemini line, and CLAUDE.md §8 and DSFA M15 as today; the Claude round would have allowed anonymised sentences) | Eval cases from transcripts are written as synthetic sentences | T-7.x |
| L15 | E10: call log, is 90 days right? | Yes, 90 days (D10), and E10 stays. Decided by Maxi: the log records only the hour of a call, not the minute, so a line can hardly be matched to the router's call list | Hour window in the log; the privacy notice names the log once it is online | T-11.4; Maxi |
| L16 | Piper licence (GPL-3.0-or-later) | No duty while the software runs only as our own service; GPL duties start when a copy is handed to anyone else. The service `voice` imports Piper in-process (`api/telephony/adapters/tts_piper.py`), so a shipped `voice` would be one combined work | Before any copy leaves our control (E16): Maxi picks Piper behind a process boundary, `voice` under the GPL, or Piper not shipped | Maxi, at E16 |
| L17 | Allergens (LMIV): only maintained values, otherwise callback? | Yes for the wording, but a phone order is distance selling: allergen information must be available before the contract, so the handover to the team or the callback comes before `confirm` when the guest asked beyond the maintained values | No `confirm` while an allergen question is open; allergen list on the website; first real order. **Built 25.10.2026 (T-11.1, second PR):** an unmaintained value after `get_item_details`, and a question about traces, contact or a promise of safety, hand the call to the team or a callback at once, and the call ends before any `confirm`; the allergen list on the website stays Maxi's | T-11.1; Maxi |
| L18 | Register (TSE): AI orders booked properly? | Yes, under D2: the register stays the only booking system with a TSE (E4); every order the agent takes must be booked there by the team. Agent order rows without personal fields are kept as long as the tax adviser asks | Daily report of orders not marked as typed in (built 28.10.2026, T-11.6: the mark „In Kasse eingegeben“ on the tablet, a red card from the next business day, one push a day); procedure documentation (GoBD); tax adviser's word on retention; first real order | T-11.6; Maxi |
| D9 | Data protection officer | Decided by Maxi on 20.10.2026: Maxi himself, against the recommendation of D9 and the Claude round. Recorded risk: the CJEU sees a conflict of interest where the officer decides purposes and means (C-453/21, Art. 38(6)); an appointment with such a conflict can be challenged by the authority | Publish the contact and report it to the authority (Art. 37(7)); first real call | Maxi |

### Proposals, Claude round only (U1 to U8)

Not compared with a second model; Maxi decides each when it is due.

- **U1, wording:** greeting "Guten Tag, Sie sprechen mit dem KI-Assistenten von <Pilotbetrieb>. Wenn Sie lieber mit einem Menschen sprechen möchten, sagen Sie es einfach. Hinweise zum Datenschutz finden Sie unter example.com/datenschutz. Wie kann ich helfen?" Recording request, only while recording is on: "Zur Verbesserung unseres Assistenten würden wir dieses Gespräch gern aufzeichnen und 30 Tage speichern. Sind Sie damit einverstanden? Bitte sagen Sie Ja oder Nein."
- **U2, requests of data subjects:** phone, mail, in person; a request log with the one-month deadline (Art. 12(3)); identity by details only that person knows (name with an order or reservation and its date, or the delivery address), never by the phone number alone, since several customers can share one; access and erasure cover only the verified person's rows, not every row with that number; erasure covers every store: the customer, `caller_id`, orders (the tax rows stay), reservations, callbacks, recordings, transcripts and outbox payloads, and writes an entry to the erasure ledger (U7); objection switches recognition off. A documented manual procedure is enough until a job exists (T-11.8; the procedure is in section 2, "Requests of data subjects"). The request log holds the verification details, so it has a place, a period and a cleanup step: table `data_subject_requests` (a file outside the repository until T-11.8 builds it), verification details stripped 3 months after the answer, the thin row deleted at the end of the third calendar year after the year of the answer, cleanup by the daily job of T-11.2 once the table exists (`docs/03_DATA_MODEL.md` Löschkonzept). Until T-11.8 builds it, the interim file has no job: the person who keeps it strips the verification details and deletes expired lines by hand in the monthly routine (section 3), and T-11.8 moves the remaining lines into the table with their dates. The period is an assumption of 21.10.2026 awaiting Maxi (`docs/01_STATUS.md` Made Assumptions).
- **U3, selling to other restaurants:** each restaurant is controller; when we host, we are processor with an Art. 28 contract, an Art. 30(2) record and tenant separation (exists); each restaurant needs its own DSFA; the Art. 50 provider duty stays with us; L16 applies once code is shipped.
- **U4, logs:** periods decided with L8 (docs/03).
- **U5, team data:** GUI accounts and audit log on Art. 6(1)(b), (c), (f); audit log 12 months (decided with L8); no speech-to-text and no recording on the team's leg after the AI hands a call over (the recorded team calls of mode `shadow` follow L11).
- **U6, reservations and addresses:** periods decided with L8 (docs/03).
- **U7, backups:** client-side encryption, EU storage with a DPA, rolling 30 days (decided with L8); an erasure ledger outside the database and its backups (row ids and date, no personal data) is replayed after every restore, so nothing erased comes back (T-9.3); the privacy notice says erased data lives on in backups until they expire.
- **U8, text model and speech services:** per vendor before the first key on real data: signed DPA, EU region in the contract, no training, zero or stated retention, sub-processor list, transfer basis (L7), deletion at contract end.

---

## 2. Runbook

### Not-Aus
**GUI → Kopfzeile → KI PAUSIEREN.** Ein Tap. Alle Anrufe gehen sofort ans Team. Das ist der erste Griff bei jeder Störung, vor jeder Diagnose. Wieder einschalten fragt einmal nach ("KI einschalten"). The one page for the team, to read through once and hang next to the tablet: `docs/vorlagen/team_einweisung.md` (T-8.5).

### Modus (T-8.1)
The header's three buttons switch between `shadow` ("KI hört mit"), `overflow` ("KI springt ein") and `primary` ("KI nimmt an"); a step towards more AI asks once, a step back is one tap. Until the SIP trunk (T-10.12) `overflow` and `primary` differ only in the approval of orders: who rings first is the router's job (docs/02 §4), so the way to take calls back is `shadow` or the emergency stop. Weekly windows (`call_mode_windows`, written by SQL until an admin page exists) hold a mode of their own and the header says "nach Plan bis …"; a tap holds against a window that began before it, until the next window begins (docs/02 §4). The emergency stop beats every window and is never a plan.

### Alarme für Maxi (T-8.3, Thema `ops` am Push-Server)
Cost of the day above `ALARM_COST_PER_DAY_CENTS`; `ALARM_ERROR_CALLS_PER_HOUR` or more calls ending in an error within an hour; events to n8n that failed for good (n8n down); the day report from `MONITOR_REPORT_HOUR` on the previous day. They reach Maxi's device without n8n and never the team. Setup: `n8n/README.md`; without `MONITOR_PUSH_TOKEN` they are only logged. Also `voice` not answering, Asterisk not registered (with `VOICE_URL` set) and no good backup for `ALARM_BACKUP_HOURS`. Also the print bridge silent while pickup or delivery is open, which the tablet shows as well.

### Störungen

| Symptom | Sofort | Dann |
|---|---|---|
| Kunden beschweren sich über den Agenten | KI pausieren | Anruf-Log ansehen, Fall als Eval-Fall anlegen |
| Bestellungen kommen nicht in der Küche an | `handover_state` in der GUI prüfen, „Nochmal senden"; liegt der Eingabezettel am Haupt-Bondrucker, wurde die Bestellung in die Kasse getippt? (D2: Küchenbon druckt nur die Kasse) | Drucker an, Papier, Deckel zu; läuft die Druckbrücke (`printbridge/README.md`, Probebon mit `--test`); Log „Alarm: Küchenbon" im Dienst `dispatcher`; Kassenverbindung |
| Agent versteht auffällig schlecht | KI pausieren | Ist das Menü aktuell? Neue Gerichte ohne Alias? Voice layer or a speech engine failing (docs/20 §5)? |
| Kosten laufen hoch | Push "Cost above the daily limit" on `ops`, or the day report; if unsure, KI pausieren | Anrufdauer, Schleifen, Spam-Nummern sperren; calls named "of unknown cost" have no price set in `.env` |
| Push "Events not delivered" | check n8n (`docker compose ps`, its logs); the team's pushes and the outbox events wait as `failed` | restart n8n; failed events are not sent again by themselves, read their `last_error` in `outbox` |
| Push "Voice layer not answering" or "Asterisk not registered" | calls ring the team by the dialplan or the router; nothing to do at once | `docker compose --profile voice ps`, logs of `voice` and `asterisk`, the access at the router (docs/20 §5) |
| Push "Print bridge silent", banner „Druckbrücke meldet sich nicht" on the tablet | no order prints: team takes orders from the tablet, nothing is lost, the slips wait in the outbox | register PC on? bridge running (`printbridge/README.md`)? internet in the restaurant? |
| Push "Backup overdue" | `backups/backup.log` | run `bash scripts/backup.sh` by hand, check the cron line (docs/13 §4) |
| Push "Calls with errors" | KI pausieren if guests are affected | Anruf-Log, `error` calls, the log of `voice` |
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

### Requests of data subjects (U2, manual until T-11.8 builds the table and the job)

A guest asks what we hold about them (access, Art. 15), to have it erased (Art. 17), or objects (Art. 21), by phone, mail or in person. The restaurant, the controller, answers within **one month of receipt** (Art. 12(3)); for a request on the phone line the day of the call is the day of receipt. A complex request may take two more months, and the guest is told so with the reason inside the first month. The agent has no handling of its own for such a request yet: it goes to the team like any other wish it cannot serve, so the team reads the `out_of_scope` callback cards for one.

1. **Log it the day it arrives.** One line in the request log, a file outside the repository and outside the database until T-11.8 builds `data_subject_requests` (U2; place, period and cleanup in `docs/03_DATA_MODEL.md`, Löschkonzept): request id in the form `dsr_2026_001` (lower-case letters, digits and underscores, so it also works as a backup label), received date, channel, kind (access, erasure, objection), the deadline, the verification details as the guest gave them, every phone number the guest says they used, then the outcome and the answered date.
2. **Verify the person, never by the number alone.** Several guests can share one number. Ask for details only that person knows: the name with an order or a reservation and its date. A delivery address cannot be checked against anything yet (`addresses` stays empty until T-6.5). Asking for these details does not stop the month (an open point for Maxi); a request that cannot be verified, or that is refused, gets a written answer inside the month with the reason and the right to complain to the supervisory authority.
3. **Read the rows on the day of receipt,** before the daily deletion job (T-11.2) strips them, and copy them into the access answer. Read on the server from the stack's folder with `docker compose exec db psql -U <POSTGRES_USER of .env> -d <POSTGRES_DB of .env>`, the owner role of `docs/13_DEPLOYMENT.md` section 3 (`OWNER_DATABASE_URL` cannot be given to `psql`: its `+psycopg` scheme is refused and its host `db` resolves only inside the Compose network). Every query names the restaurant's `tenant_id`, and soft-deleted or cancelled rows count too:
   - `orders` (`customer_name`, `phone`, `note`, `created_at`, and the `note` of their `order_items`) and `reservations` (`guest_name`, `phone`, `note`, `reserved_for`), by `phone` in E.164 (`+4972215551234`) and the name;
   - `customers` by `phone` in E.164 and the name, and their `addresses` by `customer_id`; `recognition_objections` by `phone` in E.164. All three stay empty until T-6.2 and T-6.5 write them, and an objection may be someone else's on a shared number;
   - `callbacks` by `phone` in E.164 and `calls` by `caller_id`, which is stored as the line gives it, not always in E.164: match its digits on the national number (`regexp_replace(caller_id, '\D', '', 'g') like '%7221555123%'`). A call or a callback that carries only the number and no name cannot be verified by details: the team asks the guest whether it is theirs (date, time, what it was about) and takes only those they confirm;
   - through the ids found: `consent_log` by `call_id` (no number in it), and `outbox` by `payload->>'order_id'`, `'reservation_id'`, `'callback_id'` or `'call_id'` (the table has no `call_id` column; most payloads are thinned to these ids after their period, `api/jobs/retention.py`);
   - `audit_log` holds ids, reasons and labels of the team's actions, no name, number or note: it is named in the access answer as the team's actions and left to its 12-month period;
   - outside the database: paper input slips still at the register and what the team typed into the register (D2); the call log CSV of `docs/17_ANRUFPROTOKOLL.md`, searched only when the guest names the date and hour of a call, since its verbatim phrases may hold a name; n8n executions (pruned after 168 hours, `EXECUTIONS_DATA_MAX_AGE` in `docker-compose.yml`); the router's call list and the team's phones; container logs (14 days where `deploy/journald.conf` is installed; Asterisk writes no number, docs/20 section 4). Recordings and transcripts do not exist yet; once they do (T-7.x, L11), they are found by the `call_id` too.
4. **Access:** the guest gets a copy of their rows, nothing of another guest, and the items of Art. 15(1) as the privacy notice states them (L4): purposes and legal basis (L3), recipients and processors (L5), transfers (L7), the periods of `docs/03_DATA_MODEL.md`, the source (their own call), the rights to rectification, erasure, restriction and objection, the right to complain, and the contact of the data protection officer (D9). Send it through the channel the guest names.
5. **Erasure:** first a backup, `bash scripts/backup.sh --label dsr_2026_001` (`docs/13_DEPLOYMENT.md` section 4). Open items are closed with the guest first: a future reservation is cancelled or kept by their choice, an open callback is done or dropped. Then, in one `begin; ... commit;` in the same `psql` session, with each statement's row count checked before the commit, for the verified rows only:
   - the verified `customers` row is deleted, which takes its `addresses` and clears the links on its orders;
   - finished `reservations` (date passed or cancelled) are deleted; `callbacks` get `phone` and `summary` set to `''`; `calls.caller_id` is set to null;
   - `orders` stay for the tax records (L18, Art. 17(3)(b)), stripped as the deletion job strips them: `customer_name` and `phone` to `''`, `note`, `customer_id` and `address_id` to null, `note` of their `order_items` to null;
   - `outbox` rows of those ids that are `sent` or `failed` are thinned to their id keys as the deletion job does, or deleted with their reservation; a `pending` row is left alone until the dispatcher has sent it or marked it failed (its payload is the team's notification, `n8n/team_events.json`), and is handled then;
   - `consent_log` rows stay (`docs/03_DATA_MODEL.md`: the proof that the allergy question was asked, L13); they hold no number and name nobody once `caller_id` is cleared. Whether they stay after an erasure is open for Maxi;
   - paper slips are destroyed; the register keeps its tax records (L18).
   Each erased or cleared row goes into the erasure ledger, a file outside the database and its backups (U7): date, table, row id, action, no name and no number. Until T-9.3 replays the ledger by itself, whoever restores a backup does so with the AI paused and `api`, `dispatcher` and `voice` stopped, and erases the ledger's rows again by these steps before the agent takes calls, so nothing erased comes back. Before the answer, `psql -l` shows no `_before_restore_` copy still holding the rows (`docs/13_DEPLOYMENT.md` section 4), and the `dsr_` backup is deleted by hand once the erasure is checked.
6. **Objection:** recognition by number (T-6.2, L12) is not built yet, so there is nothing to switch off today. The number goes into `recognition_objections` in E.164, `insert ... on conflict do nothing`, with the restaurant's `tenant_id`; the row outlives the request log and the customer, and T-6.2 reads it. A number still on the interim list outside the repository moves there once, and the list goes. An objection withdrawn in writing removes the row. The linked calls get `caller_id` set to null. What else an objection covers is open for Maxi.
7. **Answer and close.** Answer in writing before the deadline and say what stays and why: the order rows for the tax records without name, number or notes; the `consent_log` row; the thin line of the request log; backups until they expire (rolling 30 days, U7); n8n executions until they expire. Set the answered date and the outcome. When the monthly routine (section 3) strips the line's verification details, the mail thread and any file of the access copy go too.

T-6.5, T-6.7 and the rest of T-11.8 update this section in the same pull request that adds a store (customers, addresses, the request table and its job). Open for Maxi (`docs/01_STATUS.md`, Made Assumptions): the period of the request log (assumption of 21.10.2026), whether asking for verification details stops the month, whether `consent_log` rows stay after an erasure, and what an objection covers beyond recognition.

---

## 3. Tägliche Routine

**Morgens (1 Minute):** Modus stimmt? Wartezeiten passen? Rückrufe von gestern erledigt? Fehlerkarten offen?

**Abends (2 Minuten):** Korrekturen des Tages ansehen. Jede Korrektur hat einen Grund, jeder Grund ist ein Hinweis. Häufungen werden zu Aliasen oder Eval-Fällen.

**Wöchentlich:** Genauigkeit, Eskalationsquote, Kosten je Bestellung, verpasste Anrufe. Trend statt Einzelwert.

**Monatlich:** Kennzahlen gegen die Ziele, neue Fehlerklassen, Anbieterpreise, offene Punkte aus dem Rechts-Check. Request log of data subjects (U2): clean the interim file (strip verification details, delete expired lines).

---

## 4. Rückwege

Jede Umschaltung braucht einen Rückweg, der ohne Nachdenken funktioniert.

| Schritt vorwärts | Rückweg |
|---|---|
| `shadow` → `overflow` | Modus zurückstellen, ein Tap |
| Approval step of `overflow` off (after G5, T-8.2) | `UPDATE service_config SET overflow_needs_approval = true`; orders confirmed meanwhile have already gone to the kitchen, new ones wait for "Passt" again. The amount limit holds in every mode either way |
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
| 27.10.2026 | component | `b7c8c36` | T-10.1 step 2: the call path at the pilot's router (Asterisk configuration and `render.sh`, the `asterisk` and `voice` services, the voice service's token, `/ending` and `/missed`) | no P1, no P2; one note on address translation for the step itself ([#252, comment of 27.10.2026](https://github.com/Maeximilion/Maex-Voice-Agent/issues/252#issuecomment-6452068463)) |
| 29.10.2026 | component | `a7df8f4` | T-10.1 step 2, scoped to the 69 exposed files changed since `b7c8c36`: the router's tunnel (`deploy/asterisk/wireguard/`, `docker-compose.line.yml`), `ACCESS_ALSO` in `render.sh`, `api/telephony/` with the call limit, migrations 011 and 012, `n8n/team_events.json`, backup and restore, the two new CI workflows; kind component, so the 81 files changed between `141d412` and `b7c8c36` stay with the next scoped audit | no P1, no P2; five notes, three of them for T-10.2 ([#252, comment of 29.10.2026](https://github.com/Maeximilion/Maex-Voice-Agent/issues/252#issuecomment-6482996393)) |
| 29.10.2026 | component | `9d5e281` | T-10.1 step 2 and T-10.2, the six exposed files changed since `a7df8f4`: `api/config.py` (#429), the tunnel overlay `deploy/asterisk/docker-compose.tunnel.yml`, `docker-compose.line.yml`, `render.sh`, the arrival line in `extensions.conf`, and `api/telephony/speech_bench.py` | no P1, no P2; two new notes: the RTP range reachable from the router's networks, Asterisk left without a network when `wireguard` restarts ([#252, second comment of 29.10.2026](https://github.com/Maeximilion/Maex-Voice-Agent/issues/252#issuecomment-6499672800)) |
| 30.10.2026 | component | `e43208b` | T-10.1 step 2 and T-10.2, the 33 exposed files changed since `9d5e281`: the mode in `call_routing`, the agent core of #437, the routes `/asterisk/alive` and `/asterisk/seen`, the Asterisk healthcheck, the operator alarms and the `ops` topic, the bridge heartbeat, holiday suggestions and the tablet routes, migrations 013 to 017, `scripts/backup.sh`; nothing under `deploy/` changed | no P1, no P2; three notes ([#252, comment of 30.10.2026](https://github.com/Maeximilion/Maex-Voice-Agent/issues/252#issuecomment-6520130125)) |
| 30.10.2026 | component | `7768182` | T-10.1 step 2 and T-10.2, the five exposed files changed since `e43208b`, all from PR #445: `api/agent/allergies.py`, `consent.py`, `escalation.py`, `guards.py`, `loop.py`, plus what they call; a read-only pass with one skeptic per finding, no pytest and no database | no P1, six P2 (allergen guards and readings, none on the fate of a call), three P3 ([#252, second comment of 30.10.2026](https://github.com/Maeximilion/Maex-Voice-Agent/issues/252#issuecomment-6533664709)) |
| 31.10.2026 | component | `52bf88c` | T-10.1 step 2 and T-10.2, the six exposed files changed since `7768182`, all from PR #447: `api/agent/allergies.py`, `consent.py`, `escalation.py`, `guards.py`, `loop.py`, `api/domain/menu/wishes.py`, plus what they call; a read-only pass with one skeptic per finding, no pytest and no database | no P1, nine P2 (the allergen path, most of them regressions of #447, none on the fate of a call), three P3 ([#252, comment of 31.10.2026](https://github.com/Maeximilion/Maex-Voice-Agent/issues/252#issuecomment-6535938572)) |

**Waived, not audited (Maxi, 31.10.2026):** the audit of the #449 diff (`52bf88c` to `34c3a2e`, the six allergen files of the row above) for the wire run of T-10.2 only. That run uses invented calls, and the diff changes how allergy sentences are read, nothing in Asterisk, the tunnel or the outage path. A waiver is no audit, so it has no row and `audit_due.sh` keeps naming T-10.1 as due; the audit runs before the first real guest call.
