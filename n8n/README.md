# n8n workflows (cold path)

`team_events.json` receives the events the dispatcher (`api/events/dispatcher.py`) posts to `N8N_WEBHOOK_URL` and turns them into one notification for the team, or, for a change made without the owner's PIN and every change of the call mode, one for the owner and one for the operator (#588, T12 in `docs/23_ACCESS_SECURITY.md`).

```text
Event from dispatcher (webhook, POST /webhook/maex, basic auth)
  -> Seen before?            dedupe on the event id
  -> Route by event type     one branch per type
  -> Message: ...            builds title, text, priority
  -> Send push to team       publishes to the push server in the stack, topic `team`
     (or, for the three change events:
      To owner and ops       one item per topic, `owner` and `ops`
      Send push to owner and ops)
  -> Remember event id
  -> Respond: delivered
```

## Events

| Event type | Message (topic `team` unless named) | Payload fields used |
|---|---|---|
| `reservation.confirmed` | "Neue Reservierung": party size, date and time | `party_size`, `reserved_for` |
| `callback.created` | "Rückruf offen": the reason in words, priority `high` for a complaint | `reason` |
| `order.handover_failed` | "Bestellung nicht angekommen": pickup code and reason (first 120 characters), priority `high` | `pickup_code`, `reason` |
| `daily.report` | "Kasse prüfen": how many orders of earlier business days nobody marked "In Kasse eingegeben" on the tablet, and the day of the oldest (T-11.6, docs/09 §1 L18); one a day from `REGISTER_REPORT_HOUR`, none when all are marked, queued again an hour after a failed one | `count`, `oldest_day` |
| `team_phone.changed` | To `owner` and `ops`: "Teamnummer geändert", today's number and the one before; the check card waits on the tablet | `to`, `from` |
| `day_hours.changed` | To `owner` and `ops`: "Öffnungszeit geändert" with the date, the new and the earlier hours as the tablet words them; "Öffnungszeit wartet auf Freigabe" when longer hours for a later date wait for the admin PIN | `day`, `to`, `from`, `held` |
| `call_mode.changed` | To `owner` and `ops`: "Anrufmodus geändert", the mode now and before with the words of the tablet's header; priority `high` when the agent is switched off | `to`, `from` |

`order.confirmed` never arrives here: the print bridge collects it (`api/events/types.py`, `KITCHEN`). `api/tests/test_n8n_workflow.py` fails as soon as an event type reaches n8n without a branch.

The message texts are German because the team reads them. The time is shown in `Europe/Berlin`, pinned in the workflow settings so it does not depend on the environment of the n8n instance.

`tenant_id` arrives with every event but is not used: one location, one team, one timezone. A second tenant needs its own channel and its timezone in the payload.

## Import

1. Import the file: in the editor via "Import from File", or

   ```bash
   docker compose exec n8n n8n import:workflow --input=/workflows/team_events.json
   ```

2. Open the node **Event from dispatcher** and create a "Basic Auth" credential with the values of `N8N_BASIC_AUTH_USER` and `N8N_BASIC_AUTH_PASSWORD` from `.env`. Without it the webhook answers 500 to every request. These two values protect the webhook only. The editor is protected by the owner account n8n asks for on its first start, so create it right away, and in production also by `N8N_EDITOR_AUTH_USER` and `N8N_EDITOR_AUTH_HASH` in front of it (`docs/13_DEPLOYMENT.md` §3).
3. Open the node **Send push to team** and create a "Header Auth" credential: name `Authorization`, value `Bearer <token>`, the token of the user `n8n` from `NTFY_AUTH_TOKENS` in `.env` (next section). Without it the push server answers 403 and every event fails.
4. Publish the workflow. `N8N_WEBHOOK_URL` must point at the production URL `/webhook/maex`, not at `/webhook-test/`.

Importing the file again with the command above overwrites the workflow with the same id, including the manual steps: repeat steps 2 to 4. "Import from File" in the editor does not overwrite, it loads the nodes into the workflow that is open.

## The channel node and the push server

**Send push to team** posts `title`, `text` and `priority` to the push server, the service `push` in `docker-compose.yml`, at `http://push/` inside the Compose network. The topic is `team`; `normal` becomes priority 3, `high` priority 4. Decided 04.10.2026 (D13 in `docs/01_STATUS.md`): self-hosted, nothing is forwarded to a relay, the messages stay on our server.

The node

- receives `title`, `text` and `priority` and nothing else: the event payload stops at the message nodes, so the push server never sees customer data,
- must fail when sending fails (no "Continue on Fail"), otherwise a lost notification is recorded as delivered,
- gives up after 5 seconds, before the dispatcher's own timeout (`N8N_TIMEOUT_SECONDS`), so a hanging push server does not make the dispatcher send the event a second time while the first run still waits,
- keeps its token in an n8n credential. An export contains only the reference (id and name), never the value.

The messages carry no names, phone numbers or free text from the call; the details are on the tablet. Adding such fields puts customer data on the team's devices: check `docs/09_OPERATIONS_LEGAL.md` first.

### Setting up the push server

The server is closed by default: without users nobody can publish or read. Two users are fixed in `docker-compose.yml`: `n8n` may only write, to the topics `team`, `owner` and `ops`, and `team` may only read `team`.

1. One password hash per user, typed twice at the prompt:

   ```bash
   docker run --rm -it binwiederhier/ntfy:v2.28.0 user hash
   ```

