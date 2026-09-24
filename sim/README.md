# sim – the text phone

Conversations with the agent without phone and without voice platform.

```bash
python -m sim.cli                                    # interactive: you are the customer
python -m sim.replay evals/cases/reservierung_0001_tisch_fuer_vier.json
python -m sim.cli --noise 0.2 --seed 7               # 20 percent of words garbled, reproducibly
python -m sim.cli --now 2026-09-15T18:00+02:00       # fixed point in time, e.g. inside opening hours
```

Shows per turn: customer sentence, tool calls with duration from `calls.tool_calls`, agent
response, conversation state. Uses `api/agent/` directly and writes to the local database,
so a confirmed reservation is really in `reservations` and appears in the UI later.

Needs a seeded database (`make migrate`, `make seed`); `--tenant` picks one by name,
otherwise the first one is used.

Until T-2.4 connects a real model, `scripted_llm.py` stands in for it: rule-based, German,
scoped to the reservation flow and the pickup flow (`scripted_order.py`) from
`prompts/system_v2.md`. It never guesses — what it does
not recognize it reports as a failed attempt, and the understanding ladder (`api/agent/ladder.py`)
decides what happens next.

The caller's number comes from caller ID, as on the phone: `--caller` in the terminal,
`caller_id` in a case file. With it the agent does not ask for the number; without it
(withheld) it asks.

Files: `cli.py` terminal, `replay.py` transcript, `session.py` shared call mechanics,
`scripted_llm.py` model stand-in, `scripted_order.py` its pickup flow, `noise.py`
deliberate garbling.

Tasks: T-2.1 through T-2.3 in `docs/07_WORKPACKAGES.md`. Structure: `docs/11_MODULE.md` §sim.
