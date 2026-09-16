# 13 – Deployment und Entwicklungsumgebung

---

## 0. Wo was läuft – verbindlich

| Baustein | Läuft auf | Nie auf |
|---|---|---|
| Voice-Plattform (Telefonie, Spracherkennung, Stimme) | beim Anbieter, EU-Rechenzentrum | Maxis PC |
| Agent-API, Datenbank, GUI, n8n | EU-Server (Stufe 1 bis 6) | Maxis PC im Betrieb |
| Sprachmodell des Agenten | beim Voice- oder Modellanbieter | Maxis PC |
| Entwicklung und Simulator | Maxis PC, nur zum Bauen und Testen | – |
| Transkription der Einlern-Aufnahmen (Stufe 4) | EU-Server: Transkriptionsdienst mit EU-Hosting und AVV, oder Whisper-Container auf dem Server (CPU reicht, läuft nachts) | Maxis PC |

**Regel:** Kein Anruf hängt jemals davon ab, ob ein Rechner bei Maxi eingeschaltet ist. Der PC ist Werkbank, nicht Betrieb.

## 1. Warum das früh wichtig ist

Die Voice-Plattform muss unsere Tools über **öffentliches HTTPS** erreichen. Ohne erreichbare Adresse gibt es keinen Testanruf. Das betrifft schon den PoC in Stufe 1, nicht erst den Betrieb.

---

## 2. Entwicklung (lokal)

```text
Maxis PC (nur Werkbank)
  docker compose up      → Postgres, API, n8n lokal zum Entwickeln
  sim/cli.py             → Gespräche ohne Telefon
  Tunnel                 → öffentliche HTTPS-URL auf localhost:8000, nur für Testanrufe
```

**Tunnel** Vorschlag: `cloudflared tunnel --url http://localhost:8000` oder `ngrok http 8000`. Die URL wechselt bei jedem Start, in der Plattform eintragen. Nur für Tests, nie für echte Kunden.

---

## 3. Betrieb (EU-Server)

```text
Internet
  │ 443 only
  ▼
Caddy (TLS automatisch, Reverse Proxy)
  ├── agent.example.com/v1/tools/*  → api:8000  (Token-Pflicht)
  ├── agent.example.com/gui/*        → api:8000  (Basic-Auth oder VPN Annahme)
  └── n8n.example.com                → n8n:5678  (Basic-Auth)
Postgres: nur im Docker-Netz, kein offener Port
```

Annahme: Domainnamen sind Vorschläge.

**Server** Vorschlag: kleiner VPS bei einem Anbieter mit Rechenzentrum in Deutschland, 2 vCPU, 4 GB RAM reichen für Stufe 1–6. Docker, Compose, `ufw` mit 22 und 443. Unattended Upgrades an.

**Compose:** `docker-compose.yml` (Basis) + `deploy/docker-compose.prod.yml` (Caddy, keine offenen DB-Ports, `restart: always`, `--reload` aus).

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
```

---

## 4. Backups

| Was | Wie | Wohin |
|---|---|---|
| Postgres | `pg_dump` täglich 03:00, `scripts/backup.sh` | verschlüsselt auf einen Zweitspeicher bei einem anderen EU-Anbieter (Object Storage) |
| n8n-Workflows | Export nach `n8n/` bei jeder Änderung | Repo |
| `.env` | manuell, verschlüsselt | Passwortmanager |

**Wiederherstellung wird einmal wirklich geprobt** (T-8.6), bevor die erste echte Bestellung läuft. Ein Backup, das nie zurückgespielt wurde, ist eine Hoffnung.

---

## 5. Monitoring (Minimum)

- `/health` alle 60 s von außen prüfen (kostenloser Uptime-Dienst Annahme), bei Ausfall SMS an Maxi
- Heartbeat der Voice-Plattform, falls angeboten
- Outbox: Ereignisse mit Status `failed` → Alarm
- Kosten je Tag über Schwelle → Alarm
- Log-Rotation, 14 Tage lokal

---

## 6. CI

`.github/workflows/ci.yml`: bei jedem Push `ruff check`, `ruff format --check`, `pytest` gegen einen Postgres-Service. Evals laufen nicht in CI (kosten Tokens), sondern vor dem Merge lokal — Ergebnis in die Commit-Nachricht.

---

## 7. Umgebungen

| | dev | prod |
|---|---|---|
| `ENV` | `dev` | `prod` |
| Sim-Konsole in der GUI | an | aus |
| `--reload` | an | aus |
| Test-Mandant | ja | ja, für Rauchtests nach Deploy |
| Anbieter | Testnummer | echte Nummer |
| Löschjob | aus | an |
