# 13 – Deployment und Entwicklungsumgebung

---

## 0. Wo was läuft – verbindlich

| Baustein | Läuft auf | Nie auf |
|---|---|---|
| Own voice layer (docs/20; built, real speech engines open, T-1.11 part 2): Asterisk, service `voice`, speech engines behind ports | In operation: EU server (§3), speech engines are EU speech services (D18). Test phase: the workbench, open speech models, invented data and own test calls only | Maxis PC in operation; a real customer call |
| Agent-API, Datenbank, GUI, n8n, Push-Server für das Team (D13) | EU-Server (Stufe 1 bis 6) | Maxis PC im Betrieb |
| Sprachmodell des Agenten | In operation: at the model provider, processing in the EU. For development and tests: a local model on Maxi's PC is allowed, with invented test data only (text phone, evals). The model is swappable behind `agent/llm.py`, so a better one replaces it without touching the conversation core (T-2.4, T-5.3). Candidates, local setup and `scripts/setup_local_llm.sh`: `docs/18_MODEL_SELECTION.md` | Maxi's PC in operation; real call content in a local model |
| Entwicklung und Simulator | Maxis PC, nur zum Bauen und Testen | – |
| Druckbrücke für den Eingabezettel am Haupt-Bondrucker (T-4.6, D2) | Rechner im Lokal, der den Bondrucker erreicht (Kassenrechner oder eigener Kleinrechner, offen: Maxi) | Maxis PC |
| Transkription der Einlern-Aufnahmen (Stufe 4) | EU-Server: Transkriptionsdienst mit EU-Hosting und AVV, oder Whisper-Container auf dem Server (CPU reicht, läuft nachts) | Maxis PC |

**Regel:** Kein Anruf hängt jemals davon ab, ob ein Rechner bei Maxi eingeschaltet ist. Der PC ist Werkbank, nicht Betrieb. Test calls with invented data may run on the workbench; a real customer call never does.

## 0a. What the restaurant needs

