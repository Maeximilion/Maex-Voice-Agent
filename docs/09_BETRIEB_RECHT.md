# 09 – Betrieb und Recht

---

## 1. Rechts-Check

> ⚠️ Keine Rechtsberatung. Vor dem ersten echten Anruf durch Anwalt oder Datenschutzberater prüfen lassen.
> **Jeder offene Punkt blockiert die Aufgaben T-7.x und den Wechsel in den Modus `overflow`.**

- [ ] **AI Act Art. 50** — KI-Hinweis im ersten Satz. Gilt seit 02.08.2026.
- [ ] **Aufzeichnung** — Einwilligung von Kunde und Team (§201 StGB). Ansage plus ein Weg zu widersprechen, der zum Menschen führt.
- [ ] **DSGVO** — Rechtsgrundlage je Zweck (Bestellung · Aufnahme · Auswertung)
- [ ] **DSGVO** — Informationspflicht: kurze Ansage plus Datenschutzerklärung auf yokiyoki.de
- [ ] **DSGVO** — AVV mit Voice-Plattform, Hosting und jedem weiteren Dienstleister
- [ ] **DSGVO** — Drittlandtransfer prüfen, falls ein Anbieter außerhalb der EU verarbeitet
- [ ] **DSGVO** — Löschkonzept umgesetzt und getestet (`docs/03_DATENMODELL.md`)
- [ ] **DSGVO** — Verzeichnis der Verarbeitungstätigkeiten ergänzt
- [ ] **DSGVO** — Datenschutz-Folgenabschätzung prüfen
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
| Bestellungen kommen nicht in der Küche an | `handover_state` in der GUI prüfen, „Nochmal senden" | n8n-Logs, Drucker- oder Kassenverbindung |
| Agent versteht auffällig schlecht | KI pausieren | Ist das Menü aktuell? Neue Gerichte ohne Alias? Plattform-Störung? |
| Kosten laufen hoch | Kosten-Alarm prüfen | Anrufdauer, Schleifen, Spam-Nummern sperren |
| Agent antwortet nicht | Rufumleitung greift automatisch | `/health`, Container-Logs, Plattform-Status |
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
