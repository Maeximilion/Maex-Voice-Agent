# 13 – Deployment und Entwicklungsumgebung

---

## 0. Wo was läuft – verbindlich

| Baustein | Läuft auf | Nie auf |
|---|---|---|
| Voice-Plattform (Telefonie, Spracherkennung, Stimme) | beim Anbieter, EU-Rechenzentrum | Maxis PC |
| Agent-API, Datenbank, GUI, n8n, Push-Server für das Team (D13) | EU-Server (Stufe 1 bis 6) | Maxis PC im Betrieb |
| Sprachmodell des Agenten | beim Voice- oder Modellanbieter | Maxis PC |
| Entwicklung und Simulator | Maxis PC, nur zum Bauen und Testen | – |
| Druckbrücke für den Eingabezettel am Haupt-Bondrucker (T-4.6, D2) | Rechner im Lokal, der den Bondrucker erreicht (Kassenrechner oder eigener Kleinrechner, offen: Maxi) | Maxis PC |
| Transkription der Einlern-Aufnahmen (Stufe 4) | EU-Server: Transkriptionsdienst mit EU-Hosting und AVV, oder Whisper-Container auf dem Server (CPU reicht, läuft nachts) | Maxis PC |

**Regel:** Kein Anruf hängt jemals davon ab, ob ein Rechner bei Maxi eingeschaltet ist. Der PC ist Werkbank, nicht Betrieb.

## 0a. What the restaurant needs

