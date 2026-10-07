# 20 – Own Voice Layer

> Version 1.0 · 07.10.2026 · Decisions by Maxi in §1. Nothing in this file is built or measured yet.
> Every product property and price below comes from web research of 07.10.2026 and is an
> assumption until a row in §11 says it was measured.

The voice layer is everything between the telephone network and the conversation core: taking the
call, turning the caller's speech into text, speaking the answer, handing a call to the team. Until
07.10.2026 the plan bought this from a hosted voice platform (E1 in `docs/00_PCF.md`). Now we build it.

---

## 1. Decisions (Maxi, 07.10.2026)

| # | Decision |
|---|---|
| E1 (flipped) | No hosted voice platform. The voice layer is ours and runs on our own server. |
| E15 | It starts as a chain: speech-to-text, our conversation core, text-to-speech. Ear and mouth sit behind small ports and can be swapped. A model that hears and speaks itself is measured against the chain with the same cases and replaces it only if the numbers say so (hard rule 4). A hosted platform is the last resort, see §13. |
| E16 | The product does not depend on a restaurant's router or carrier: the software speaks SIP. Nothing heavy runs in the restaurant. One model call per turn; no multi-agent setup on a live call. |
| D18 | Test phase: free, open speech models on the workbench, invented data only. Operation: EU speech services, chosen by measurement before the first real call. |
| D3 | Test phase: the workbench hosts. Operation: an EU server, ordered when the first of these is due: the team uses tablet or print bridge against the real system, the measurement calls for G1, or the first real customer call. |
| Access | Test phase: the software registers as one more telephone at the pilot's router and uses one of its three numbers. Operation and further restaurants: a SIP trunk. The trunk is decided at customer acceptance, or earlier if the router path fails its test. |

**Why the chain first.** The conversation core takes one finished sentence as text and returns one
sentence as text (`api/telephony/port.py`, `api/agent/loop.py`). With a chain it stays as it is, and
so do the eval suite and the guards that need the caller's words as text before the model answers
(`agent/escalation.py`, `agent/consent.py`). Research on speech-to-speech models found, on a public
task benchmark for voice agents, 30 to 46 % of tasks resolved against about 85 % for text models, a
time to first audio of 0.85 to 1.35 s (no clear gain) and one to five times the cost of a chain.
These numbers are not ours; T-10.10 measures the candidates on our own cases.

**What stays true.** E9 and the rule of `docs/13_DEPLOYMENT.md` §0: the workbench is for building
and testing. Real customer calls and customer data run on the EU server only.

---

## 2. Components

```text
caller → telephone network → access (test: the pilot's router as PBX · operation: SIP trunk)
       → Asterisk → call audio over WebSocket → service "voice" (Python)
            ear:   speech-to-text  → one finished utterance as text
            core:  CallHandler and run_turn, unchanged
            mouth: text-to-speech  → audio back to Asterisk
```

| Piece | Runs as | Planned place |
|---|---|---|
| Registration, dialplan, media | container `asterisk` (22 LTS, 22.6 or later for `chan_websocket`) | `deploy/asterisk/` |
| Voice process | service `voice`, same image as `api`, one worker | `api/telephony/app.py` |
| WebSocket endpoint, dialplan callback | inside `voice` | `api/telephony/router.py` |
| Media protocol of Asterisk | | `api/telephony/adapters/asterisk.py` |
| Call session, provider neutral | | `api/telephony/session.py` |
| Ports for ear and mouth | | `api/telephony/speech.py` |
| Speech engines and their fakes | | `api/telephony/adapters/stt_<engine>.py`, `tts_<engine>.py`, `fake_speech.py` |

**Reused unchanged:** `api/agent/`, `api/domain/`, `api/events/`, `api/gui/`, `sim/`, `evals/`,
`api/telephony/adapters/fake.py`. `CallHandler` stays the only `CallEvents` implementation and
`TelephonyPort` the only way the core makes the line do something. `api/tools/` leaves the call path
but stays as the tested contract.

**Own small pipeline, no pipeline framework.** Everything rule-critical is our code either way:
protecting the read-back, draining speech before a transfer, the keypad buffer, the outage path.
Whether a local end-of-turn detector is needed on top of the speech engine's own is a number from
T-10.3, not a choice made here.

---

## 3. Access

One build, two entries. Asterisk registers either at a router that acts as a telephone system or at
a SIP trunk; everything behind it is the same. Failure behaviour and transfer are tested per entry.

