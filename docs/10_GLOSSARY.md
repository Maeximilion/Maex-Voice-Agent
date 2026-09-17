# 10 – Glossar

| Begriff | Bedeutung |
|---|---|
| **Agent** | die KI, die den Anruf führt |
| **Agent-API** | unsere FastAPI-Anwendung, die die Tools bereitstellt |
| **Alias** | umgangssprachlicher Name für ein Gericht („die knusprigen" → Nr. 47) |
| **AVV** | Auftragsverarbeitungsvertrag nach DSGVO |
| **Barge-in / Unterbrechen** | der Kunde kann in die Ansage hineinsprechen |
| **DTMF / Tastentöne** | Eingabe über die Telefontastatur, robust bei schlechter Leitung |
| **Durchstich** | kleinste Version, die einmal durch alle Schichten läuft |
| **Eval** | Testfall mit erwartetem Ergebnis, misst die Genauigkeit automatisch |
| **Gate** | Prüfpunkt mit messbaren Kriterien, ohne Bestehen keine nächste Stufe |
| **Heißer Pfad** | Arbeit, während der Kunde wartet, hartes Zeitbudget |
| **Kalter Pfad** | Arbeit nach dem Gespräch, darf Sekunden dauern |
| **Idempotenz** | doppelt gesendet, einmal ausgeführt, verhindert Doppelbestellungen |
| **LMIV** | EU-Lebensmittelinformationsverordnung, regelt die Allergenauskunft |
| **Menü-Index** | Nummer und Name aller Gerichte, das Einzige, was im Prompt steht |
| **Overflow / Überlauf** | die KI nimmt nur an, wenn das Team nicht abnimmt |
| **PCF** | Project Context File, der Gesamtplan (`docs/00_PCF.md`) |
| **PoC** | technischer Machbarkeitstest |
| **Readback** | der Satz, den der Agent vor der Bestätigung vorliest |
| **Regressionstest** | Evals nach jeder Änderung, damit nichts unbemerkt schlechter wird |
| **Schattenmodus** | Team telefoniert, KI wertet die Anrufe nachträglich offline aus |
| **TSE** | Technische Sicherheitseinrichtung der Kasse, gesetzlich vorgeschrieben |
| **Verständnis-Leiter** | gestufte Strategie bei schlechtem Empfang, statt zu raten |
| **Outbox** | Tabelle, in die Ereignisse zusammen mit dem Fachvorgang geschrieben werden; ein Dispatcher liefert sie an n8n. Verhindert verlorene und doppelte Ereignisse. |
| **Port / Adapter** | `port.py` ist das Interface zum Telefon, `adapters/<anbieter>.py` die konkrete Umsetzung. Anbieterwechsel = eine Datei. |
| **Simulator (`sim/`)** | Text-Telefon im Terminal, treibt den Agenten ohne Voice-Plattform |
| **Slash-Befehl** | wiederkehrender Ablauf für Claude Code, z. B. `/task T-1.3`, hinterlegt in `.claude/commands/` |
| **Abhängigkeitsrichtung** | Regel, welche Schicht welche importieren darf: immer nur nach unten |
