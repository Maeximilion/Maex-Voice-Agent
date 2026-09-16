# sim – das Text-Telefon

Gespräche mit dem Agenten ohne Telefon und ohne Voice-Plattform.

```bash
python -m sim.cli                          # interaktiv: du bist der Kunde
python -m sim.replay evals/cases/menu_0042_*.json   # Transkript abspielen
python -m sim.cli --noise 0.2              # 20 % der Eingaben absichtlich verrauscht
```

Zeigt je Zug: Kundensatz → Tool-Aufrufe mit Dauer → Agentenantwort → Gesprächszustand.
Nutzt `api/agent/` direkt und schreibt in die lokale DB, sodass die Bestellung sofort in der GUI erscheint.

Aufgaben: T-2.1 bis T-2.3 in `docs/07_ARBEITSPAKETE.md`. Aufbau: `docs/11_MODULE.md` §sim.
