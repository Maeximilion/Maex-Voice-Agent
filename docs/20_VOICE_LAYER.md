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

**Thresholds (proposal; T-10.3 reported on 12.10.2026, the confirmation is Maxi's):** median voice
to voice at most 1.5 s on turns with one tool hop; silent wrong card numbers at most 2 % on
`evals/cases/nummern.jsonl` spoken as audio.

- **Silent wrong numbers: at the limit, not safely under it.** The picked ear took a number the
  guest had not said in 1.5 % (run 1) and 2.1 % (run 2) of 724 number utterances; the Whisper ears
  in 4.6 to 9.0 %. Counted only where the number is a dish of the eval menu (so the guest gets
  another dish read back, instead of "that number does not exist"): 1.1 and 0.8 %. Plain numbers
  ("die 23") were safe: 0 and 1 of 148. Almost all of it is one kind of sentence, a number with a
  letter: "S12" is heard as "ist zwölf" and taken as dish 12, "23g" as "23 D" (9 to 11 % of 88).
  That is a gap in the number rule as much as in the ear (§12).
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
  The dialed extension and the caller number arrive as URI parameters of the WebSocket request.
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

### Handover: T-10.3, ear and mouth on the workbench (run 12.10.2026)

| | |
|---|---|
| Start folder | the repository folder in the Ubuntu home directory (or a worktree of it), Ubuntu terminal |
| The one command | `python3 -m scripts.speech_spike` (`--dry-run` prints the steps, `--limit 6` is a short trial of the kit, not a measurement; a full run takes about 20 minutes) |
| Maxi has to have ready | Docker Desktop running with the WSL integration, about 10 GB of disk (image 4.8 GB, model files about 5 GB, both kept for the next run). No account, no `.env` entry, no sudo. The first run downloads the model files |
| What the result looks like | one table for the ears, one for the mouths, one for the silence detector, then the pick, or `no-go` with the ear that came closest; exit code 0 with a pick, 1 without. `deploy/speech/run/report.json` holds the same, `judged.json` every sentence with what each ear heard, `samples/` one sound file per voice and agent sentence, as a caller would hear it |

What it runs (`deploy/speech/Dockerfile`, one image: the open speech engines next to the project's
packages; three containers one after another, no host port): the first downloads the model files
and is the only one with network access; the second speaks, hears and times
(`api/telephony/speech_bench.py`); the third judges (`evals/heard.py`). The last two run with
`--network none`, so no audio and no sentence leaves the machine. The sentences are the number eval
(`evals/cases/nummern.jsonl`) and the dish names of the eval menu; the key terms an ear can be
given are those dish names, read from the menu file. Which engines are tried is the table at the
top of `api/telephony/speech_spike.py`.

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
- **From T-10.3, numbers with a letter:** "S12" spoken is heard as "ist zwölf" or just "zwölf" by
  every ear tried, and the number rule then takes dish 12 without a question; "23g" becomes "23 D".
  The read-back names the dish, so the guest can still say no, but nothing in the code stops it.
  Proposal, as a task of its own with a red case in `evals/cases/nummern.jsonl` first: where the
  card has a letter prefix, a number that follows "ist" or stands alone where a prefixed one exists
  asks back ("S12 oder 12?"). It decides which dish a sentence means, so it runs at effort `xhigh`.
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
  connection in `websocket_client.conf` can carry a user name and password, and TLS). The Asterisk
  image is a third-party build, pinned by digest; for operation decide between that and an image
  built from the official source. The containers of the voice layer are to start as an unprivileged user from the beginning (the probe does,
  Asterisk drops root itself).

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