2. One token for the workflow:

   ```bash
   docker run --rm binwiederhier/ntfy:v2.28.0 token generate
   ```

3. Into `.env`, in single quotes because a hash contains `$`:

   ```text
   NTFY_AUTH_USERS='team:<hash>:user,n8n:<hash>:user'
   NTFY_AUTH_TOKENS='n8n:<token>:n8n workflow'
   ```

   The password of `n8n` is never used, the workflow sends the token. `NTFY_BASE_URL` is the address under which the team's devices reach the server (`deploy/Caddyfile`).

4. Start it:

   ```bash
   docker compose up -d push
   ```

5. On each device of the team: subscribe to the topic `team` on that address with the user `team` and its password, in the app of the push server or in its web page.

The owner hears of every change the staff make without the admin PIN and of every change of the call mode (#588), on the topic `owner`. One more user, `owner`, may only read it; without that user the start command sets no rule for it and the stack starts as before, but the pushes to `owner` still go out and wait unread on the server. `n8n` may write to `team`, `owner` and `ops`:

```text
NTFY_AUTH_USERS='team:<hash>:user,n8n:<hash>:user,owner:<hash>:user'
```

Then `docker compose up -d push`, and on the owner's phone subscribe to `owner` with the user `owner`. The operator reads the same changes on `ops` with the alarms below.

The operator's alarms (T-8.3, `api/jobs/monitor.py`) use a topic of their own, `ops`, and do not pass through n8n. Two more users, both or neither: the start command opens `ops` only when both exist, so a stack without them starts as before. `monitor` may only write to `ops` (the dispatcher, with its token), `ops` may only read it (the operator's device):

```text
NTFY_AUTH_USERS='team:<hash>:user,n8n:<hash>:user,ops:<hash>:user,monitor:<hash>:user'
NTFY_AUTH_TOKENS='n8n:<token>:n8n workflow,monitor:<token>:monitor'
MONITOR_PUSH_TOKEN=<the token of monitor>
```

Then `docker compose up -d push dispatcher`, and subscribe to `ops` with the user `ops`. Checked 29.10.2026 against the push server 2.31.1 in a throwaway container: `monitor` publishes to `ops` (200) and not to `team` (403), `n8n` not to `ops` (403, before #588 opened it), `ops` reads `ops` and `team` does not (403); a push from `monitor.push` arrives with title, text and priority.

Locally the server listens on `http://localhost:8090`, loopback only. Android devices keep their own connection to our server. iPhones and iPads get a message at once only through a relay of the app's maker; that is not configured, and Maxi decided on 19.10.2026 to leave it so: the team's devices are Android devices or the web page on the tablet (D14, option A, in `docs/01_STATUS.md`).

## What the dispatcher gets back

| Case | Answer | Effect in the outbox |
|---|---|---|
| Notification sent | 200 `delivered` | `sent` |
| Same event id again | 200 `duplicate`, no second notification | `sent` |
| Wrong or missing basic auth | 401 | retry, then `failed` with alarm |
| Event type without a branch | 422 `unknown_event_type` | retry, then `failed` with alarm |
| Push server down, wrong token or no credential | 500 | retry, then `failed` with alarm |

## What n8n stores

The workflow settings switch the execution history off, for successful and failed runs: a saved run holds the whole event, including guest name, phone number and note. n8n still writes a run to its database while it runs; right after the run the row is marked as deleted and no longer shows in the editor (observed), and n8n's pruning job removes it afterwards (not checked here).

The price: a failed run leaves nothing to look at in the editor. To debug, switch "Save failed production executions" on in the workflow settings for a while, and off again.

## Dedupe

The event id (`X-Idempotency-Key`, same as `id` in the body) is stored in the workflow's static data, the last 1000 ids. It is stored after the channel node succeeded, not before: a failed notification leaves no mark, so the retry is processed again. The price is that a notification can arrive twice (sent, but the answer to the dispatcher got lost, or two runs at the same moment overwrite each other's mark). Twice is the safe side for an alarm.

n8n keeps static data only for a published workflow called through its production URL. Test runs in the editor do not dedupe.

## Changing the workflow

Edit in n8n, download the workflow, overwrite `team_events.json`, then run the test. It also checks what the channel node must not do (carry on after an error, hold a secret in its parameters), and `api/tests/test_push_service.py` checks that node, Compose file and proxy agree:

```bash
pytest api/tests/test_n8n_workflow.py api/tests/test_push_service.py
```

Checked 09.11.2026 for #588 against n8n 2.45.7 and the push server 2.35.0 in a throwaway stack from `docker-compose.yml`, the workflow imported and published: each of the three change events arrives once for `owner` and once for `ops` with the texts above (the call mode switched off at priority 4), a callback only for `team`, a repeated id answers `duplicate`, a stopped push server gives 500 and the retry is delivered; `owner` cannot read `team` and `team` cannot read `owner` (403).

Checked by hand against n8n 2.40.5 and the push server 2.28.0 in a throwaway stack, sent through the dispatcher's own sender: all three event types arrive for the user `team`, a repeated id arrives once, a stopped push server gives 500 after 5 seconds and the retry is delivered, a wrong token gives 500 and no message. The access rules were tried directly: publishing without a token, with the token on another topic and as `team` is refused, reading as `n8n` or without a login too. Earlier, with a stand-in channel: unknown event type, wrong webhook password, and a run without `GENERIC_TIMEZONE`.
