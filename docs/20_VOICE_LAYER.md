# 20 – Own Voice Layer

> Version 1.1 · 14.10.2026 · Decisions by Maxi in §1. Built: the call session (T-10.4, §4) and the
> AI telephone up to the speech engines (T-1.11, §2 and §5); what was measured is in §11. Every
> other product property and price below comes from web research of 07.10.2026 and is an
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

| Piece | Runs as | Place |
|---|---|---|
| Registration, dialplan, media | Compose service `asterisk` (22.11.0, pinned by digest; not older, §11), profile `voice`; built (T-1.11) | `deploy/asterisk/conf/`, rendered by `deploy/asterisk/render.sh` |
| Voice process | Compose service `voice`, the API's image with the speech engines (target `voice` of `api/Dockerfile`), one worker, profile `voice`; built (T-1.11) | `api/telephony/app.py` |
| WebSocket endpoint, dialplan callback | inside `voice`: `/media` and `/calls/{id}/ending`; built (T-1.11) | `api/telephony/router.py` |
| Media protocol of Asterisk and the line on it | built (T-1.11) | `api/telephony/adapters/asterisk.py` |
| Call session, provider neutral | built (T-10.4) | `api/telephony/session.py` |
| Ports for ear, mouth and the line | built (T-10.4) | `api/telephony/speech.py` |
| Speech engines and their fakes | built (T-10.5), inside `voice` since T-1.11 part 2 | `api/telephony/adapters/stt_<engine>.py`, `tts_<engine>.py`, `fake_speech.py` |

**Reused unchanged:** `api/agent/`, `api/domain/`, `api/events/`, `api/gui/`, `sim/`, `evals/`,
`api/telephony/adapters/fake.py`. `CallHandler` stays the only `CallEvents` implementation and
`TelephonyPort` the only way the core makes the line do something. `api/tools/` leaves the call path
but stays as the tested contract.

**Own small pipeline, no pipeline framework.** Everything rule-critical is our code either way:
protecting the read-back, draining speech before a transfer, the keypad buffer, the outage path.
Whether a local end-of-turn detector is needed on top of the speech engine's own was a number from
T-10.3: with the local engines it is, they report no end of an utterance (§6).

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
| The model is slow | Built in the core (#252, 13.10.2026): one guest turn has `service_config.turn_deadline_seconds` (10), then the outage sentence and the team. The clock starts when the core gets the sentence, so what the ear takes before that is not in it. Still to build here: a hold sentence ("Bitte warten Sie einen Moment") spoken while a turn runs; with it the restaurant's value goes to 15 (Maxi, 12.10.2026). |
| Model client | One per call from the `llm_factory` the session is built with. The call handler closes it when the call ends, whichever way it ends. |
| Call start | The first sentence answers the line. A transfer before any sentence (mode `paused` or `shadow`, limit reached, database down) leaves the call unanswered and rings the team. |

`CallEvents` gains what a platform used to hide: silence and a failure of the voice layer itself.
`TelephonyPort.say` gains whether a sentence may be interrupted.

**Built with T-10.4 (13.10.2026): `telephony/session.py`, `telephony/speech.py`, fakes in
`adapters/fake_speech.py`.** One worker and one queue per call: audio, keys, what the ear heard
and what the line finished playing are queued with the time they happened and handled strictly one
after another. The core runs in that worker. What it asks of the line (`Lines`, the
`TelephonyPort` over all sessions) is kept while its turn runs and carried out the moment the turn
returns, before anything that queued up behind it; timers fire on the time of the events, never
ahead of an event that waited in the queue, and an event's time and its place in the queue are
taken under one lock. Three ports: `Ear`, `Mouth` and
`Line`, the caller's leg (`answer`, `play` with a mark the line reports back when it was played,
`flush`, `transfer`, `hangup`); the media adapter of T-1.11 implements `Line`. How a session ended
is its `outcome`: only `hangup` lets the dialplan end the call, after `transfer`, `closed` or
`failed` it rings the team (§5). What the table left open, as built:

