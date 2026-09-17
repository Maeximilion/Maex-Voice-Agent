# sim – the text phone

Conversations with the agent without phone and without voice platform.

```bash
python -m sim.cli                          # interactive: you are the customer
python -m sim.replay evals/cases/menu_0042_*.json   # replay transcript
python -m sim.cli --noise 0.2              # 20% of inputs intentionally noised
```

Shows per turn: customer sentence → tool calls with duration → agent response → conversation state.
Uses `api/agent/` directly and writes to local DB so the order appears immediately in the UI.

Tasks: T-2.1 through T-2.3 in `docs/07_WORKPACKAGES.md`. Structure: `docs/11_MODULES.md` §sim.