The product is a web application on the EU server, not a program installed in the restaurant. In operation the containers run on the server and nowhere in the restaurant (Docker on Maxi's PC is the workbench, §2); a slow computer on site is no limit, because the views are HTML rendered by the server.

| Needed | For what |
|---|---|
| A device with a current browser and internet: a tablet in the kitchen, optionally a PC | operations view (`/gui/`), later the admin view |
| The address plus user name and password | access, Basic-Auth in `deploy/Caddyfile` (§3) |
| The phone line routed to the voice platform | calls reach the agent (provider open, D1) |
| A phone that rings at the team | the number `transfer_to_team` hands a call to, stored as `team_phone`. Nothing else uses it: it is no alert for a failed print bridge or for lost internet in the restaurant (that is the red card and the team push, see below). Where calls go when the voice platform is down depends on how the forward is set up, which is open with C2; `docs/02_ARCHITECTURE.md` §5 states the intended behaviour, not a tested one |
| From the first order on: the print bridge on one machine that reaches the main receipt printer at the register | until the register has an interface it prints every confirmed order as an input slip the team types in (D2, `docs/02_ARCHITECTURE.md` §2a); `printbridge/README.md`; today this needs Python 3.12 set up by hand |
| A device that receives the team notifications: an app on a phone or the web page on the tablet | callbacks and failed handovers (D13, push server in §3); which devices is open (D14) |

Not needed in the restaurant: the repository, Docker, git, an installer for the product itself. Two small installs remain: the print bridge and, depending on D14, the notification app.

**Why no desktop program (.exe) for the product** (asked 05.10.2026):

- A call must never depend on a computer in the restaurant being switched on, awake and not restarting for an update (rule above, CLAUDE.md §2 rule 5).
- The voice platform has to reach the tools over public HTTPS at a fixed address (§1). A router in a restaurant offers neither without port forwarding or a tunnel.
- On the hot path every tool call would travel over the restaurant's uplink instead of between two data centres (budget in `docs/04_API_TOOLS.md`).
- Tablet, admin PC and print bridge show the same live state. The computer running the program would be a server anyway, only a worse one.
- An update is one deploy on the server instead of one visit per computer.
- Names, phone numbers and addresses are kept on one server whose backup is encrypted (§4; the second storage and the production server are still open) instead of on a computer next to the till.
- Postgres and n8n do not fit into one program file; it would be a second product.

The price of this choice: a monthly server bill, the server is ours to operate, and the restaurant depends on its internet connection: without it the tablet shows nothing new and the print bridge fetches no slips. The server notices after 60 s: the card turns red and `order.handover_failed` is queued for the team push (`docs/02_ARCHITECTURE.md` §5). Nobody outside the tablet is notified until the push server is set up on the server and a device has subscribed (D13, D14), and a tablet without internet does not show the red card either. What happens to the calls in that case depends on where the forward is done. Today the Fritz!Box on the same line does it; whether the provider can forward in its network is open with C2 (`docs/01_STATUS.md`, phone line).

Where an installer does make sense is the print bridge, the only software of ours that runs on site (T-9.6, proposal). The wish to start the view from an icon is met by a home-screen icon in the browser (T-3.7, proposal).

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

**Host ports:** `docker compose up` and `make up` read `docker-compose.override.yml` next to the base file. It publishes Postgres (5432), the API (8000), n8n (5678) and the push server (8090) on `127.0.0.1` only, so nothing on the workbench is reachable from the local network. A tunnel on the same machine still reaches `localhost:8000`. The base file `docker-compose.yml` publishes no port at all. One consequence on a Linux workbench: `scripts/backup.sh` and `scripts/restore.sh` with the Postgres client from a container (no `pg_dump` on the host) no longer reach `localhost:5432`, because a container gets to the host through the bridge address and not through loopback. Set `BACKUP_DOCKER_NETWORK=<project>_default` there, as on the server. Docker Desktop forwards to the host's loopback and is not affected.

**Tunnel** Vorschlag: `cloudflared tunnel --url http://localhost:8000` oder `ngrok http 8000`. Die URL wechselt bei jedem Start, in der Plattform eintragen. Nur für Tests, nie für echte Kunden.

---

## 3. Betrieb (EU-Server)

```text
Internet
  │ 443 only (80 answers the certificate challenge and redirects to 443)
  ▼
Caddy (TLS automatisch, Reverse Proxy)
  ├── agent.example.com/v1/tools/*  → api:8000  (Token-Pflicht)
  ├── agent.example.com/gui/*        → api:8000  (Basic-Auth in deploy/Caddyfile)
  ├── n8n.example.com                → n8n:5678  (Basic-Auth)
  └── push.example.com               → push:80   (the push server checks its own users)
Postgres: nur im Docker-Netz, kein offener Port
```

Annahme: Domainnamen sind Vorschläge.

**Zugang zur Betriebsansicht:** `/gui/*` liegt hinter Basic-Auth im `deploy/Caddyfile`. Die Anwendung selbst prüft dort keinen Token - ein Browser schickt keinen Bearer-Kopf, und die GUI zeigt Gastnamen, Telefonnummern und Notizen. Benutzer und Passwort-Hash stehen als `GUI_BASIC_AUTH_USER` und `GUI_BASIC_AUTH_HASH` in der `.env` des Servers, nie im Repo. Hash erzeugen: `docker run --rm caddy:2-alpine caddy hash-password --plaintext '<PASSWORT>'`. Wer stattdessen ein VPN vor den Server setzt, kann den Block entfernen - aber nicht beides weglassen.

**Server** Vorschlag: kleiner VPS bei einem Anbieter mit Rechenzentrum in Deutschland, 2 vCPU, 4 GB RAM reichen für Stufe 1–6. Docker, Compose, `ufw` mit 22 und 443. Unattended Upgrades an.

**Druckbrücke:** holt Bons per HTTPS ab, im Router des Lokals bleibt alles zu. Einrichtung in `printbridge/README.md`; auf dem Server `KITCHEN_BRIDGE_TOKEN` und `KITCHEN_BRIDGE_TENANT_ID` setzen, ohne beide ist `/v1/kitchen/*` zu. Genau eine Brücke je Betrieb.

**Push server (D13):** the service `push` carries the team notifications from the n8n workflow to the team's devices. It is closed by default and knows two users, `n8n` (write only) and `team` (read only); hashes and token live in `.env`. Nothing is forwarded to a relay outside. Setup in `n8n/README.md`; it needs the DNS name from the Caddyfile and `NTFY_BASE_URL` set to it.

**Compose:** `docker-compose.yml` (Basis) + `deploy/docker-compose.prod.yml` (Caddy, `restart: always`, `--reload` aus).

**Published ports:** only Caddy publishes host ports (80 and 443). Postgres, API, n8n and the push server have no `ports:` entry in either of the two files and are reachable only inside the Docker network. Three rules keep it that way:

- Compose merges `ports` lists across files. `ports: []` in an override removes nothing; until 05.10.2026 the production file relied on exactly that and the rendered stack published 5432, 8000 and 5678 on all interfaces. A service that needs a host port for development gets it in `docker-compose.override.yml`, never in the base file.
- Always start with both `-f` flags as below. With `-f`, Compose does not read `docker-compose.override.yml`. A bare `docker compose up` on the server would start the development layout: no Caddy, ports on loopback.
- Docker publishes ports past `ufw`. The firewall rule for 22 and 443 does not close a published container port, so the Compose files are the control. `api/tests/test_compose_ports.py` fails when the merged production configuration publishes a port outside Caddy. Check on the server after every deploy: `docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps` must show host ports for `caddy` only.

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

### Scripts (T-9.3)

Both scripts act on the database of `DATABASE_URL`, the same variable the application and the menu import read, so a backup can never come from a different stack than the one being changed. They read their configuration from the environment or from `.env`:

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | which database is dumped and replaced (`BACKUP_DATABASE_URL` overrides it for the scripts only) |
| `BACKUP_PASSPHRASE_FILE` | file with the passphrase, readable only by the backup user; never in the repo. Without it no dump is written. The passphrase is the first line without its line ending, so a file saved with Windows line endings gives the same key. The passphrase also belongs in the password manager: without it no backup can be read |
| `BACKUP_DIR` | where dumps go, default `backups/` (ignored by git) |
| `BACKUP_KEEP_DAYS` | scheduled dumps older than this are removed after a good run, default 14, `0` keeps everything |
| `BACKUP_DOCKER_NETWORK` | Compose network of the stack, for example `maex-voice-agent_default`. Needed on the server: Postgres has no published port there and the host `db` only resolves inside that network |
| `BACKUP_PG_CLIENT` | `auto` (default), `local` or `docker`. `auto` takes the client from the `postgres:16-alpine` image (`BACKUP_PG_IMAGE`) when no `pg_dump` is on PATH, when `BACKUP_DOCKER_NETWORK` is set, or when the local client has another major version than the server |

```bash
bash scripts/backup.sh                              # scheduled dump, also `make backup`
bash scripts/backup.sh --label before_menu_import   # by hand, never removed by the retention
bash scripts/restore.sh <file> --check              # rehearsal, the live database is not touched
bash scripts/restore.sh <file> --replace <database> # the real restore
```

**Backup:** `pg_dump` in custom format, encrypted with gpg (AES256, symmetric), written as `maex_<database>_<UTC time>[_label].dump.gpg`, readable only by its owner (mode 600; on Windows the folder decides). The dump is read back to its end with the same passphrase before it counts (gpg checks integrity, `pg_restore` unpacks every table); a failed run keeps no file, removes no older backup and exits 1. Every run writes its own temporary file and publishes it without replacing anything: of two backups started in the same second one succeeds and the other exits 1. Connection parameters of the URL (`sslmode`, `sslrootcert`, `sslcert`, `sslkey`, `sslcrl`, `connect_timeout`, `application_name`, `options`, `channel_binding`, `gssencmode`, `target_session_attrs`) are passed on to the client; any other parameter stops the script with exit 2 instead of being dropped, and certificate files need a local client because they do not exist in the client container. `--no-encrypt` writes a plain dump and is only for a database without customer data.

**Restore:** the dump always goes into a new database first. `--check` reports table count and schema revision and drops that database again. `--replace` renames the current database to `<name>_before_restore_<time>` and the restored one into its place, both renames in one transaction. Nothing is dropped: the way back is a rename, and the old database is removed by hand once the restored state is checked. That copy holds customer data and nothing expires it, so every later run of `restore.sh` names the copies that still exist. `--replace` wants the database name as a confirmation and refuses while sessions are connected, so stop `api` and `dispatcher` first (`docker compose stop api dispatcher`). If the database is gone entirely, `--replace` creates it from the dump. If the swap itself reports an error, the script asks the server which names exist before it says anything: "nothing changed" only when the swap did not happen, a warning and exit 0 when it did, and "outcome unknown" with what to look for when the server cannot be asked. The restored schema is at the revision of the dump; if the code is newer, `alembic -c db/alembic.ini upgrade head` follows.

**Cron:** `deploy/backup.cron` runs the backup daily at 03:00 and appends to `backups/backup.log`. Nobody is told yet when a night's backup fails: that alarm comes with the monitoring (T-8.3).

**Rehearsed 04.10.2026** on a throwaway Postgres 16 with schema revision 005, the seed and the eval menu (17 tables): backup 2.7 s and 48 kB, then the menu deleted and a table dropped, `--check` 6.5 s, `--replace` 8.0 s, row counts of all 17 tables identical to the state before. The same once through a Docker network with the host name `db`, as on the server. Known limits: the restore uses `--no-owner --no-privileges`, right for today's single database user and to be revisited when roles are separated; without a local client on Linux, a database published on `127.0.0.1` only is not reachable from the client container (install a client or use the Compose network). Still open: the second storage at another EU provider (which provider is a cost decision, with D3), and the rehearsal on the production server once it exists (T-8.6).

---

## 5. Monitoring (Minimum)

- `/health` alle 60 s von außen prüfen (kostenloser Uptime-Dienst Annahme), bei Ausfall SMS an Maxi
- Heartbeat der Voice-Plattform, falls angeboten
- Outbox: Ereignisse mit Status `failed` → Alarm
- Kosten je Tag über Schwelle → Alarm
- Log-Rotation, 14 Tage lokal

---

## 6. CI

`.github/workflows/ci.yml`: bei jedem Pull Request und jedem Push auf `main` `ruff check`, `ruff format --check`, dann pytest in zwei Schritten gegen einen Postgres-Service: `pytest -n auto -m "not latency"` parallel, danach `pytest -m latency` seriell. Ein neuer Push auf denselben PR bricht den alten Lauf ab. Jeder Test bekommt eine Kopie einer einmal migrierten Vorlage-Datenbank (`migrated_template_url`), statt Alembic je Test laufen zu lassen; lokal fiel die Suite damit von 8:40 auf 3:01 min, mit 4 Workern auf 1:22 min (03.10.2026). Die Latenztests (Budget < 300 ms, CLAUDE.md §7) tragen `@pytest.mark.latency` und laufen nicht parallel: unter `-n auto` konkurrieren die Worker um CPU und Postgres, p95 maß dann die Last statt des Codes (302 bis 306 ms auf `main` und zwei PRs, seriell rund 150 ms). Der serielle Schritt kostet rund 25 s. `p95_ms` mit echter Uhr bricht in einem Test ohne Marker ab, damit kein neuer Latenztest in den parallelen Schritt rutscht; unter einem xdist-Worker wird ein Latenztest übersprungen (`pytest -n 4` lokal zeigt sie als skipped). Evals laufen nicht in CI (kosten Tokens), sondern vor dem Merge lokal — Ergebnis in die Commit-Nachricht.

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