| Topic | As built |
|---|---|
| Which sentences may be interrupted | The handler decides per turn: a plain question of the agent may, and so may the two questions into a silent line. Never the AI disclosure at the start, anything said while a draft waits for its yes, the confirmation, the farewell, the outage sentence or a sentence before a transfer; and never a sentence the core counts as heard: one that asks for the guest's number, or one after which a dish may enter the order (it named the dish, or said back what was found). A guest who talked over it would be measured against words they never heard (Codex PR #286). A flush drops everything queued, so nothing is interrupted while one sentence that may not be is still to be played. |
| Barge-in by a finished utterance | Two words of a partial stop the sentence, as the table says. A finished utterance of one word ("Nein") stops it too: the agent takes the answer instead of talking over it. |
| Said while the core was working | An utterance that ended before the answer began to play is the next turn and does not stop that answer. If the answer is a read-back, the utterance is dropped like one spoken into it. |
| Keys and the read-back | Keys pressed into a sentence that may not be interrupted are dropped, like an utterance. |
| Keys without end | An entry is handed over at 20 digits whatever the gaps, so a key every two seconds cannot keep a call from ever reaching a turn. |
| Silence | The session reports it once per silence (`on_silence`); the handler asks "Sind Sie noch da?", then "Ich höre Sie leider nicht. Sind Sie noch dran?", then says the farewell and hangs up. **A yes to one of these questions confirms nothing** (hard rule 3): the question is now the last thing the guest heard, so the core reads the draft out again first. A guest who stays silent books nothing. |
| Speech without a transcript | Once is a cough; twice in a row is a failure of the ear and goes to the team. |
| The line does not report the end of playback | After the length of the audio plus 5 s it is a failure of the call and goes to the team. Whether the caller heard the sentence is not known then, so a read-back never counts as heard (Codex PR #286). A transfer that waited for the report happens all the same. A hangup that waited does not: the session ends as failed, so the dialplan rings the team instead of taking a farewell nobody may have heard for a finished call. |
| The mouth does not finish a sentence, or makes no audio | Each sentence has a deadline of 10 s; an engine that hangs is a failure like one that raises, and does not hold the one worker of the call (Codex PR #286). So is an engine that returns no audio without an error: queued as it is, the line would report a read-back as played that nobody heard. A sentence without words is not spoken at all and is no failure. A mouth that failed is not asked again on that call: the outage sentence is skipped, so the transfer does not wait a second deadline. The ear's `feed` must not wait for its engine at all: an adapter whose engine blocks runs it on a thread of its own. |
| The caller speaks, and no word is known yet | The ear reports it as a partial, with an empty text where the engine has no partial transcripts, at least once a second. Silence is counted from that, so a long order is not answered with "Sind Sie noch da?" in the middle of it. Speech without words stops no sentence. |
| When playback begins | When the audio is on the line, not when the sentence was asked for: what the caller said while the mouth was working is the next turn and no barge-in. The same holds for a partial and for a key: what is older than the answer on the line stops nothing. A partial counts by the time of its audio, not of its report, because the ear may lag and audio may have waited in the queue. |
| The line does not answer | The line must not block: every method of `Line` hands its command to the telephone system and returns. The session cannot bound a call to the line itself the way it bounds the mouth: a command it gave up waiting for could still be carried out later, a hangup while the dialplan is already ringing the team (Codex PR #286). An adapter whose transport can stall bounds and cancels its own commands and reports a lost leg as closed; that is a requirement on the media adapter of T-1.11. |
| A silent line and the tenant's call limit | A caller who never speaks is asked twice and let go by the silence rule, after three silence timeouts. The call limit (`max_call_seconds`) is checked where a turn begins, and its handover to the team is for a call on which somebody speaks; a line on which nobody does is ended, not handed over. With a limit shorter than three silence timeouts the silence rule ends the call a little after the limit. |
| A failure the core does not answer | If the core neither transfers nor hangs up after a failure, the session ends as failed instead of staying on a line it no longer listens to. |
| An answer that was already waiting | The guest says the order and "Ja" in one breath, and the worker is still on the first sentence. By then the core has "said" the read-back and counts it as heard, while the guest has not heard a word. So what the core asks for in a turn is carried out before the next queued event: the read-back protects itself first, and the waiting "Ja" is dropped like one spoken into it (Codex PR #286). |
| The worker is late | A key, an answer or a report from the line that arrived in time counts as in time, however late it is looked at: timers run up to the time of the next queued event, not to the clock. Two keys a second apart stay one answer after a slow turn, and a report that waited is no lost report. |
| The ear begins an utterance and never ends it | A partial says that the caller is speaking; the finished utterance has to follow within 10 s of the last partial, and within 60 s of the first, or it is a failure of the ear and the call goes to the team. The second limit is for a detector stuck in "speech": the tenant's call limit is checked when a turn begins, and without an end of the utterance no turn would. From the first partial to the final no silence is counted, also when playback ends in between. A final that is dropped (noise, nothing understood, said into a read-back) starts the silence anew. So a call that is open always waits for something with a deadline: playback and its report, an utterance and its end, a key and the gap, or the silence (Codex PR #286). |
| A hangup on a call whose voice failed | Never carried out, whoever asked for it and when: if the farewell cannot be spoken, the hangup behind it would drop the caller. The core's transfer takes the call, or the dialplan. |
| A transfer and a hangup both asked for | The first one stands, except that a transfer replaces a hangup that still waits for its sentence: a failure during the farewell goes to the team. |
| The session itself breaks | Nothing is hung up; the core logs the call as a failure, and the dialplan rings the team. |

Not in the session, by design: the end of an utterance. That is the ear's (`Heard(final=True)`),
from an engine's own endpoint or from the silence detector of its adapter; the orders run of
13.10.2026 says that silence must not be shorter than 700 ms (§11), and the adapter of T-10.5 waits
for 704 ms (§6). The silence timeout and the sample rate are arguments of the session; the timeout
is `VOICE_SILENCE_SECONDS` (§7).

**Built with T-1.11 (14.10.2026): the line on Asterisk's media channel** (`MediaLine` in
`adapters/asterisk.py`, one per connection). What it does was seen on the wire first, with a
capture endpoint against Asterisk 22.10.1 and again 22.11.0 (frames, recorded on 22.11.0, in
`api/tests/fixtures/asterisk_media_*.jsonl`, §11):

| Topic | As built |
|---|---|
| Which call a connection is | The dialplan sets `__VOICE_CALL` (the caller's channel id) and `__VOICE_CALLER`; the media channel inherits them and `MEDIA_START` reports them. `voice` makes the session only then, and refuses a connection that names no call or a call that is live already. Not URI parameters of `v()`: Asterisk 22 can hand them to another call's connection (issue #2194, no fix; on 22.11.0 a later call without `v()` carried an earlier call's parameters, 10 of 10), and Asterisk logs the dial string with them when `voice` cannot be reached, the caller's number included (Codex on PR #297). In 100 calls placed ten at a time each connection got its own call. |
| Answering | Dialled with `n`: the channel is not answered until `ANSWER`, which the first sentence sends. A call the core hands to the team before a word is never picked up by us; the caller hears ringing until the team answers (recorded: `NOANSWER`, the caller's channel still ringing). |
| When a sentence was heard | Each sentence is followed by `MARK_MEDIA`; `MEDIA_MARK_PROCESSED` for it is the session's `played`. `QUEUE_DRAINED` is not used: a "drained" for one sentence can arrive after the next was queued, and a read-back would count as heard before it played. |
| A full queue | Asterisk keeps about 1000 frames (20 s). Recorded: 25 s sent at once, the queue full at 1020 frames, the rest dropped without a word and the mark behind it reported at 20.5 s. So the line keeps at most three messages of audio (about 6 s) unplayed at Asterisk, each acknowledged by a mark of its own, and holds the rest back; the mark of a sentence goes out after its last audio. |
| Barge-in | `FLUSH_MEDIA`; recorded: marks queued before it are never reported. What the line held back is dropped with it. |
| Transfer and hangup | The media channel has no transfer. The line records the ending for the dialplan ("hangup" or "transfer <number>", the number checked for digits) before it sends `HANGUP`; `Dial` goes on (`g`) and the dialplan asks `/calls/{id}/ending`. Only "hangup" ends the call. |
| A connection that stalls | A frame Asterisk does not take within 2 s closes the connection and tells the session at once (the requirement of the row "The line does not answer" above): a failure of the voice layer (`broken`); a send into a connection Asterisk already closed is a caller who hung up. Without an ending the dialplan rings the team. |
| What Asterisk logs | The call's id and the kind of ending ("hangup", "transfer"), never a number: neither the caller's nor the team's, which can be somebody's own mobile (Codex on PR #297). |
| A frame the line does not understand | A failure of the voice layer (`CallSession.broken`), not a caller who hung up: the call log says so, and without an ending the dialplan rings the team. |
| Access | `/media` and `/calls/{id}/ending` want the media token as the password of Basic credentials, which is how Asterisk sends `username` and `password` of `websocket_client.conf` (recorded). A refused connection fails `Dial`, and the team rings. |

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

**Built and drilled on the workbench (T-1.11, 14.10.2026).** The dialplan of
`deploy/asterisk/conf/extensions.conf` dials `voice` with 10 s to answer and a backstop of 3600 s
on the call; a `voice` it did not reach is not asked for an ending; the team is dialled at the
number of the ending, else at `TEAM_PHONE` of `.env`. The loop guard is built in two of its three
parts: `voice` refuses to start when the team number (in the database or in `.env`) is
`VOICE_NUMBER`, `asterisk` refuses to start when `TEAM_PHONE` is that number (else every call
the dialplan sends to the team would come back and be dropped), and the dialplan refuses a call
from that number and never dials it as the team, in the forms +49..., 0049... and 0....
`asterisk` also refuses a `TEAM_PHONE` it could not dial and a `VOICE_NUMBER` that is not a German
number in E.164, so an outage never ends at a number that does not exist.
The line test with the software off is T-10.2's, at the router. `python3 -m scripts.voice_drill`
drills it against a stand-in for the router (§10); times are from placing the call (the kill for a
crash) to the team telephone ringing, on the kit's monotonic clock, which polls the logs every
0.25 s:

| Drill | Result on 14.10.2026 |
|---|---|
| `voice` killed in the middle of a call | the team rang after 0.4 s |
| Mode `paused` (the core sends the call on before a word) | the team rang after 0.2 s; the AI never answered |
| `voice` stopped | the team rang after 5.2 to 5.5 s, almost all of it the name lookup of a stopped container in WSL (§12) |
| `voice` frozen (its process paused: it takes the connection and answers nothing) | the team rang after 4.4 s, twice the connection timeout for the handshake. On Asterisk 22.10.1 it never rang: the handshake read had no timeout (issue #1979, fixed in 22.11.0), so the pin is 22.11.0 |
| `voice` freezes in the middle of a call | the team rang 8.2 and 9.6 s after the freeze in two runs: two pings of 2 s without a pong close our leg (`enable_pingpongs`, 22.11.0 and later), then the dialplan's question to the frozen `voice` fails after 2 s. Without the pings the team had not rung after 40 s; only the backstop of an hour would have ended the call |
| Database stopped | the outage sentence, the team rang after 8.3 s (the same lookup, for `db`) |
| A turn of the core with the model down | the core hands over: the team rings, or, outside the opening hours by its guess, a callback card and goodbye (T-10.8) |
| A silent caller | answered, asked twice, goodbye and our hangup after 6.5 s (2 s silence in the drill); the team does not ring |
| A call from our own number | refused, nobody answers, the team does not ring |

---

## 6. Ear and mouth

Two ports in `api/telephony/speech.py`, one fake each for tests, one adapter per engine. The key
terms an engine may be given (dish names, card numbers) come from the active menu in the database,
never from a list in code.

| Phase | Ear | Mouth | Cost |
|---|---|---|---|
| Test phase (D18) | an open speech-to-text model on the workbench | an open text-to-speech voice on the workbench | none |
| Operation | an EU speech-to-text service | an EU text-to-speech service | by use, §9 |

- **The local engines, measured in T-10.3 on 12.10.2026 (§11, handover in §10).** Ear: Parakeet
  TDT 0.6b v3 through ONNX, on the CPU, 0.15 s for a sentence; it leaves the graphics card to the
  text model, where Whisper large-v3-turbo would take 2.3 GB of the 16 that the Windows desktop
  (about 5) and the text model (about 9) already fill. Mouth: the Piper voice `thorsten`, 0.08 s
  (medium) or 0.27 s (high) to the first audio; which of the two, and whether the voice is
  acceptable at all, is for Maxi's ear (D4, samples in the run folder). This is a recommendation,
  not a clean go: the ear is at the limit for silent wrong numbers, see the thresholds below.
- **End of an utterance.** Neither ear reports one, each transcribes a finished piece of audio. So
  the call session needs a local silence detector (the one that ships with the Whisper package was
  used): at 500 ms of silence it kept 98 to 99 % of the sentences in one piece, cut 0.5 % in two and
  found no speech in 0.6 to 1.5 % (very short sentences of the weakest voices). Synthetic voices do
  not hesitate, so whether silence alone is enough for real callers is measured with recorded
  voices (T-10.7). §4 "the speech engine's endpoint event closes a turn" holds for an engine that
  has one; with the local engines the detector is that event.
- **Key terms.** Handing the dish names of the menu to Whisper raised the dish names heard letter by
  letter from 37 to 81 %. The picked ear takes no key terms in the kit, and heard 18 to 27 %; the
  name search forgives more than letters, so what this costs is a number for T-10.5 and T-10.7.
- Candidates for operation, first to be measured: the speech services of the vendor that is also the
  favourite for the text model (`docs/18_MODEL_SELECTION.md` §3), so one vendor and one data
  processing agreement would cover ear, model and mouth. Further candidates are named in the report
  of T-10.3.
- Numbers from local engines say little about operation. Latency and recognition for G1 are
  measured with the operation engines on the server.

**Built with T-10.5 (14.10.2026): `adapters/stt_whisper.py`, `adapters/tts_piper.py`, key
terms through `Ear.expect`.** Started on the local pair of T-10.3 before that task is closed (Maxi,
14.10.2026).

- **Ear: Whisper large-v3-turbo on the graphics card**, the ear for orders by the run of
  13.10.2026 (§11), behind `SegmentingEar`. Whisper only transcribes, so the adapter adds what the
  session needs (`speech.Heard`): the silence detector that ships with faster-whisper judges the
  caller's audio in windows of 32 ms on a thread of the ear; while there is speech it reports a
  partial with an empty text every 0.5 s, and after 704 ms of silence (22 windows) it hands the
  utterance, with 288 ms of audio before and after it, to Whisper and reports the final. Less than
  256 ms of speech is reported as no speech and never transcribed, so a click is no word and no
  yes; the windows of speech are counted, not the span from the first to the last, so two clicks
  with a pause between are no speech either (Codex PR #292, P1). Durations are counted in audio, not in arrival times; `ended_at` is when the audio of the
  last window of speech began, by the arrival of the frames it came in, less the audio behind it
  in each: the speech ended then or later, so a "Ja" stamped after the end of a read-back surely
  ended after it, also when part of its window waited for a frame that came late (Codex PR #292,
  P1 twice). When the ear is closed, what is still queued is dropped, not worked through. An error of detector or engine is raised by the next `feed`, which the
  session takes as a failure of the ear; an engine that hangs is caught by the session's deadline
  for the final (§4). The detector keeps its state from window to window: judged window by window
  without it, its probabilities differ from those of the whole recording by up to 0.9, with it by
  0.0 (checked 14.10.2026), so the adapter carries it, which reaches into the package that
  `deploy/speech/` pins.
- **Mouth: Piper `thorsten-medium`**, converted from the voice's 22,050 Hz to the line's 16 kHz
  piece by piece. Which voice stays Maxi's to hear (D4).
- **Key terms per turn, never the menu.** After each turn the handler hands the engine the names
  of the dishes the agent named in what it just said (the guest answers "die 47 oder die 48?"
  next), then those of the order, which a correction names (`handler.key_terms`). The names come
  from the searches of the call, so from the active menu in the database, as the guest says them
  (without "(4 Stück)"). Whisper keeps what fits half its prompt. Terms that would hold every dish of the active menu
  (a menu of one dish, or of two both in question) are not handed over at all (Codex PR #292). The
  agent's sentence is compared with the names as a guest says them. The two questions into a
  silent line set the terms as well, so a "Ja" to "Sind Sie noch da?" is not heard with the names
  of the dishes asked about before. A first version took only the
  dishes that became known in the turn and gave a repeated question no terms; the dishes named in
  the sentence are what the guest answers. Nothing is handed at the first mention of a dish: no
  window of a few names can be chosen before the guest says one.
- **Measured on the workbench, warm, graphics card (14.10.2026, synthetic voice through the
  modelled line):** Whisper 0.28 s median per utterance (max 2.2 s of 12), detector 0.08 ms per
  window, Piper 0.14 s to the first piece; a read-back of 12.5 s of audio takes 0.17 s to its
  first piece and 0.76 s whole, and the session plays a sentence only when it is whole (open point
  in `docs/01_STATUS.md`). After the caller stops speaking: 0.7 s of silence plus about 0.3 s of
  the ear before the core has the turn. The engines are tested in the image of `deploy/speech/`
  (`api/tests/test_telephony_speech_engines.py`, skipped elsewhere), the segmenting and the
  mouth's conversion against stand-ins in CI.
- **Not built here:** the settings (engine, model folder, device, voice file) and the image of the
  service `voice` come with T-1.11, which builds the adapters from them; partial transcripts with
  words, which a barge-in of two words needs (with this ear a sentence that may be interrupted
  stops at the end of the caller's utterance); Whisper on the CPU, which took 3.9 s for a sentence
  in T-10.3 and would miss the session's 10 s for a long order.

**Thresholds (proposal; T-10.3 reported on 12.10.2026, the confirmation is Maxi's):** median voice
to voice at most 1.5 s on turns with one tool hop; silent wrong card numbers at most 2 % on
`evals/cases/nummern.jsonl` spoken as audio.

- **Silent wrong numbers: at the limit, not safely under it.** The picked ear took a number the
  guest had not said in 1.5 % (run 1) and 2.1 % (run 2) of 724 number utterances; the Whisper ears
  in 4.6 to 9.0 %. Counted only where the number is a dish of the eval menu (so the guest gets
  another dish read back, instead of "that number does not exist"): 1.1 and 0.8 %. Plain numbers
  ("die 23") were safe: 0 and 1 of 148. Almost all of it is one kind of sentence, a number with a
  letter: "S12" is heard as "ist zwölf" and taken as dish 12, "23g" as "23 D" (9 to 11 % of 88).
  That is a gap in the number rule as much as in the ear (§12). **Closed for the prefix
  on 12.10.2026:** "ist" directly before a number now keeps the S number as a second
  candidate, and a number that went through it is never taken without a question: the
  search asks which of the two where the menu has both (eval `abholung_0077`) and offers
  the one it has otherwise. The same transcripts judged again: the picked ear takes 8
  instead of 12 of 724 (1.1 %), all ears 142 instead of 174 of 2,929; 32 utterances move
  from a wrong dish to a question back and none becomes wrong. The price: the plain sentence
  "die Nummer ist 23" now gets a confirming question (15 utterances of the run that
  were taken directly before). What is left is in §12. **Closed for the trailing letter
  heard as a word on 13.10.2026** (eval `abholung_0080`): a word the ear writes for a
  letter behind the number ("26 der", "30 ja", "27 die") keeps the lettered number as a
  second candidate where the menu has it, and the search asks. The run on the real menu
  judged again is the row of 13.10.2026 in §11.
- **Voice to voice: ear and mouth are not what decides it.** Together they take 0.24 to 0.41 s. The
  silence that ends a turn (0.5 s) and the text model come on top; the local text model alone needs
  about 1.1 s per request (docs/18 §5) and a turn with a tool hop is two requests. So 1.5 s is out of
  reach on the workbench whatever ear and mouth do, and is a number for the server (G1).

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

Built with T-1.11: the tenant is `TENANT_NAME` (exactly one of that name in the database);
`VOICE_MEDIA_TOKEN` (at least 32 characters outside dev and test); `VOICE_NUMBER`, the AI
telephone's own number; `VOICE_EAR` and `VOICE_MOUTH` (`whisper` and `piper`, the engines of T-10.5, or `fake`, the drill
pair, refused outside dev and test); `VOICE_EAR_DEVICE` (`cuda` or `cpu`); `VOICE_MOUTH_VOICE`
(the Piper voice, D4); `VOICE_SILENCE_SECONDS` (8, an assumption). `voice` does not
start without any of them, without a model (`LLM_*`), or with the team on its own number. The
prices come with T-10.6.

Built with T-1.11 part 2: the engines' packages are in the target `voice` of `api/Dockerfile`
(`api/requirements-voice.txt`, hashed, about 3 GB more than the API's image, most of it CUDA);
their model files are the speech kit's, mounted read-only from `deploy/speech/run/models` (a
worktree hard-links them from the main checkout, `docker-compose.yml`). Whisper asks for the
graphics card; a host without one needs `deploy: !reset {}` for `voice` and
`VOICE_EAR_DEVICE=cpu`, as `deploy/docker-compose.prod.yml` has for the EU server, where ear and
mouth are services (§6). `voice` does not start with an engine it cannot load or run: each loader
runs its engine once at start (the ear 0.4 s, then 0.1 s a run, 15.10.2026). A watchdog ends
the process when one engine call works longer than 30 s (exit 70), and Compose starts it again:
a thread cannot be cancelled, and a hung inference would hold the model every later call needs.
Each engine takes one call at a time, and the wait for it does not count. With the ear on the
processor the limit is 600 s: it takes about its audio's length for a sentence there, and up to
six passes when it is unsure.
The phone line's database sessions end a statement after 2 s (`db.bounded_sessions`), so a tool
that waits on a lock sends the call to the team instead of keeping the guest waiting. The call
log is closed when the line reports the end of the leg, with the session's outcome: after the
goodbye was played, and as `error` when the line fails on it; a hangup the line fails to carry
out after the goodbye is still a hangup. That last write waits up to 30 s on a lock instead of
the 2 s of a turn: nobody waits on it, and a lock held for a moment must not leave the call
without an end; a lock past that leaves it open, as a crash does (docs/01, open points). A call
whose leg ends while it is still being opened is closed once it is open, with no greeting. The
call log gets the model and its cost, and the log the line "model usage", as on the text phone.

For Asterisk only, never in Python: the registrar's host, user and password (`ACCESS_HOST`,
`ACCESS_USER`, `ACCESS_PASSWORD`), and the public media address (not yet, it comes with the trunk).
The pilot's router and a later trunk provider are named in `.env`, never in the repository.
`deploy/asterisk/render.sh` puts them, and the token, into the configuration on a tmpfs inside the
container (a ";" escaped, which would start a comment there) and stops the start when one is
missing or holds a line break. Asterisk runs without verbose output: it would
print every dialplan step with its values, the token included (seen in the drill, §11).

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

### Handover: T-10.1 step 1, the media channel on the workbench (run 07.10. and 11.10.2026)

| | |
|---|---|
| Start folder | the repository folder in the Ubuntu home directory (or a worktree of it), Ubuntu terminal |
| The one command | `python3 -m scripts.voice_spike` (about 40 s; `--dry-run` prints the steps, `--keep` leaves the containers running, `--down` stops them again) |
| Maxi has to have ready | Docker Desktop running with the WSL integration. No account, no `.env` entry, no sudo |
| What the result looks like | one line `pass` or `FAIL` per point, "28 of 28 points passed", exit code 0; `deploy/asterisk/run/report.json` holds the same list, `pbx.log` next to it the output of both telephone containers |

What it starts (`deploy/asterisk/docker-compose.spike.yml`, one Compose project per run folder, so
two worktrees do not share containers; own network, no host port published): a test PBX (Asterisk 22.10.1, image pinned by digest), a scripted softphone (a
second Asterisk that dials in over SIP with G.711 A-law, plays a tone, presses keys and records what
it hears) and the probe (`api/telephony/probe.py`), which stands where the service `voice` will
stand. The softphone's password is made new on every run and exists only in `deploy/asterisk/run/`,
which git ignores. Only invented numbers and test tones. The kit refuses to run as root, because the
containers take the caller's uid, and a run first stops what an earlier run with `--keep` left
behind. It clears folders only in a run folder it made itself (marked `.voice-spike-run`) or in an
empty one, and refuses any other. The kit itself is `api/telephony/spike.py`; `scripts/voice_spike.py` is its entry.

What step 1 does not answer: a call from a softphone on Windows through WSL's address translation
was not tried. Step 2 answers the direction that matters, because there Asterisk registers outwards
at the pilot's router over a VPN; nothing in the plan needs a telephone that reaches into WSL.

What the media adapter (T-1.11) and the outage spike (T-10.2) can build on, all seen in the run:

- The dial string `WebSocket/<connection>/c(slin16)f(json)v(...)` gives 16 kHz signed linear in
  both directions and JSON control frames; Asterisk resamples from the 8 kHz of the telephone line.
  The dialed extension and the caller number arrive as URI parameters of the WebSocket request
  (the spike's way; T-1.11 names a call by inherited variables instead, §4).
- Audio from the caller comes as one binary frame of 640 bytes every 20 ms; the last frame of a
  playback on the far side can be shorter (552 bytes seen), so the ear must not assume a size.
- Audio to the caller can be sent as fast as it is produced, in messages of whole frames up to
  65,500 bytes. Asterisk paces it. `REPORT_QUEUE_DRAINED` answers when the last frame was played
  (the moment a read-back is over), `FLUSH_MEDIA` drops what is queued (the caller speaks over a
  sentence), `HANGUP` after `QUEUE_DRAINED` ends the call only when everything was spoken.
- `connection_id` in `MEDIA_START` is the name of the connection in `websocket_client.conf`, the
  same for every call. The call is named by `channel_id`.
- With the voice service not reachable, `Dial` gives up after the connection timeout (2 s set, one
  try) and the dialplan goes on to its next line. That line is where the outage fallback of T-10.2
  belongs.
- Asterisk times its waits by the wall clock, and a clock that is set during a call cuts them
  short. Seen on 11.10. (the softphone pressed its key 0.8 s into the call instead of 2 s) and on
  12.10.2026 (a softphone that should wait 2 s hung up after 0.35 s by the probe's monotonic clock,
  while its own log stamps show 3 s). Measured on 12.10.: after the machine had slept, the wall
  clock in WSL and in the containers was set forward by about 3.3 s every 30 s. The kit compares
  both clocks around every call and places a call again when the wall clock was set during it (four
  attempts, then the run breaks instead of judging such a call); the
  judgement itself uses the probe's monotonic clock and the samples in the recording. For operation
  it means the server's clock has to be adjusted gradually, not in steps.

### Handover: T-1.11, outage drills of the AI telephone (run 14.10.2026)

| | |
|---|---|
| Start folder | the repository folder in the Ubuntu home directory (or a worktree of it), Ubuntu terminal |
| The one command | `python3 -m scripts.voice_drill` (about 2 minutes, the first run longer for the image; `--dry-run` prints the steps, `--keep` leaves the containers running, `--down` stops them again) |
| Maxi has to have ready | Docker Desktop running with the WSL integration. No account, no `.env` entry, no sudo |
| What the result looks like | one line `pass` or `FAIL` per point, "15 of 15 points passed", exit code 0; `deploy/asterisk/drill-run/report.json` holds the same list, `drill.log` next to it the output of the containers |

What it starts (`deploy/asterisk/docker-compose.drill.yml`, one Compose project per run folder, own
network, no host port): the service `asterisk` of `docker-compose.yml` as it is, so the drills
exercise the product's configuration; the service `voice` with the drill engines, a model address
that does not exist and a throwaway database, migrated and seeded; and a stand-in for the router
(`deploy/asterisk/drill/`), a second Asterisk that is the registrar the AI telephone registers at,
the caller who rings it and the team telephone. Token and password are made new on every run; the
kit checks that neither shows up in any container's output. The kit is `api/telephony/drill.py`;
`scripts/voice_drill.py` is its entry.

What the drills do not answer: anything about the pilot's router (T-10.1 step 2, T-10.2), speech
(the drill engines hear nothing), and what the team sees as the caller's number on a handed-over
call (§12).

### Handover: T-10.3, ear and mouth on the workbench (run 12.10.2026)

| | |
|---|---|
| Start folder | the repository folder in the Ubuntu home directory (or a worktree of it), Ubuntu terminal |
| The one command | `python3 -m scripts.speech_spike` (`--dry-run` prints the steps, `--limit 6` is a short trial of the kit, not a measurement; a full run takes about 20 minutes) |
| Maxi has to have ready | Docker Desktop running with the WSL integration, about 14 GB of disk (image 4.8 GB, model files about 9 GB, both kept for the next run). No account, no `.env` entry, no sudo. The first run downloads the model files |
| What the result looks like | one table for the ears, one for the mouths, one for the silence detector, then the pick, or `no-go` with the ear that came closest; exit code 0 with a pick, 1 without. An ear that returned an empty transcript for every number sentence (a dish name it heard does not count) is listed as failed ("heard nothing") and is never the pick: an empty transcript asks back and is never wrong, so it would pass the limit for silent wrong numbers. An ear that heard a few number sentences is judged as usual, the empty ones count as asked back. `deploy/speech/run/report.json` holds the same, `judged.json` every sentence with what each ear heard, `samples/` one sound file per voice and agent sentence, as a caller would hear it |

What it runs (`deploy/speech/Dockerfile`, one image: the open speech engines next to the project's
packages; three containers one after another, no host port): the first downloads the model files
and is the only one with network access; the second speaks, hears and times
(`api/telephony/speech_bench.py`); the third judges (`evals/heard.py`). The last two run with
`--network none`, so no audio and no sentence leaves the machine. The sentences are the number eval
(`evals/cases/nummern.jsonl`) and the dish names of the eval menu; the key terms an ear can be
given are those dish names, read from the menu file. Which engines are tried is the table at the
top of `api/telephony/speech_spike.py`.

**On another menu than the eval menu (12.10.2026).** The evals and CI stay on the invented eval menu. To hear what the ears make of a real one, convert the register copies into the folder `imports/`, which git ignores, and hand the kit the file:

```bash
# Ubuntu terminal, repository folder (it needs the `.env` every Compose command needs).
# REGISTER_DIR is the folder with the register copies; set it to yours. It is mounted read-only.
# Exit 1 with the allergen errors is expected.
REGISTER_DIR="$HOME/register"
mkdir -p imports
docker compose run --rm --no-deps --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$REGISTER_DIR:/register:ro" -v "$PWD/imports:/app/imports" \
  api python -m scripts.kasse_to_csv /register --out imports
python3 -m scripts.speech_spike --menu imports/menu_items.csv   # about 45 minutes for 173 dishes
python3 -m scripts.speech_spike --judge-only                    # after a change to the number rule
```

**Spoken orders, a run of its own (13.10.2026).** `--orders N` speaks N orders per size instead of the other sentences: several dishes of the menu by their card numbers in one sentence ("Zweimal die 13, die 26d und einmal die S12."), drawn with a fixed seed, so every run speaks the same orders. `--order-sizes` sets how many dishes an order has (default `2,3,4`, up to 12). Each order gets one verdict (`evals/heard.py`): the transcript is cut into positions as the conversation cuts a sentence, each position goes through the number rule, and the order is **right** with every dish and quantity as said, **wrong** when a dish or quantity is taken that was not said or a dish is missing without a question, **asked** otherwise. The report counts per size, says how many of the dishes said arrived, and how many orders the rule gets right from the sentence as text, which no ear can beat. It is slow and never part of CI:

```bash
# Ubuntu terminal, repository folder. About 50 minutes for 180 orders of 2 to 12 dishes.
python3 -m scripts.speech_spike --menu imports/menu_items.csv --orders 30 --order-sizes 2,3,4,6,9,12
```

With `--menu` the kit speaks the dish names of that file (without the number of pieces at the end, any other bracket stays), reads the file once, keeps a copy of it in the run folder and checks that copy in the image with the import's own rules before anything is spoken (`importer.parse`: a card number the search cannot take, a missing price, the same number twice) and ends the run on an error, every one of its card numbers once ("Nummer S12") and the sentences of the number eval, hands the ear a window of `KEY_TERMS` (10) names as key terms with each utterance, a dish with its own name among them and never the whole menu (hard rule 6: the full menu goes into no model context, and an ear's prompt is one; a menu of one dish gets no key term). The window is cut to what the ear's prompt takes before the ear hears, and the report counts what each utterance really got. The judge reads from the plan which letters a number can carry and what is on the card. The expectations of the number eval were written for the eval menu, so with another menu a number sentence is right when the transcript resolves like the sentence that was said. `--judge-only` speaks and hears nothing: it removes the old verdict and judges the recorded run in the run folder again with the number rule as it is now, against the menu the run was recorded with (every plan carries its menu). **A real menu stays out of the repository:** its names are in `imports/` and in the run folder only (a copy of the file, plan, results, the list of wrong numbers), both ignored by git, and the log below carries counts, no names. With `--menu` the kit refuses a `--run-dir` inside the repository other than its own, also where its own is a link to another folder of the repository.

What the numbers are and are not:

- **The callers are synthetic.** Every sentence is spoken by the mouth candidates, so a wrong
  transcript is the ear's fault or the voice's, and the two cannot be told apart from the table. A
  voice that mispronounces a dish name costs every ear the same.
- **The line is a model**: the band of a telephone (300 to 3,400 Hz), 8 kHz A-law, back to 16 kHz.
  No noise, no lost packets, no handset.
- **A dish name counts only letter by letter.** The name search forgives more, so the dish column
  is a lower bound and mostly shows what key terms change.
- **The voices differ from run to run** (the mouth adds a little randomness to every sentence), so
  single sentences flip between runs and the shares move by a point or two.
- **The text model is not in the measurement.** The kit reports what ear and mouth take of the
  1.5 s from voice to voice and what is left for the silence that ends a turn and for the model.

---

## 11. Measurement log

Each spike adds its rows here: date, what was run, the result, go or no-go.

| Date | Package | What was measured | Result |
|---|---|---|---|
| 07.10. and 11.10.2026 | T-10.1 step 1 | `python3 -m scripts.voice_spike` on the workbench (Ubuntu in WSL, Docker Desktop): Asterisk 22.10.1 as test PBX, a scripted softphone over SIP with A-law, the probe at the other end of the media channel (`slin16`, JSON control frames). Tones instead of speech, detected by frequency in the softphone's recording and in the probe. The whole kit run several times, same result | **Go for step 1, 28 of 28 points.** Audio to the caller: 4.00 s heard of 4 s sent. Audio from the caller: its tone detected, one 640-byte frame every 20 ms on average (19.96 to 20.00 over the runs), the longest gap between two frames 20 to 29 ms. Caller number: arrives, one invented number per call. Key events: every digit arrives as `DTMF_END`. Pacing: 4 s sent at once, queue empty after 4.01 s. Stopping playback: 15 s queued, the caller plays a 3 s tone and then presses a key, the caller heard our tone for 3.2 s and it ended about 20 ms after `FLUSH_MEDIA` (three runs: +21 ms each). Both are put on one clock through the start of the caller's own tone, which is in the softphone's recording and in the probe's log; the recording is counted in 20 ms windows, so the figure is exact to one or two frames. Queue length 0. Hangup after speech: the full 1 s heard, call over after 1.3 s. Three calls at once: on the line together for 5.2 s, each with its own tones, number and digit, and none of another call's tone in either direction. Voice service unreachable: `Dial` fails after one try and the dialplan continues. Not measured: speech, delay from mouth to ear, a softphone outside the Docker network, step 2 at the pilot's router |
| 12.10.2026 | T-10.3, local engines | `python3 -m scripts.speech_spike` on the workbench, twice. The 181 sentences of the number eval and the 31 dish names of the eval menu, spoken by four Piper voices, sent through a modelled telephone line (300 to 3,400 Hz, 8 kHz A-law), heard by five ears: 848 utterances per ear, 724 of them number sentences, judged by the rule that resolves numbers on the phone. No network while it measures. Right / asked back / silent wrong, run 1 and run 2: Parakeet v3 on the CPU 63.1 / 35.4 / **1.5 %** and 62.6 / 35.4 / **2.1 %**; Whisper large-v3-turbo on the graphics card 68.8 / 24.7 / 6.5 % and 68.2 / 25.1 / 6.6 %; the same with key terms 71.0 / 24.0 / 5.0 % and 69.8 / 25.7 / 4.6 %; Whisper small on the CPU 59.9 / 31.1 / 9.0 % and 61.3 / 31.8 / 6.9 %. Median per sentence: Parakeet 0.15 s, Whisper turbo on the card 0.11 s (2.3 GB of graphics memory) and on the CPU 3.9 s (a sample of 41, timing only), Whisper small 0.76 s. Dish names letter by letter: 37 % Whisper turbo, 81 % with key terms, 18 to 27 % Parakeet. Mouth, first audio: `thorsten-medium` 0.08 s, `thorsten-high` 0.27 s, the two `low` voices 0.07 s; the picked ear understood 65 to 68 % of the number sentences from `thorsten`, 58 to 61 % from the `low` voices. Silence detector at 300 / 500 / 700 ms: 96 to 98 / 98 to 99 / 98 to 99 % of the sentences in one piece | **Recommendation, no clean go.** Ear Parakeet v3 on the CPU, mouth Piper `thorsten`, a local silence detector at 500 ms. The limit of 2 % silent wrong numbers is met in one run and missed in the other; plain numbers are safe, numbers with a letter are not (§6, §12). Ear and mouth take 0.24 to 0.41 s of the 1.5 s. Not measured: real voices, a real line, the text model, the EU services for operation (they wait for accounts) |
| 12.10.2026 | T-10.3, the pilot's real menu | `python3 -m scripts.speech_spike --menu imports/menu_items.csv` on the workbench, one run. The 173 dishes of the register export (43 plain numbers, 37 with S, 6 with SM, 87 with a trailing letter a to g): every card number spoken once ("Nummer S12"), every dish name, and the 193 sentences of the number eval; 2,156 utterances per ear, 1,464 of them number sentences, judged against the 173 numbers with the number rule of PR #257 (`--judge-only` after its last change). Right / asked back / silent wrong over all number sentences: Parakeet v3 on the CPU 60.7 / 36.5 / **2.9 %** (2.3 % another dish of the menu); Whisper large-v3-turbo on the graphics card 71.9 / 22.3 / 5.8 %, with key terms 72.1 / 22.1 / 5.7 %; Whisper small on the CPU 62.9 / 29.4 / 7.7 %. The 692 utterances of real card numbers on the picked ear: 57.2 / 38.9 / **3.9 %** (27, of them 24 another dish). By form: plain 70.9 / 29.1 / 0 % of 172; S 33.8 / 66.2 / 0 % of 148 (59 of the questions come from the "ist" rule); SM 33.3 / 66.7 / 0 % of 24; trailing letter 62.1 / 30.2 / **7.8 %** of 348. All 27 silent wrong numbers are numbers with a trailing letter: in 17 the letter is lost and the plain number taken (d written as "der" 7 times, e as "die" or "äh", a as "ja"), in 6 it becomes another letter (g heard as D 4 times), 4 have other digits. By letter, wrong: d 17 %, e 15 %, g 14 %, b 7 %, f 4 %, c 2 %, a 1 %. By voice: `karlsson-low` 11.6 % (20 of the 27), `thorsten-medium` 2.3 %, `kerstin-low` 1.2 %, `thorsten-high` 0.6 %. Per dish: the number of 87 of the 173 got through from at least three of the four voices (32 of 43 plain, 49 of 87 with a letter, 6 of 37 S, none of 6 SM), of 21 from none; 26 dishes had at least one silent wrong number. Dish names letter by letter: Parakeet 18 %, Whisper turbo 26 %. Key terms: only 25 of the 173 names fit the ear's prompt (223 tokens; the whole list is 1,369), so the 44 % of the ear with key terms is a mix: the 25 dishes heard with their term arrive in 90 % (24 % for the same dishes without key terms), the other 148 in 36 %. In this run 11 of the 173 names were spoken without the remark in their bracket ("(vegan)", "(Bandnudeln)"): the kit cut every bracket then, since 12.10.2026 only the number of pieces ("(6 Stück)"). The dish-name shares hold for those shortened names; the number sentences are not touched by it. With the full names 23 fit the prompt instead of 25. Since 12.10.2026 the kit no longer hands over the list at all: every utterance gets a window of 10 names with its own dish among them, so a later run measures the key term for every dish. Median per sentence: Parakeet 0.21 s, Whisper turbo 0.12 s, Whisper small 0.84 s (the machine ran tests at the same time). Silence detector at 300 / 500 / 700 ms: 96.9 / 98.8 / 99.1 % in one piece | **No-go against the 2 % limit, and the picture changes:** on the real menu the open problem is not the prefix (S and SM: no silent wrong number, at the price of a question in two of three utterances) but the trailing letter, which half of the dishes carry. A plain number was never taken wrongly. One synthetic voice causes three quarters of the errors, so the size of it is a question for real voices (T-10.7); the rule for it is §12 |
| 13.10.2026 | T-10.3, the real-menu run judged again | `python3 -m scripts.speech_spike --judge-only` on the transcripts of 12.10.2026 with the rule for a trailing letter heard as a word (eval `abholung_0080`); nothing spoken or heard again. Right / asked back / silent wrong over all 1,464 number sentences: Parakeet v3 60.7 / 37.4 / **2.0 %** (29 instead of 42; 1.5 % another dish); Whisper turbo 71.9 / 23.0 / 5.1 %, with key terms 72.1 / 22.5 / 5.3 %; Whisper small 62.9 / 30.1 / 7.0 %. The 692 utterances of real card numbers on the picked ear: 57.2 / 40.8 / **2.0 %** (14 instead of 27, of them 12 another dish); trailing letter 62.1 / 33.9 / **4.0 %** of 348; plain, S and SM unchanged at 0 %. 13 utterances of the picked ear moved from a wrong dish to a question (d as "der" 7, "dann" 1; e as "die" 1, "äh" 1; a as "ja" 1; b as "war" 1; f as "ist" 1), 39 over all ears; none that was right before asks now, and the share of right answers is the same. The 14 that are left: the letter is gone with nothing behind the number (4), it became another letter (6, g heard as D 4 times), other digits (4, "30e" as "dreißig eins"); 9 of them from `karlsson-low` | **The kit's limit of 2 % is met by 0.02 points (29 of 1,464), on one run of synthetic voices: at the limit, not under it.** What a rule can see in the transcript is closed; the 14 left are nothing a rule can tell from a plain order, they need a better ear for letters (key terms per utterance, second run), the read-back with its correction (PR #271), and real voices (T-10.7) |
| 13.10.2026 | T-10.3, second run on the pilot's real menu | `python3 -m scripts.speech_spike --menu imports/menu_items.csv`, one run, with the rule of 13.10.2026, the full dish names and key terms per utterance (10 of the 173 names, the said one among them): 553 sentences (207 of the number eval, 173 card numbers, 173 names), 4 voices, 2,212 utterances per ear, 1,520 of them number sentences. Right / asked back / silent wrong over all number sentences: Parakeet v3 58.9 / 38.6 / **2.4 %** (37, 1.6 % another dish); Whisper turbo 71.8 / 23.2 / 4.9 %, with key terms 72.1 / 22.9 / 5.0 %; Whisper small 62.8 / 30.1 / 7.2 %. The 692 utterances of real card numbers: Parakeet 56.9 / 40.2 / **2.9 %** (20); Whisper turbo 74.0 / 21.1 / 4.9 %, with key terms 76.2 / 19.2 / 4.6 %; Whisper small 66.0 / 26.3 / 7.7 %. By form, Parakeet: plain 66.9 / 33.1 / 0 %, S and SM 34.3 / 65.1 / 0.6 %, trailing letter 63.2 / 31.3 / 5.5 %. Whisper turbo: plain 91.9 / 7.6 / 0.6 %, S and SM 47.7 / 48.3 / 4.1 % (55.8 / 39.0 / 5.2 % with key terms), trailing letter 78.2 / 14.4 / 7.5 %. **Parakeet's 20 silent wrong card numbers:** the letter gone with nothing behind the number 11, another letter 5 (g heard as D 4 times), other digits 4; 15 of the 20 from `karlsson-low`. None is a letter heard as a word: the rule of 13.10.2026 asked in 11 such utterances, the "ist" rule in 54. **Parakeet's questions are mostly no questions of a rule:** in 159 of the 692 card number utterances (23 %) the transcript holds no number at all, because the ear wrote the two words in another language or as noise ("Number four.", "Número cuanci.", "Mainz."); all 57 questions on plain numbers are of that kind. Whisper, which is told the language, has 24. Whisper's silent wrong S numbers are the dropped prefix ("Nummer s17" written "Nummer 17"). Dish names letter by letter: Whisper turbo 25.1 %, with key terms 80.9 %; Parakeet 17.6 %; Whisper small 12.6 % | **No-go: 2.4 % against the 2 % limit, and no ear is near 90 % right.** Between this run and the recording of 12.10.2026 the same ear moves by about one point (2.0 and 2.9 % on card numbers with the same rule), so one run of synthetic voices decides nothing at the limit. The two ears fail differently: Parakeet takes the fewest wrong numbers but loses a quarter of the short sentences to another language; Whisper turbo understands nine of ten plain numbers and four of five dish names with key terms, but drops a prefix or a letter without a trace more often. What is left on both is the letter the ear does not write at all, which no rule can see (§12) |
| 13.10.2026 | T-10.3, spoken orders from the pilot's real menu | `python3 -m scripts.speech_spike --menu imports/menu_items.csv --orders 30 --order-sizes 2,3,4,6,9,12`, one run: 180 orders (30 each of 2, 3, 4, 6, 9 and 12 dishes, 3.5 to 17 seconds of speech), 4 voices, 720 utterances and 4,320 dishes said per ear; judged per order with the number rule of PR #282 and the positions of PR #284. Read as text all 180 orders are right, so every loss below is the ear's. A sixth ear is new: Canary 1b v2, Parakeet's sibling that takes the language. Orders right / asked / silent wrong, and dishes that arrived: Whisper turbo on the graphics card 62.4 / 19.9 / **17.8 %**, 82.5 %; with key terms (dish names, which are no help for a number) 60.0 / 24.2 / 15.8 %, 79.0 %; Parakeet v3 47.9 / 34.0 / 18.1 %, 73.4 %; Canary v2 47.9 / 36.2 / 15.8 %, 73.7 %; Whisper small 49.2 / 25.7 / 25.1 %, 72.4 %. **By size**, Whisper turbo: 2 dishes 82 / 15 / 2 %, 3 dishes 72 / 21 / 8 %, 4 dishes 72 / 21 / 8 %, 6 dishes 59 / 18 / 22 %, 9 dishes 51 / 18 / 31 %, 12 dishes 38 / 26 / 36 %. Parakeet: 85 / 13 / 2, 63 / 28 / 8, 68 / 23 / 9, 36 / 44 / 20, 23 / 43 / 33, 12 / 52 / 36 %. Canary: 78 / 22 / 1, 68 / 26 / 6, 59 / 35 / 6, 43 / 42 / 14, 19 / 47 / 34, 20 / 46 / 34 %. **Per dish said, a dish taken that was not said:** Parakeet 4.9 %, Whisper turbo with key terms 5.5 %, Canary 6.2 %, Whisper turbo 7.0 %, Whisper small 10.9 %. **Why an order is wrong** (Whisper turbo, 128 orders): another letter or the letter gone 77, another number 35, the right dish with another quantity 15 (the ear moves the comma: "die 4, dreimal die 56" heard as "die 4 dreimal, die 56"), a dish missing without a question 1. **Why an order asks:** almost always one piece that is no card number any more ("die FENF" for the 5, "die L" for the 11, "dreißig Zi" for 30c); the sentence not cut into dishes at all in 10 to 15 of 720. By voice, Whisper turbo: `kerstin-low` 74 / 14 / 12 %, `thorsten-medium` 71 / 14 / 15 %, `thorsten-high` 59 / 18 / 23 %, `karlsson-low` 46 / 33 / 21 %. Median per order: Whisper turbo 0.18 s, Parakeet 0.31 s, Whisper small 1.07 s, Canary 1.22 s (p95 3.2 s). **Silence detector:** a silence of 300 ms cuts 44.9 % of the orders in two, 500 ms 10.8 %, 700 ms 2.9 % | **No-go, and far from it: no ear takes an order of several dishes by number without a wrong dish in more than one of six orders.** The share of a wrong dish per dish said (5 to 7 %) is what the single numbers showed; it adds up with every dish, so an order of two is wrong in 2 % and an order of twelve in a third. Three things follow. The ear for orders is Whisper turbo: it brings 82 % of the dishes through, Parakeet and Canary 73 %; holding the ear to German (Canary) helps the two-word sentences of the earlier runs and nothing here, at four times the time. The silence that ends a turn has to be 700 ms or the detector cuts every tenth order in two (§12). And the read-back is no net under the ear but the step that decides: at this rate most orders of four dishes and more need one correction (§12) |
| 14.10.2026 | T-1.11, the media channel as the adapter uses it | A capture endpoint in place of `voice` against Asterisk 22.10.1 and a scripted softphone on the workbench, dialled with `nc(slin16)f(json)`; one call per question, frames kept as fixtures (`asterisk_media_marks`, `_flush`, `_overflow`, `_unanswered`, `_closed`) | `MEDIA_START` arrives before the call is answered; `ANSWER` answers it. Marks: reported exactly when the audio before them was played (1.0 s and 0.5 s after it was sent). `FLUSH_MEDIA`: the two marks queued before it never reported, a mark after it reported. 25 s sent at once: queue full at 1020 frames, the mark behind it reported at 20.5 s, so 4.5 s were dropped without a word. `HANGUP` before `ANSWER`: `Dial` status `NOANSWER`, the caller's channel still ringing, the team leg answers it. Connection closed without `HANGUP`: status `ANSWER`, no ending, the team rang. `voice` unreachable: `CHANUNAVAIL` within the same second. `username` and `password` of `websocket_client.conf` arrive as Basic credentials |
| 14.10.2026 | T-1.11, outage drills | `python3 -m scripts.voice_drill` on the workbench, several full runs while building, the numbers from the run after the last change: the product's `asterisk` and `voice` against a stand-in for the router (§10) | **15 of 15 points on Asterisk 22.11.0.** Times to the team telephone in §5: kill 0.4 s, `paused` 0.2 s, `voice` stopped 5.2 to 5.5 s, `voice` frozen 4.4 s, `voice` frozen mid-call 8.2 and 9.6 s, database stopped 8.2 to 8.3 s. Both freezes failed before their fix: on 22.10.1 the team had not rung after 40 s, and mid-call without the pings neither. Findings fixed on the way: Asterisk's verbose output printed the media token with every call (it runs without it now, and the kit checks every container's output for token and password); dialling the team without a port in the URI cost 2 to 7 s of NAPTR and SRV lookups before the team rang (0.1 s with it); the caller's number travelled in the media URL, which uvicorn and, with `voice` down, Asterisk logged (it travels as a channel variable now, and the kit checks the output of `voice` and `asterisk` for every caller's and the team's number). Not measured: the pilot's router, speech |
| 14.10.2026 | T-1.11, registration at the access | The drill stack, the stand-in router stopped for 180 s and 300 s, and made to refuse the AI telephone's password for 150 s; each once with Asterisk's default retries and once with the product's (`max_retries` at its maximum, `fatal_retry_interval=60`, `auth_rejection_permanent=no`) | Router away: registered again within 10 to 30 s after it came back, with either setting. Password refused for a while: with the defaults rejected for good, until somebody restarts Asterisk; with the product's settings registered again within 30 s. The health check of `asterisk` is the registration now: `unhealthy` about 2 minutes into an outage, `healthy` again once registered (Codex on PR #297, P2) |
| 14.10.2026 | T-1.11, which call a connection is | The capture endpoint on Asterisk 22.11.0: 10 rounds of ten calls set up by one `Dial` to ten legs, the call named by inherited variables; 10 rounds the same with `v()` parameters; then ten calls without `v()` | Inherited variables: 100 of 100 connections reported their own call (checked against Asterisk's own log of each call id). `v()`: 100 of 100 too in these rounds, but the ten later calls without `v()` all carried a query of an earlier `v()` call: the leak of Asterisk issue #2194 (open, no fix), on 22.11.0 as reported for 22.9.0. So the product names a call by variables, which the issue does not touch |
| 14.10.2026 | T-10.5, the adapters on the workbench | `api/tests/test_telephony_speech_engines.py` and two timing scripts in the image of `deploy/speech/`, graphics card, the model files of the kit's run folder; Piper `thorsten-medium` speaks, the sentence goes through the modelled line (§10), the ear is Whisper large-v3-turbo behind `SegmentingEar`. A sentence fed in frames of 20 ms arrives as one utterance with partials before it, and its end is the window the detector finds over the whole recording, to the frame. "Ich hätte gern zweimal die Nummer 23." arrived word for word in 6 of 7 runs and once as "Nummer 3 und 20", which the number rule reads as 23 as well; the test speaks the voice without its noise ("Nummer 45"), so every run hears the same audio. with three duck names as key terms "Einmal die Ente süß-sauer, bitte." arrives word for word; a silent line reports nothing. Warm, 12 utterances: Whisper 0.28 s median, 2.2 s at most; the detector 0.08 ms per window of 32 ms; Piper 0.14 s to the first piece, a read-back of 12.5 s of audio 0.17 s to the first piece and 0.76 s whole. Of six sentences with a phone number the core reads four as said; Whisper writes the digits in its own groups ("0721 5.551.234", "01712345678", from words "0171 2345678"), "plus" as a word ("Plus 491712345678") and "01712.345.678 um 19 Uhr" are not read, and nothing wrong is taken. A spelled name arrives as "M-E-I-E-R". | **The pair runs as adapters.** Ear and mouth together are about 0.4 s of a turn on the card, plus the 0.7 s of silence that ends it. Synthetic voices only; what real callers, line noise and the echo of our own voice on the caller's leg do to the detector and to the numbers is T-10.7 |
| 14.10.2026 | T-1.11 part 2, the engines inside `voice` | The image of the target `voice` on the workbench (graphics card, the kit's model files read-only); `api/tests/test_telephony_speech_engines.py` in it; `voice` started as Compose starts it (uid 10001) with `whisper` and `piper` against a throwaway database; `python3 -m scripts.voice_drill` twice on the branch | The engine tests 4 of 4, among them a whole call through `/media` on the engines as the service loads them: Piper greets, "Ich hätte gern zweimal die Nummer 45." goes in as frames of the media channel and reaches the core word for word, the goodbye is played and the line hung up; mouth to first audio 0.08 s, ear 0.48 s after the audio. `voice` healthy 4 s after its start, the models loaded (two warnings: Hugging Face cannot write its tree cache into the read-only folder, onnxruntime cannot keep a telemetry id). Drills: first run 10 of 11, in "voice frozen" the team rang after 4 s and hung up after 2 s, but the caller's leg on the stand-in router stayed open until the kit gave up after 60 s (§12); second run 15 of 15 (kill 0.4 s, `paused` 0.2 s, `voice` stopped 5.2 s, frozen 4.5 s, frozen mid-call 8.1 s, database stopped 8.4 s). Not measured: an engine that really hangs in the service (the watchdog is tested against a clock), real voices, the router |

---

## 12. Open questions

- **A caller's leg left open after the team hung up (seen once, 14.10.2026):** in the drill
  "voice frozen" of the first of two runs on the branch of T-1.11 part 2, the team leg rang and
  hung up, and the caller's leg stayed up on the stand-in router for the 60 s the kit waits; the
  product's Asterisk logged nothing after "to the team". The second run, and every run of part
  1, ended the call. The backstop of the dialplan is an hour. Watch for it in T-10.2: a real
  caller hangs up, but the line stays busy until then.
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
- **From T-10.3, numbers with a letter, what the number rule does not catch** (the prefix
  heard as "ist" is fixed since 12.10.2026, §6): (1) the letter is dropped completely,
  "Ich hätte gern Es zwölf" heard as "Ich hätte gerne zwölf" (8 of 16 utterances of that
  one sentence over all ears, 1 of 4 on the picked ear). Nothing in the transcript tells it
  from the plain 12; on the real menu 30 of the 37 S numbers also exist as plain numbers,
  so asking on every bare number would cost a question on most plain orders. Not built; the
  read-back names the dish. (2) **The trailing letter on the real menu** (run of
  12.10.2026, §11): 87 of the 173 dishes carry a letter a to g, and all 27 silent wrong
  numbers of the picked ear were among their 348 utterances (7.8 %). **The letter heard as
  a word is fixed since 13.10.2026** (eval `abholung_0080`, Maxi's programme of 12.10.2026:
  silent wrong to zero, then 90 % right): "Nummer 26 der", "27 die", "30 ja", "27 äh", with
  nothing but filler behind the word, ask "Meinen Sie Nummer 26, ... oder Nummer 26d, ...?" where the menu
  has the lettered number, the way "ist" in front of the number does since PR #257. The
  recording judged again: 14 instead of 27 (4.0 % of the 348, 2.0 % of all card numbers).
  **Left, and not catchable by a rule:** the letter gone with nothing behind the number (4:
  "Nummer 36e" heard as "Nummer sechsunddreißig", the same as (1)), the letter heard as
  another letter of the menu (6, g as D 4 times), other digits (4). Words the rule leaves
  alone on purpose: "bitte" (heard once for b by a Whisper ear), because "die 20 bitte" is
  how a plain order ends. **The second run (13.10.2026, §11) confirms it:** none of the 20
  silent wrong card numbers of the picked ear is a letter heard as a word any more, 11 are
  the letter gone without a trace. It also shows where the questions come from: the picked
  ear writes 23 % of the two-word sentences in another language or as noise, Whisper with
  the language set 3 %. So the way to 90 % right is the ear, not the rule: an ear that is
  held to German (to check: whether Parakeet v3 can be, else Whisper turbo with key terms,
  whose dropped prefixes and letters then need the read-back), measured on what guests
  really say. **Spoken orders (13.10.2026, §11):** 180 orders of 2 to 12 dishes by number,
  judged per order. The best ear (Whisper turbo) takes 62 % of the orders completely right
  and 18 % with a dish or quantity the guest did not say; per dish said that is 5 to 7 %
  on every ear, and it adds up with the length of the order (2 % wrong at two dishes, a
  third at twelve). Canary, the ear held to German, is no better than Parakeet on orders.
  What this means for the build, each a decision or a task of its own: (a) the read-back
  with its correction (PR #271) is what makes an order right, not the ear, so it has to
  name every dish with its number and survive several corrections in a row; (b) the turn
  must not end before 700 ms of silence while an order is being said, or one order in ten
  is cut in two (T-10.4 and the turn detector); (c) the agent already says back what it
  understood after each sentence, and a long order said in one breath takes that chance
  away, so asking for the dishes a few at a time is worth a try in the prompt; (d) words
  the ears write for a letter or a number of synthetic voices ("dreißig Zi", "die FENF")
  are not worth a rule before real voices are heard. Next: real voices (T-10.7).
- **From the run on the real menu, key terms do not scale to a whole menu** (and the full menu may go into no model context, hard rule 6; the kit hands over a window of 10 names per utterance since 12.10.2026)**:** a dish name heard
  with its key term arrives in 90 % instead of 24 %, but only 25 of the 173 names fit the
  ear's prompt at once. T-10.5 ("key terms from the active menu") therefore has to choose the
  terms per turn, for instance the dishes that are in question at that point of the call,
  instead of handing over the menu. **Built so with T-10.5 (§6):** the dishes the agent just
  named, then the order. Not measured yet on calls: how often a guest's answer is a dish the
  agent had named (T-10.7).
- **From T-10.3, what the synthetic callers cannot say:** the measurement ranks the engines, it
  does not predict real callers. The same sentences spoken by people over a real line are T-10.7;
  the thresholds of §6 are confirmed or changed with those numbers.
- **From T-10.3, the model files are not pinned:** the packages in the image are, the weights the
  kit downloads are whatever the model repositories hold that day. A later run can measure other
  weights than the two runs in §11; pin them when a number has to be reproduced exactly.
- **From T-10.3, the voice (D4):** Maxi finds the test-phase voice not clean enough for customers; his own voice as the mouth is an idea kept for the measurement of the operation services (`docs/01_STATUS.md` D4).
- **From T-10.3, licences:** the Piper package (`piper-tts` 1.8) is GPL-3.0-or-later, the voice
  `thorsten` is from a CC0 data set; Parakeet v3 is CC-BY-4.0, its ONNX runtime wrapper MIT; Whisper
  and its wrapper are MIT. All run as separate programs on our own machine in the test phase. What
  that means once the software is handed to a customer is a point for the legal check before
  operation; in operation the plan is EU services anyway (§6).
- **From T-10.3, candidates for operation (web research of 12.10.2026, not verified, no account
  yet):** the speech-to-text and text-to-speech services of the text model's favourite vendor
  (docs/18 §3), and one vendor with an EU endpoint for both. Where each processes data and whether a
  data processing agreement exists is checked before the first key is used on anything but invented
  sentences.
- **Team reachability:** today `transfer_to_team` guesses from the opening hours whether somebody
  can answer; the real answer is the result of the transfer (T-10.8).
- **From T-10.1 step 1, for T-1.11:** the media socket of the spike has no access control, which is
  fine on a network of its own without a published port; the service `voice` needs one (the
  connection in `websocket_client.conf` can carry a user name and password, and TLS). **Built
  with T-1.11:** Basic credentials with the media token on both doors of `voice`; TLS between the
  two containers of one network is not. The Asterisk
  image is a third-party build, pinned by digest; for operation decide between that and an image
  built from the official source. The containers of the voice layer are to start as an unprivileged user from the beginning (the probe does,
  Asterisk drops root itself).
- **From T-1.11, what the team sees on a handed-over call:** the team leg carries the user of our
  registration as its caller (`from_user`), not the guest's number; the router authenticates us by
  it. Whether the pilot's router shows the guest's number when we also send it (P-Asserted-Identity)
  is a point for T-10.2. The team needs it to call a guest back.
- **From T-1.11, container logs on the server:** no number is in them by design (the drill checks),
  but they are kept as long as Docker keeps them; on the server they need a limit of their own
  (rotation and age), like every other place that may hold call data (docs/17).
- **From T-1.11, a stopped container's name:** in WSL the name lookup of a stopped container (`voice`,
  `db`) takes about 5 s before it fails ("Try again"), and that is most of the time to the team in
  those two drills. On the server it is measured again (T-10.2); a lookup that fails at once would
  bring both under a second.
- **From T-1.11, not built:** the hold sentence while a turn runs (§4). A `voice` whose process
  answers pings but holds a call and does nothing (a worker stuck while the event loop runs) is
  ended only by the backstop of an hour; the session's own deadlines cover the known ways (§4).
  TLS and the public
  media address for a trunk (§3), and with that address the media range of `rtp.conf` published on
  the server, exactly that range, with the port test's allow-list for it: published without the
  address it would carry nothing, so production publishes nothing beside the proxy until then
  (Codex on PR #297). `voice` holds its live calls in one process, so a restart sends every live
  call to the team, as the drill of a crash shows.
- **From T-1.11, a team that does not pick up:** the dialplan rings the team for 30 s and then ends
  the call; the caller hears nothing more. That is the row "The team does not pick up" of §5, the
  task T-10.8 (a callback card, and a sentence that nobody is reachable), which waits for T-1.11.
  Until then the core sends a call to the team only when the opening hours say somebody is there,
  and no real customer calls the AI telephone (§3, test number).

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