The product is a web application on the EU server, not a program installed in the restaurant. In operation the containers run on the server and nowhere in the restaurant (Docker on Maxi's PC is the workbench, §2); a slow computer on site is no limit, because the views are HTML rendered by the server.

| Needed | For what |
|---|---|
| A device with a current browser and internet: a tablet in the kitchen, optionally a PC | operations view (`/gui/`), later the admin view |
| The address plus user name and password | access, Basic-Auth in `deploy/Caddyfile` (§3) |
| The phone line reaching our software | calls reach the agent: in the test phase the software is one more IP telephone at the pilot's router, in operation a SIP trunk (`docs/20_VOICE_LAYER.md` §3) |
| A phone that rings at the team | the number `transfer_to_team` hands a call to, stored as `team_phone`. The AI telephone dials it only when it is `TEAM_PHONE` of `.env` or on `TRANSFER_TARGETS`, written the same way; any other number rings `TEAM_PHONE` instead (`docs/20_VOICE_LAYER.md` §5). Nothing else uses it: it is no alert for a failed print bridge or for lost internet in the restaurant (that is the red card and the team push, see below). Where calls go when the voice layer is down: `docs/20_VOICE_LAYER.md` §5 states the intended behaviour (target, not yet measured), `docs/02_ARCHITECTURE.md` §5 repeats it |
| From the first order on: the print bridge on one machine that reaches the main receipt printer at the register | until the register has an interface it prints every confirmed order as an input slip the team types in (D2, `docs/02_ARCHITECTURE.md` §2a); `printbridge/README.md`; today this needs Python 3.12 set up by hand |
| A device that receives the team notifications: an app on a phone or the web page on the tablet | callbacks and failed handovers (D13, push server in §3); which devices is open (D14) |

Not needed in the restaurant: the repository, Docker, git, an installer for the product itself. Two small installs remain: the print bridge and, depending on D14, the notification app.

**Why no desktop program (.exe) for the product** (asked 05.10.2026):

- A call must never depend on a computer in the restaurant being switched on, awake and not restarting for an update (rule above, CLAUDE.md §2 rule 5).
- The tablet, the print bridge and, in operation, the telephone access need a fixed public address (§1). A router in a restaurant offers that only with port forwarding or a tunnel.
- On the hot path every tool call would travel over the restaurant's uplink instead of between two data centres (budget in `docs/04_API_TOOLS.md`).
- Tablet, admin PC and print bridge show the same live state. The computer running the program would be a server anyway, only a worse one.
- An update is one deploy on the server instead of one visit per computer.
- Names, phone numbers and addresses are kept on one server whose backup is encrypted (§4; the production server and the second storage are decided, D3 of 07.10.2026, and not yet ordered) instead of on a computer next to the till.
- Postgres and n8n do not fit into one program file; it would be a second product.

The price of this choice: a monthly server bill, the server is ours to operate, and the restaurant depends on its internet connection: without it the tablet shows nothing new and the print bridge fetches no slips. The server only notices through an order: once a slip has waited 60 s without being fetched, the card turns red and `order.handover_failed` is queued for the team push (`docs/02_ARCHITECTURE.md` §5). Outside the mode `primary` a slip exists only after "Passt" on the tablet, so the 60 s start there. The bridge sends no heartbeat: an outage while no order waits goes unnoticed. Nobody outside the tablet is notified until the push server is set up on the server and a device has subscribed (D13, D14), and a tablet without internet does not show the red card either. What happens to the calls in that case: `docs/20_VOICE_LAYER.md` §3 and §5 (test phase: the pilot's router; operation: the forwarding rules of the SIP trunk; target, not yet measured).

Where an installer does make sense is the print bridge, the only software of ours that runs on site (T-9.6, proposal). The wish to start the view from an icon is met by a home-screen icon in the browser (T-3.7, proposal).

## 1. Warum das früh wichtig ist

Tablet, print bridge and, in operation, the telephone access need a fixed **public HTTPS address**. The tunnel for test calls is no longer needed (T-9.1 dropped): no platform has to reach our tools from outside, and test calls with invented data run on the workbench (`docs/20_VOICE_LAYER.md` §3).

---

## 2. Entwicklung (lokal)

```text
Maxis PC (nur Werkbank)
  docker compose up      → Postgres, API, n8n lokal zum Entwickeln
  sim/cli.py             → Gespräche ohne Telefon
  local model (optional) → for sim and evals only, never for a real call (§0);
                           setup: bash scripts/setup_local_llm.sh (docs/18 §5)
                           use:   python -m sim.cli --model qwen3:14b
                                  make eval MODEL=qwen3:14b  (LLM_* in .env)
  voice layer (profile voice) → Asterisk + service voice for test calls with invented data, docs/20; no tunnel
```

**Host ports:** `docker compose up` and `make up` read `docker-compose.override.yml` next to the base file. It publishes Postgres (5432), the API (8000), n8n (5678) and the push server (8090) on `127.0.0.1` only, so nothing on the workbench is reachable from the local network. The base file `docker-compose.yml` publishes no port at all. One consequence on a Linux workbench: `scripts/backup.sh` and `scripts/restore.sh` with the Postgres client from a container (no `pg_dump` on the host) no longer reach `localhost:5432`, because a container gets to the host through the bridge address and not through loopback. Set `BACKUP_DOCKER_NETWORK=<project>_default` there, as on the server. Docker Desktop forwards to the host's loopback and is not affected.

**No tunnel:** until 07.10.2026 this paragraph proposed a tunnel with a public URL for test calls from a hosted platform. With the own voice layer nothing outside calls into the workbench; the VPN to the pilot's router for the test phase is described in `docs/20_VOICE_LAYER.md` §3.

---

## 3. Betrieb (EU-Server)

```text
Internet
  │ 443 only (80 answers the certificate challenge and redirects to 443)
  ▼
Caddy (TLS automatisch, Reverse Proxy)
  ├── agent.example.com/gui/*        → api:8000  (Basic-Auth in deploy/Caddyfile)
  ├── agent.example.com/v1/kitchen/* → api:8000  (token of the print bridge)
  ├── agent.example.com/health       → api:8000
  │      every other path on that name: 404 from the proxy
  ├── n8n.example.com                → n8n:5678  (Basic-Auth in deploy/Caddyfile, own user)
  └── push.example.com               → push:80   (the push server checks its own users)
Postgres: nur im Docker-Netz, kein offener Port
```

Annahme: Domainnamen sind Vorschläge. The real names are set as `AGENT_DOMAIN`, `N8N_DOMAIN` and `PUSH_DOMAIN` in the server's `.env`; the Caddyfile in the repo keeps the placeholders.

**What the proxy passes on (12.10.2026):** only what something outside the server needs: the operations view, the print bridge and `/health` for the uptime check. `/v1/tools/*` and `/v1/calls/*` were public while an external voice platform called them; since 07.10.2026 the voice layer runs on this server and calls neither: `voice` runs the core and the domain in its own process (`api/agent/dispatch.py`, no HTTP). So the proxy answers 404 for them, and for `/docs` and `/openapi.json`, which the API serves in `ENV=dev` only. The token is still required inside the network. Bodies over 256 kB are refused at the proxy. `api/tests/test_deploy_exposure.py` fails when a path is added to the public list or the n8n access is dropped.

**Operations view behind a secret from Caddy (Maxi, 15.10.2026, #252 finding 6):** the proxy's Basic-Auth is the check in front of `/gui/`, and since Asterisk, a service that takes outside input, joined the Docker network, the view also answers only requests that carry `GUI_PROXY_SECRET`. Caddy sets the header `X-Maex-Proxy-Secret` on every request it passes to the API (`deploy/Caddyfile`), replacing one a client sent; `require_gui_proxy` (`api/core/auth.py`) checks it in constant time on every route of the view, the event stream included. Outside dev and test a missing, short or placeholder secret, or one equal to `AGENT_API_TOKEN`, closes the view: the tablet is locked out until `.env` holds it, which is why a deploy checks the tablet (the price that made this a no on 12.10.2026). Only the API and Caddy get the secret; dispatcher and `voice` have it blanked. `/gui/static` (style and script, no data) stays outside the check. Not done, noted for later (T-9.8): separate Docker networks, so that Asterisk reaches only `voice` and nothing else reaches the database; that would also protect the database, n8n and Asterisk itself, not only the view. Before 15.10.2026: accepted for stage 1 without the header (Maxi, 12.10.2026), with the condition to look again when a second tenant or a service that takes outside input joins the network.

**`AGENT_API_TOKEN`:** outside `ENV=dev` and `ENV=test` the API and the dispatcher do not start on the placeholder of `.env.example`, on fewer than 32 characters, or on a value containing whitespace (`openssl rand -hex 32`). Whitespace is rejected rather than trimmed, so the configured credential is never silently changed. Request authentication applies the same production rules even if settings validation was bypassed. An empty token opens nothing in any environment.

**n8n editor:** n8n 2 ignores `N8N_BASIC_AUTH_ACTIVE`, `_USER` and `_PASSWORD` (checked against the pinned image: `/` answers 200 without credentials and offers the owner setup to the first visitor). The editor therefore sits behind its own Basic-Auth in the Caddyfile, `N8N_EDITOR_AUTH_USER` and `N8N_EDITOR_AUTH_HASH` in `.env`, never the tablet's access. Empty values close the editor for everyone. Create the owner account in n8n right after the first start all the same: from inside n8n the operations view is reachable without an access check, because that check is the proxy's. `N8N_BASIC_AUTH_USER` and `_PASSWORD` remain as the credential of the webhook node, which the dispatcher sends.

**Containers in production:** `api` and `dispatcher` run as uid 10001, and every bind mount of the checkout is read-only (the production file repeats the mounts of the base file with `:ro`). The host runs `scripts/backup.sh` from this checkout every night; a container that could write there would run its own code on the host. The files only need to be readable by others, which a `git clone` gives them.

**Database roles (#252, Maxi 12.10.2026):** on the server the application connects as a role of its own that reads and writes rows and nothing else: no superuser, no new table, no dropped one, no file or program of the database server. The user the Postgres image creates (`POSTGRES_USER`, a superuser) owns the schema and stays with migrations, backup and restore. Two lines of `.env` carry it:

| Variable | Names | Read by |
|---|---|---|
| `DATABASE_URL` | the application's role with its password, for example `maex_app` | `api`, `dispatcher`, seed, menu import |
| `OWNER_DATABASE_URL` | the owner, `POSTGRES_USER` with `POSTGRES_PASSWORD`. Host, port and database must be those of `DATABASE_URL`: every script that reads it refuses another server or database (exit 2), so a typo cannot dump, replace or migrate some other database | `scripts/db_role.sh`, `scripts/migrate.sh`, `scripts/backup.sh`, `scripts/restore.sh` |

`bash scripts/db_role.sh` creates the role from these two lines, or brings an existing one to that state: it logs in with the password of `DATABASE_URL`, may read and write every table and use every sequence of the schema, and gets the same on every table the owner creates later. Then the script logs in as the role and checks what it holds; it exits 1 when the role holds an attribute above a plain login, is a member of another role, may create a schema or objects in one, may use a schema besides `public`, cannot use a table or a sequence, may truncate a table or set a sequence, or owns anything in the database or on the server, of any kind (a table, a function, a schema, a database). What the role itself was granted before, on the database, the schema, tables and sequences, is revoked first and the settings, connection limit and expiry it carried are reset, so an existing role with more ends like a new one; a right that reaches it through `PUBLIC` is not revoked and fails that check instead. A role of that name that is a superuser is left as it is and the script exits 2: a typo in `DATABASE_URL` must not take an administrator's rights away. It runs on the host like the backup, through the client container on `BACKUP_DOCKER_NETWORK`, changes no data and is safe to run again. It is also how the password is changed: new password in `DATABASE_URL`, run the script, restart `api` and `dispatcher`.

- **New server:** the database alone, the role, the rest, the schema, and the seed where the tenant does not exist yet. `.env` holds both lines before the first command:

  ```bash
  docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build db
  bash scripts/db_role.sh
  docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
  COMPOSE_FILE=docker-compose.yml:deploy/docker-compose.prod.yml bash scripts/migrate.sh
  docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml exec api python -m scripts.seed
  ```

- **Existing volume, single user so far:** no data moves and nothing changes its owner. Backup first, with `.env` as it is. Then, in `.env`, copy the current `DATABASE_URL` into `OWNER_DATABASE_URL` and give `DATABASE_URL` the new user and a new password (`openssl rand -hex 24` prints one). The script sets the role up, and the last command starts `api` and `dispatcher` again with the new line. The way back is the old `DATABASE_URL`.

  ```bash
  bash scripts/backup.sh --label before_roles
  bash scripts/db_role.sh
  docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d
  ```

  Both URLs must name the same host, port and database (table above). `db_role.sh` does not read `BACKUP_DATABASE_URL`: that override may name the database under another address for backup and restore, and the role belongs to the address the application uses. The final login goes through `DATABASE_URL` itself, with its connection parameters (`sslmode` and the like), so a URL the API could not connect with fails here. A URL that switches to another role after the login (`options=-c role=...`) is refused: the proof has to be about the role that logged in.
- **Migrations** run as the owner: `bash scripts/migrate.sh` (also `make migrate`; arguments go to Alembic, the default is `upgrade head`). It hands `OWNER_DATABASE_URL` to Alembic for that one run, in a container of its own made from the `api` service (Compose `run --rm`), so a migration needs the database up and not the API. The script cannot take `-f` flags; on the server it gets the same two files as every other command through Compose's own variable: `COMPOSE_FILE=docker-compose.yml:deploy/docker-compose.prod.yml bash scripts/migrate.sh`. Without that the one-off container is built from the development layout (root, writable mounts). The application's role cannot change the schema, and a table created by it would be its own to drop.
- **Backup and restore** connect as the owner too (§4). A dump is restored without privileges, so `restore.sh` gives the role its rights on the restored database before the swap; if the server does not know the role, the restore stops with nothing changed and names `db_role.sh`.

Two things keep it that way. Outside `ENV=dev` and `ENV=test` the API does not start when `DATABASE_URL` logs in as a superuser (`api/db.py`, checked once at start, and asked of the role that authenticated: a URL that switches to a plain role with `options=-c role=...` is refused all the same; a database that does not answer does not stop the start). And the Compose file blanks `POSTGRES_PASSWORD`, `OWNER_DATABASE_URL` and `BACKUP_DATABASE_URL` in the environment of `api` and `dispatcher`, which would otherwise get every line of `.env`: a process that can read its environment must not find the superuser's password there (`api/tests/test_database_roles.py`). For the same reason Alembic does not run inside the running `api` container: a process started there with `docker compose exec` has the application's user, and the application could read the owner's URL from `/proc/<pid>/environ` while the migration runs (reproduced 13.10.2026 as uid 10001; from the one-off container nothing is visible). Not covered by either guard: the dispatcher does not check its role at start, it reads the same `DATABASE_URL` as the API, whose refusal shows the mistake.

The workbench keeps the single user of `.env.example`. Tests and evals create and drop their own databases and need the owner for that: on a stack with separate roles `make test` and `make eval` fail until they are given the owner's URL as `DATABASE_URL`. Not covered: the role may still create temporary tables and connect to the other databases of the server (the copies `restore.sh` keeps hold the same data it reads anyway). The check of `db_role.sh` is the proof for a role this script made, and it reports the usual ways an older role holds more. It is no audit of a role an administrator changed by hand: rights on single columns, on functions and types, and in the server's other databases are not looked at.

**Zugang zur Betriebsansicht:** `/gui/*` liegt hinter Basic-Auth im `deploy/Caddyfile`. Die Anwendung selbst prüft dort keinen Token - ein Browser schickt keinen Bearer-Kopf, und die GUI zeigt Gastnamen, Telefonnummern und Notizen. Benutzer und Passwort-Hash stehen als `GUI_BASIC_AUTH_USER` und `GUI_BASIC_AUTH_HASH` in der `.env` des Servers, nie im Repo. Hash erzeugen: `docker run --rm caddy:2-alpine caddy hash-password --plaintext '<PASSWORT>'`. Wer stattdessen ein VPN vor den Server setzt, kann den Block entfernen - aber nicht beides weglassen.

**Server (D3, decided 07.10.2026, in two stages):** test phase on the workbench (invented data and own test calls only); operation on an EU server, netcup VPS 1000 in Nuremberg, 4 vCPU / 8 GB (the voice layer adds Asterisk and a Python process, docs/20 §2). It is ordered when the first of these is due: the team uses tablet or print bridge against the real system, the measurement calls for G1, or the first real customer call. Docker, Compose, `ufw` mit 22 und 443. Unattended Upgrades an.

**Druckbrücke:** holt Bons per HTTPS ab, im Router des Lokals bleibt alles zu. Einrichtung in `printbridge/README.md`; auf dem Server `KITCHEN_BRIDGE_TOKEN` und `KITCHEN_BRIDGE_TENANT_ID` setzen, ohne beide ist `/v1/kitchen/*` zu. Genau eine Brücke je Betrieb.

**Push server (D13):** the service `push` carries the team notifications from the n8n workflow to the team's devices. It is closed by default and knows two users, `n8n` (write only) and `team` (read only); hashes and token live in `.env`. Nothing is forwarded to a relay outside. Setup in `n8n/README.md`; it needs the DNS name from the Caddyfile and `NTFY_BASE_URL` set to it.

**Compose:** `docker-compose.yml` (Basis) + `deploy/docker-compose.prod.yml` (Caddy, `restart: always`, `--reload` aus). Docker Compose 2.24.4 or later: the production file removes the graphics card of `voice` with `!reset`, which an older Compose does not read.

**Published ports:** only Caddy publishes host ports (80 and 443). Postgres, API, n8n and the push server have no `ports:` entry in either of the two files and are reachable only inside the Docker network. Three rules keep it that way:

- Compose merges `ports` lists across files. `ports: []` in an override removes nothing; until 05.10.2026 the production file relied on exactly that and the rendered stack published 5432, 8000 and 5678 on all interfaces. A service that needs a host port for development gets it in `docker-compose.override.yml`, never in the base file.
- Always start with both `-f` flags as below. With `-f`, Compose does not read `docker-compose.override.yml`. A bare `docker compose up` on the server would start the development layout: no Caddy, ports on loopback.
- Docker publishes ports past `ufw`. The firewall rule for 22 and 443 does not close a published container port, so the Compose files are the control. `api/tests/test_compose_ports.py` fails when the merged production configuration publishes a port outside Caddy. Check on the server after every deploy: `docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps` must show host ports for `caddy` only.

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
```

**What the build installs:** `--build` builds the API image on the server. `api/Dockerfile` installs `api/requirements.txt` with `--require-hashes`: exact versions with hashes, compiled from `api/requirements.in` by `make lock` (issue #252). The server therefore gets the versions CI tested, not whatever is newest on the day of the deploy, and a package whose file does not match its hash stops the build. The images are pinned to a version as well (`caddy` here, `n8n` and `ntfy` in the base file); Dependabot is set up to propose updates for both (`.github/dependabot.yml`; not yet seen working on the compiled file, the first weekly run shows it). Not pinned: the base image tag `python:3.12-slim` moves with its patch releases, and `apt-get` installs the current `curl` and `ca-certificates`. The compiled file is for Linux and Python 3.12; a Windows host cannot install it (README, Quick Start).

---

## 4. Backups

| Was | Wie | Wohin |
|---|---|---|
| Postgres | `pg_dump` täglich 03:00, `scripts/backup.sh` | verschlüsselt auf einen Zweitspeicher bei einem anderen EU-Anbieter: Scaleway Object Storage (decided 07.10.2026, D3), ordered together with the server |
| n8n-Workflows | Export nach `n8n/` bei jeder Änderung | Repo |
| `.env` | manuell, verschlüsselt | Passwortmanager |

**Wiederherstellung wird einmal wirklich geprobt** (T-8.6), bevor die erste echte Bestellung läuft. Ein Backup, das nie zurückgespielt wurde, ist eine Hoffnung.

### Scripts (T-9.3)

Both scripts act on the database of `DATABASE_URL`, the same variable the application and the menu import read, so a backup can never come from a different stack than the one being changed. They read their configuration from the environment or from `.env`:

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | which database is dumped and replaced, and with a single database user also who connects |
| `OWNER_DATABASE_URL` | where the application has a role of its own (§3): the owner of the schema, who dumps and restores. It must name the host, port and database of `DATABASE_URL`, or the scripts stop with exit 2. `BACKUP_DATABASE_URL` overrides both for these two scripts only, for example to reach the same database under another host name; it is the one address that is not compared |
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

**Backup:** `pg_dump` in custom format, encrypted with gpg (AES256, symmetric), written as `maex_<database>_<UTC time>[_label].dump.gpg`, readable only by its owner (mode 600; on Windows the folder decides). The dump is read back to its end with the same passphrase before it counts (gpg checks integrity, `pg_restore` unpacks every table); a failed run keeps no file, removes no older backup and exits 1. Every run writes its own temporary file and publishes it without replacing anything: of two backups started in the same second one succeeds and the other exits 1. Both scripts create the gpg home directory (the one `gpgconf --list-dirs homedir` names: `GNUPGHOME` or `~/.gnupg`; mode 700) themselves before gpg runs: gpg creates a missing one without a lock, and of two runs that start together on a fresh server one would die with "can't create directory ... File exists". Connection parameters of the URL (`sslmode`, `sslrootcert`, `sslcert`, `sslkey`, `sslcrl`, `connect_timeout`, `application_name`, `options`, `channel_binding`, `gssencmode`, `target_session_attrs`) are passed on to the client; any other parameter stops the script with exit 2 instead of being dropped, and certificate files need a local client because they do not exist in the client container. `--no-encrypt` writes a plain dump and is only for a database without customer data.

**Restore:** the dump always goes into a new database first. `--check` reports table count and schema revision and drops that database again. `--replace` renames the current database to `<name>_before_restore_<time>` and the restored one into its place, both renames in one transaction. Nothing is dropped: the way back is a rename, and the old database is removed by hand once the restored state is checked. That copy holds customer data and nothing expires it, so every later run of `restore.sh` names the copies that still exist. `--replace` wants the database name as a confirmation and refuses while sessions are connected, so stop `api` and `dispatcher` first (`docker compose stop api dispatcher`). If the database is gone entirely, `--replace` creates it from the dump. If the swap itself reports an error, the script asks the server which names exist before it says anything: "nothing changed" only when the swap did not happen, a warning and exit 0 when it did, and "outcome unknown" with what to look for when the server cannot be asked. The restored schema is at the revision of the dump; if the code is newer, `bash scripts/migrate.sh` follows. The dump is restored without owners and privileges (`--no-owner --no-privileges`), so everything belongs to the user who restores. Where the application has a role of its own (the user of `DATABASE_URL`, §3), the script gives that role its rights on the restored database before the swap, in `--check` as well; a role the server does not know stops the restore with nothing changed.

**Cron:** `deploy/backup.cron` runs the backup daily at 03:00 and appends to `backups/backup.log`. Nobody is told yet when a night's backup fails: that alarm comes with the monitoring (T-8.3).

**Rehearsed 04.10.2026** on a throwaway Postgres 16 with schema revision 005, the seed and the eval menu (17 tables): backup 2.7 s and 48 kB, then the menu deleted and a table dropped, `--check` 6.5 s, `--replace` 8.0 s, row counts of all 17 tables identical to the state before. The same once through a Docker network with the host name `db`, as on the server. **Rehearsed with separate roles 13.10.2026** on a throwaway Compose stack in the production layout (17 tables, the seed): an existing single-user volume moved over, a fresh volume set up role first, a migration down and up through `scripts/migrate.sh`, a backup, a changed row, `--replace`, and the operations view answering from the restored database as the application's role. Known limits: roles are not part of a dump, so a new server needs `scripts/db_role.sh` before a restore; without a local client on Linux, a database published on `127.0.0.1` only is not reachable from the client container (install a client or use the Compose network). The second storage is decided (Scaleway Object Storage, D3 of 07.10.2026) and is ordered together with the server. Still open: the rehearsal on the production server once it exists (T-8.6).

---

## 5. Monitoring (Minimum)

- `/health` alle 60 s von außen prüfen (kostenloser Uptime-Dienst Annahme), bei Ausfall SMS an Maxi
- Heartbeat of the voice layer (`voice` and Asterisk), planned with docs/20 §5
- Outbox: Ereignisse mit Status `failed` → Alarm
- Kosten je Tag über Schwelle → Alarm
- Log-Rotation, 14 Tage lokal

---

## 6. CI

`.github/workflows/ci.yml`: bei jedem Pull Request und jedem Push auf `main` `ruff check`, `ruff format --check`, dann pytest in zwei Schritten gegen einen Postgres-Service: `pytest -n auto -m "not latency"` parallel, danach `pytest -m latency` seriell. Ein neuer Push auf denselben PR bricht den alten Lauf ab. Jeder Test bekommt eine Kopie einer einmal migrierten Vorlage-Datenbank (`migrated_template_url`), statt Alembic je Test laufen zu lassen; lokal fiel die Suite damit von 8:40 auf 3:01 min, mit 4 Workern auf 1:22 min (03.10.2026). Die Latenztests (Budget < 300 ms, CLAUDE.md §7) tragen `@pytest.mark.latency` und laufen nicht parallel: unter `-n auto` konkurrieren die Worker um CPU und Postgres, p95 maß dann die Last statt des Codes (302 bis 306 ms auf `main` und zwei PRs, seriell rund 150 ms). Der serielle Schritt kostet rund 25 s. `p95_ms` mit echter Uhr bricht in einem Test ohne Marker ab, damit kein neuer Latenztest in den parallelen Schritt rutscht; unter einem xdist-Worker wird ein Latenztest übersprungen (`pytest -n 4` lokal zeigt sie als skipped). The eval suite `evals/cases/` runs in CI on the stand-in model (`api/tests/test_evals_runner.py`, `docs/08_EVALS.md`); a run against a real model costs tokens and stays local, before the merge.

Next to the `test` job (reviewed 12.10.2026, no workflow does the work of another): `docker-smoke` builds the API image and checks health and token auth in the running container; `voice-image` builds the image of `voice` (target `voice`, the speech engines' hashed packages) and runs `api/tests/test_telephony_engine_loading.py` in it, without a network: an engine without its model files is refused (not a required check); `dependency-review` fails a pull request that adds a dependency with a known high or critical vulnerability; `pr-title` holds the title to Conventional Commits; CodeQL runs as GitHub's default setup (Python, JavaScript, Actions); `project-maintenance` maintains the board once a day (`docs/16_GITHUB_PROJECT.md`). Lint covers `api scripts evals printbridge sim db`, the same list in `make lint`. `mypy` runs right after the two ruff steps and in `make lint`, non-strict, over the same folders; its paths and settings are in `pyproject.toml` under `[tool.mypy]`. Actions are pinned to a commit SHA with the version as a comment; Dependabot moves both. The `test` job stops after 15 minutes. The `test` job installs `api/requirements.txt` with `pip install --require-hashes`, the same compiled lock file the image build uses (`make lock` regenerates it from `api/requirements.in`), so the tests and the image run the same package versions; the lock is compiled for Linux and Python 3.12, which is what the runner and the image are.

**Required checks on `main`** live in the repository settings, not in a workflow: `.github/rulesets/main.json` is the ruleset as it should be (pull request required, no force push, no deletion, and the checks `test`, `docker-smoke`, `title`, `dependency-review` green on the head commit). Without it a red pull request can be merged, and hard rule 4 holds by habit only. Apply it from the repository folder, on a checkout that has the file. `gh` fills `{owner}` and `{repo}` from the checkout itself; the number is the ruleset's id, which the first command prints:

```bash
gh api "repos/{owner}/{repo}/rulesets" --jq '.[] | "\(.id) \(.name) \(.enforcement)"'
gh api -X PUT "repos/{owner}/{repo}/rulesets/24583491" --input .github/rulesets/main.json
```

**Applied 13.10.2026:** the active ruleset on `main` (id 24583491) carries these rules; `gh api "repos/{owner}/{repo}/rules/branches/main"` lists the pull request rule, no deletion, no force push and the four required checks. Its name on GitHub is still `t-4.6-probebo`: the second command above is refused with "Name must be unique" as long as the disabled ruleset `Main` (id 23989916) exists, because GitHub compares the names without case. The rules were sent with the existing name instead. To send the file as it is, rename or delete the disabled ruleset first.

A job renamed in `ci.yml` has to be renamed there too, otherwise the old name never reports and every pull request waits.

### CodeQL and dependency updates

GitHub's default setup scans pushes to `main`, the weekly schedule and pull requests, but
not pull requests that Dependabot opens
([upstream explanation](https://github.com/github/codeql-action/issues/2858)) and not pull
requests from forks ([setup types](https://docs.github.com/en/code-security/concepts/code-scanning/setup-types));
their `CodeQL` result is neutral with "configurations not found". Decided 19.10.2026
(Maxi): the default setup stays, and both gaps are accepted:

- **Dependabot:** `pip` and `docker-compose` updates change version pins, `docker` updates
  a base image, `github-actions` updates a pinned action in a workflow. `dependency-review`
  only rejects new versions with known high or critical vulnerabilities; it does not
  analyze a changed workflow. What covers the rest: the human review of every Dependabot
  pull request, and CodeQL on `main` after the merge, which flags the changed workflow
  there.
- **Forks:** the repository is public; every outside pull request is treated as untrusted and
  read by a person before a merge; CodeQL on `main` scans it after the merge.

An explicit `codeql.yml` workflow that closed both gaps was built and removed again (PR
#333 and PR #335): it meant a workflow file to maintain, merge commits instead of squash
for every Dependabot pull request, and new languages added by hand.

---

## 7. Umgebungen

| | dev | prod |
|---|---|---|
| `ENV` | `dev` | `prod` |
| Sim-Konsole in der GUI | an | aus |
| `--reload` | an | aus |
| Test-Mandant | ja | ja, für Rauchtests nach Deploy |
| Telephone access | test number via the pilot's router (docs/20 §3) | real number |
| Löschjob | aus | an |