| | Test phase: the pilot's router as PBX | Operation: SIP trunk |
|---|---|---|
| Contract | none, one of the three existing numbers | a trunk with one number per restaurant |
| Which number | one that customers do not call. The AI telephone is never assigned to the main number in the test phase, so no real customer call can reach the workbench (E9) | the restaurant's main number |
| How calls arrive | the software is an internal IP telephone of the router | the restaurant forwards its number in its carrier's network, or ports it |
| Caller number | delivered to an IP telephone | delivered |
| Concurrent AI calls | 2 at most: each IP-telephone call takes 2 of the router's 4 connections. The test phase runs with 1. | by tariff; D16 (up to 3) needs this entry |
| Reaching it | the workbench is not in the restaurant's network, so over a VPN to the router | registration from the server, TLS and SRTP |
| Transfer to the team | from an IP telephone only partly supported by the router; the open point of T-10.2 | a bridged leg to a team number that is never forwarded to us |
| When the software is off | the team telephones assigned to the same test number ring | the trunk's forwarding rules; the registration outlives a crash by up to its expiry |

A provider-supplied router without a registrar (as at Maxi's home) cannot serve as the test entry.
T-10.1 therefore has two steps: first on the workbench alone with a softphone, then against the
pilot's router.

---

## 4. Call session rules

One session per call. Events of one call are handled strictly one after another; the synchronous
core runs in a worker thread, and the port methods only enqueue.

| Topic | Rule |
|---|---|
| End of utterance | The speech engine's endpoint event closes a turn. Turns are sequential: no second turn starts while one is running. |
| Barge-in | Only on a sentence marked interruptible. A partial of two words or a key stops playback. Never once a transfer or hangup is queued. |
| Read-back (hard rule 3) | A read-back is not interruptible. An utterance counts as the answer only if it ends after the read-back was played to the end. A final transcript without speech energy is dropped, so line noise cannot become a yes. |
| Silence | A timer from the end of playback. Two re-prompts from code, then the farewell and hangup. |
| Keypad | Digits are collected until `#` or a gap of 3 s, then handed over as one answer. |
| Draining | Queued speech is played in order. A transfer or hangup waits for the end of the last sentence. |
| Ear or mouth fails | An engine error, or speech energy without a transcript, is a failure of the call: outage sentence and handover to the team (§5). |
| Call start | The first sentence answers the line. A transfer before any sentence (mode `paused` or `shadow`, limit reached, database down) leaves the call unanswered and rings the team. |

`CallEvents` gains what a platform used to hide: silence and a failure of the voice layer itself.
`TelephonyPort.say` gains whether a sentence may be interrupted.

---

## 5. Outage map (hard rule 5)

| What fails | Who notices | What rings the team |
|---|---|---|
| `voice` is down or refuses the media connection | the dialplan | the dialplan dials the team number |
| A crash in the middle of a call, or a transfer asked for by the core | the dialplan asks `voice` after the leg ends; only an explicit "done" hangs up | the dialplan dials the team number |
| Model, database, a tool | the existing paths in `agent/loop.py` and `telephony/handler.py` | `TelephonyPort.transfer` |
| The team does not pick up | the dial status | callback card for the team, the caller hears that nobody is reachable (T-10.8) |
| Asterisk or the whole machine is down | test phase: the router, the AI telephone is simply not registered. Operation: the trunk | test phase: the team telephones ring as before. Operation: the trunk's forwarding rules |

**Test number.** In the test phase every row of this table is tried on a number customers do not call, with a team telephone assigned to that number next to the AI telephone. The main number stays untouched until operation.

**Loop guard.** The team number must never be forwarded to us. `voice` refuses to start when the
team number is one of its own inbound numbers, the dialplan rejects a call from its own number, and
one line test proves it: software off, call the main number.

Everything in this table is the intended behaviour. T-10.2 turns each row into a measured one.

---

## 6. Ear and mouth

Two ports in `api/telephony/speech.py`, one fake each for tests, one adapter per engine. The key
terms an engine may be given (dish names, card numbers) come from the active menu in the database,
never from a list in code.

| Phase | Ear | Mouth | Cost |
|---|---|---|---|
| Test phase (D18) | an open speech-to-text model on the workbench | an open text-to-speech voice on the workbench | none |
| Operation | an EU speech-to-text service | an EU text-to-speech service | by use, §9 |

- The local engines are picked in T-10.3 by trying them on the workbench. Its graphics card is
  already filled by the local text model, so the ear may have to run on the CPU.
- Candidates for operation, first to be measured: the speech services of the vendor that is also the
  favourite for the text model (`docs/18_MODEL_SELECTION.md` §3), so one vendor and one data
  processing agreement would cover ear, model and mouth. Further candidates are named in the report
  of T-10.3.
- Numbers from local engines say little about operation. Latency and recognition for G1 are
  measured with the operation engines on the server.

**Thresholds (proposal, confirmed with the report of T-10.3):** median voice to voice at most 1.5 s
on turns with one tool hop; silent wrong card numbers at most 2 % on `evals/cases/nummern.jsonl`
spoken as audio.

**The measured comparison (T-10.10, attached to T-5.3).** Three variants on the same cases: the
chain; a model that hears the audio itself and answers as text; a speech-to-speech model in an EU
region. Reported per variant: the hard metrics of `docs/08_EVALS.md`, recognition of numbers and
dish names, latency, cost per call, and whether a read-back was spoken word for word. The two model
variants are harness code for the measurement, not product code.

---

## 7. Settings

In `Settings` (`api/config.py`), all from `.env`: the tenant the voice process serves (one tenant
per process is the known ceiling), a token for the media connection, engine choice and credentials
for ear and mouth, the silence timeout, and integer prices for speech-to-text, text-to-speech and
outbound minutes so a call's cost can be written to `calls.cost_cents`.

For Asterisk only, never in Python: the registrar's host, user and password, and the public media
address. The pilot's router and a later trunk provider are named in `.env`, never in the repository.

---

## 8. Concurrency (D16)

`domain/calls/routing.py` decides whether the AI answers from the mode and from the number of open
calls against a limit; a call above the limit rings the team through the silent transfer that
exists for `paused`. The limit and its weekly windows are a database value (migration, T-10.9).
Test phase: limit 1, because the router path allows 2 and the team needs its lines.

---

## 9. Cost model (assumptions, about 2,000 call minutes a month)

Budget approved by Maxi on 07.10.2026 (D17): 100 EUR a month and 300 EUR once for technology
(model, speech services, server and backup, telephone access). Each piece costs only from the day
it is switched on. Legal advice is a line of its own.

| Item | Test phase | Operation, a month |
|---|---|---|
| Server and backup storage | 0 (workbench) | about 18 EUR |
| Telephone access | 0 (the pilot's router) | about 2 to 6 EUR |
| Ear, model, mouth | 0 (local models) | about 20 to 60 USD |
| Transfer legs to the team | 0 | about 3 EUR to a landline |
| **Total** | **about 0** | **about 45 to 85 EUR** |

Not in the numbers: forwarded minutes on the restaurant's own tariff, the measurement runs of
T-10.3 and T-10.10 on paid services (a few euros), legal advice. If a speech-to-speech model wins
T-10.10, its line replaces the chain line at roughly 40 to 140 USD a month, which is a new budget
decision.

---

## 10. Work on the workbench

A work package that needs Maxi's machine carries the fixed note `Environment: Ubuntu (WSL) on the
workbench` in its row in `docs/07_WORKPACKAGES.md`, and `/task` checks it before it reads anything
else. How such a session is started, what a kit is and what Claude cannot do there:
`docs/12_CLAUDE_CODE_PLAYBOOKS.md`, playbook S10.

Known for T-10.1 (checked on the workbench on 07.10.2026): the distribution ships Asterisk 20.6,
too old for the media channel, so Asterisk runs as a container; `sudo` asks for a password, Docker
does not; WSL sits behind address translation, and whether call audio passes it is the first thing
the test shows.

---

## 11. Measurement log

Each spike adds its rows here: date, what was run, the result, go or no-go.

| Date | Package | What was measured | Result |
|---|---|---|---|
| | | nothing yet | |

---

## 12. Open questions

- **SIP trunk (T-10.12, deferred):** which provider; which forwarding types the restaurant's carrier
  offers in its network, whether the caller number is passed, what a forwarded minute costs, how
  long a no-answer forward waits; whether one number can be ported while the internet contract
  continues.
- **Router path:** whether the router lets an IP telephone hand a call to a team telephone; whether a
  VPN peer may register as an IP telephone; what a second and third caller hear.
- **Legal (D21, part of the check in `docs/09_OPERATIONS_LEGAL.md`):** whether live audio streamed
  to a speech service without storage falls under the rules for recordings (E11). In the test phase
  no audio leaves the workbench.
- **Texts that still name the voice platform:** the headers of `prompts/tools_v1.md` and `prompts/tools_v2.md`, comments in `api/telephony/`, `docs/03`, `docs/04`, `docs/08` and `docs/10`. They change with the first build package; a change under `prompts/` needs an eval run.
- **Team reachability:** today `transfer_to_team` guesses from the opening hours whether somebody
  can answer; the real answer is the result of the transfer (T-10.8).

---

## 13. Ways out, in this order (each one Maxi's decision)

1. The router path fails (handing over to the team, VPN, the call limit): the SIP trunk is brought
   forward (T-10.12).
2. A no-go in T-10.1 to T-10.3 that the trunk does not cure either: a hosted voice platform, with
   our conversation core behind it. The comparison of 07.10.2026 and its first candidate are kept
   outside the repository.
3. Eight weeks after T-10.4 starts without a passed outage drill and 20 role-play calls: the same.

**Not part of this layer:** software or hardware in the restaurant, recording, a multi-agent setup,
local models in operation, and the two model variants of §6 as product code.
