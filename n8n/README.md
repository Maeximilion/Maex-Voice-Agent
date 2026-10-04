# n8n workflows (cold path)

`team_events.json` receives the events the dispatcher (`api/events/dispatcher.py`) posts to `N8N_WEBHOOK_URL` and turns them into one notification for the team.

```text
Event from dispatcher (webhook, POST /webhook/maex, basic auth)
  -> Seen before?            dedupe on the event id
  -> Route by event type     one branch per type
  -> Message: ...            builds title, text, priority
  -> REPLACE ME: team notification channel
  -> Remember event id
  -> Respond: delivered
```

## Events

| Event type | Message to the team | Payload fields used |
|---|---|---|
| `reservation.confirmed` | "Neue Reservierung": party size, date and time | `party_size`, `reserved_for` |
| `callback.created` | "Rückruf offen": the reason in words, priority `high` for a complaint | `reason` |
| `order.handover_failed` | "Bestellung nicht angekommen": pickup code and reason, priority `high` | `pickup_code`, `reason` |

`order.confirmed` never arrives here: the print bridge collects it (`api/events/types.py`, `KITCHEN`). `daily.report` has no branch because nothing produces it yet. `api/tests/test_n8n_workflow.py` fails as soon as an event type reaches n8n without a branch.

The message texts are German because the team reads them. The time is shown in the timezone of the n8n instance (`GENERIC_TIMEZONE` in `docker-compose.yml`).

## Import

1. Import the file: in the editor via "Import from File", or

   ```bash
   docker compose exec n8n n8n import:workflow --input=/workflows/team_events.json
   ```

2. Open the node **Event from dispatcher** and create a "Basic Auth" credential with the values of `N8N_BASIC_AUTH_USER` and `N8N_BASIC_AUTH_PASSWORD` from `.env`. Without it the webhook answers 500 to every request.
3. Replace the node **REPLACE ME: team notification channel** (next section).
4. Publish the workflow. `N8N_WEBHOOK_URL` must point at the production URL `/webhook/maex`, not at `/webhook-test/`.

Importing the file again overwrites the workflow with the same id, including the two manual steps.

## The channel node

The placeholder is a "Stop and Error" node: it fails on purpose, so no event counts as delivered while nobody is notified. Decided 04.10.2026 (D13 in `docs/01_STATUS.md`): a self-hosted push service on the EU server replaces it. Neither the service nor the node is built yet.

The replacement

- receives `title`, `text` and `priority` (`normal` or `high`) and nothing else: the event payload stops at the message nodes, so a node that forwards its whole input sends no customer data,
- must fail when sending fails (no "Continue on Fail"), otherwise a lost notification is recorded as delivered,
- keeps its credentials in n8n. An export contains only the reference (id and name), never the values.

The messages carry no names, phone numbers or free text from the call; the details are on the tablet. Adding such fields sends customer data to whoever runs the channel: check `docs/09_OPERATIONS_LEGAL.md` first.

## What the dispatcher gets back

| Case | Answer | Effect in the outbox |
|---|---|---|
| Notification sent | 200 `delivered` | `sent` |
| Same event id again | 200 `duplicate`, no second notification | `sent` |
| Wrong or missing basic auth | 401 | retry, then `failed` with alarm |
| Event type without a branch | 422 `unknown_event_type` | retry, then `failed` with alarm |
| Channel node fails, or is still the placeholder | 500 | retry, then `failed` with alarm |

## Dedupe

The event id (`X-Idempotency-Key`, same as `id` in the body) is stored in the workflow's static data, the last 1000 ids. It is stored after the channel node succeeded, not before: a failed notification leaves no mark, so the retry is processed again. The price is that a notification can arrive twice (sent, but the answer to the dispatcher got lost, or two runs at the same moment overwrite each other's mark). Twice is the safe side for an alarm.

n8n keeps static data only for a published workflow called through its production URL. Test runs in the editor do not dedupe.

## Changing the workflow

Edit in n8n, download the workflow, overwrite `team_events.json`, then:

```bash
pytest api/tests/test_n8n_workflow.py
```

Checked by hand against n8n 2.40.5 with a local stand-in for the channel: all three event types, a repeated id, an unknown type, wrong password, and a channel outage followed by the retry.
