# 09 – Betrieb und Recht

---

## 1. Rechts-Check

> Decided 07.10.2026 (D20, Maxi): the checklist is worked through by putting the same question catalogue to two AI models independently (Gemini and Claude) and comparing the answers until every point is settled; no lawyer, no data protection consultant. Limits: this is not legal advice, two models can share the same error, liability stays with the controller. The question of the data protection officer (D9) stays open, because that role needs a person or a firm.
> **Jeder offene Punkt blockiert die Aufgaben T-7.x und den Wechsel in den Modus `overflow`.**

- [ ] **AI Act Art. 50** — KI-Hinweis im ersten Satz. Gilt seit 02.08.2026.
- [ ] **Aufzeichnung** — Einwilligung von Kunde und Team (§201 StGB). Ansage plus ein Weg zu widersprechen, der zum Menschen führt.
- [ ] **DSGVO** — Rechtsgrundlage je Zweck (Bestellung · Aufnahme · Auswertung)
- [ ] **DSGVO** — Informationspflicht: kurze Ansage plus Datenschutzerklärung auf example.com
- [ ] **DSGVO** — data processing agreements (AVV) with today's processors: speech-to-text service, text-to-speech service, model provider, hosting, backup storage, later the SIP trunk provider. None in the test phase, where everything runs on the workbench
- [ ] **DSGVO** — D21 (open question): does live audio streamed to a speech service without storage fall under the rules for recordings (E11)? In the test phase no audio leaves the workbench
- [ ] **DSGVO** — Drittlandtransfer prüfen, falls ein Anbieter außerhalb der EU verarbeitet
- [ ] **DSGVO** — Löschkonzept umgesetzt und getestet (`docs/03_DATA_MODEL.md`)
- [ ] **DSGVO** — Verzeichnis der Verarbeitungstätigkeiten ergänzt
- [ ] **DSGVO** — Datenschutz-Folgenabschätzung prüfen. Entwurf v0.1 vom 24.09.2026 liegt vor (außerhalb des Repos, enthält echte Firmendaten), Prüfung durch den Datenschutzbeauftragten offen
- [ ] **DSGVO** — Datenschutzbeauftragter benannt. Laut DSFA-Entwurf wahrscheinlich Pflicht, weil die Verarbeitung DSFA-pflichtig ist (§ 38 Abs. 1 S. 2 BDSG)
- [ ] **Team** — informiert, Einwilligung oder Vereinbarung zu Aufnahmen liegt vor
- [ ] **LMIV** — Allergen-Auskunft nur aus gepflegten Werten, sonst Rückruf
- [ ] **TSE** — KI-Bestellungen werden ordnungsgemäß in der Kasse gebucht

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

A deliberate read of the code and configuration that face the outside, done at fixed occasions and not on every change, so it does not eat the usage quota (Maxi, 15.10.2026). The cheap checks run anyway: CodeQL and a Codex review on every pull request, Dependabot and the dependency review, `/security-review` in `/done` when a diff touches auth in `core/`, `telephony/`, `deploy/` or the agent token path, and `/start` reports open alerts. The audit covers what they cannot see: how the parts fit together, configuration outside Python (Asterisk, Caddy, Compose), and points that need a decision.

### When an audit is due

| Trigger | Kind | Noticed by |
|---|---|---|
| Before the first call that comes in from outside the workbench (T-10.1 step 2 against the pilot's router, at the latest before G1) | full | `/start` step 5c |
| Before every gate from G1 on | full; scoped (the diff since the last audit) if the last full audit is younger than 30 days | `/gate` step 1b |
| Before a component becomes reachable from outside the host: the SIP trunk (T-10.12), a new public path behind Caddy, n8n webhooks, a new published port in `deploy/` | full, on that component | `/start` step 5c, when such a task is ready |
| 30 days or more since the last audit, and files of the exposed areas (below) changed since its commit | scoped | `/start` step 5c |

Nothing else triggers an audit, and an audit never starts by itself: `/start` and `/gate` recommend it in one line and Maxi decides. A trigger that is not taken stays due and is named again at the next `/start`. A gate does not pass while an audit finding of severity P1 is open.

Exposed areas, as a git pathspec (`/start` uses it as it stands):

```text
api/core/ api/telephony/ api/tools/ api/gui/ api/events/ api/main.py api/config.py api/Dockerfile api/requirements.in api/requirements.txt deploy/ docker-compose.yml docker-compose.override.yml .github/workflows/ scripts/backup.sh scripts/restore.sh :!*.md :!api/tests/
```

### How it runs

- Model Opus 5.5 at `xhigh`: the audit judges the fate of a call and the outage path (`CLAUDE.md`, "Model Routing"). Read-only subagents may split the scope; every finding is checked in the code by the main session before it is recorded.
- Scoped: only the files of `git diff --name-only <commit of the last row>..origin/main -- <exposed areas>`, plus what they call. Full: the whole checklist on the named components.
- Findings go to the open hardening issue (#252, or its successor) as one comment: severity, file and line, scenario, proposed fix. Severity: P1 reachable from outside the host, or able to lose or misroute a call, cost money or leak secrets or personal data; P2 needs another weakness first; P3 a note. A P1 becomes its own task before the trigger that called for the audit is passed.
- One row per audit in the log below. `/start` reads its last row.

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
| 15.10.2026 | full | `a717ccc` | voice layer (`api/telephony/`, Asterisk), deployment (Compose, Caddy, backups, CI), agent and kitchen token paths, GUI auth, n8n | 1 P1, 6 P2, 6 P3 ([#252, comment of 15.10.2026](https://github.com/Maeximilion/Maex-Voice-Agent/issues/252#issuecomment-6188330460)); the P1 and the SIP points are T-10.13 |
