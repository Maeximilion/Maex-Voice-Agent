# Sicherheit

## Lücken melden

Sicherheitslücken bitte **nicht** als öffentliches Issue anlegen. Stattdessen über „Report a vulnerability" im Reiter Security dieses Repositories melden (GitHub Private Vulnerability Reporting).

Hilfreich in der Meldung: betroffene Komponente, Schritte zum Nachstellen, mögliche Auswirkung. Eine Rückmeldung erfolgt innerhalb von sieben Tagen.

## Geltungsbereich

Der Agent nimmt Telefonate entgegen und verarbeitet dabei Namen, Rufnummern, Adressen und Bestelldaten. Besonders relevant sind deshalb:

- die Agent-API unter `/v1/tools/*`, erreichbar nur mit Bearer-Token (`AGENT_API_TOKEN`)
- die Team-Oberfläche und deren Sitzungen
- die Ereignis-Warteschlange zur Automatisierung und deren Ziele
- Datenbankzugriff, Backups und deren Ablageorte

## Grundregeln im Betrieb

- Zugangsdaten ausschließlich über Umgebungsvariablen, nie im Repository. `.env` ist in `.gitignore`.
- Jeder Schreibvorgang trägt eine `call_id` und landet im `audit_log`.
- Personenbezogene Daten tragen eine Löschfrist, ein täglicher Job wendet sie an.
- Die gesamte Verarbeitung läuft auf Servern in der EU, einschließlich Transkription und Auswertung.
- Echte Anrufaufnahmen werden erst verarbeitet, wenn die Rechts-Checkliste in `docs/09_BETRIEB_RECHT.md` abgeschlossen ist.
- Vor jeder Migration auf Echtdaten wird ein Backup gezogen.

## Unterstützte Version

Das Projekt ist im Aufbau. Sicherheitsmeldungen beziehen sich auf den aktuellen Stand von `main`.
