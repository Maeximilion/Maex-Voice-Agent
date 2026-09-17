"""Das Text-Telefon (docs/11 §sim): Gespraeche ohne Telefon und ohne Sprachplattform.

`cli.py` fuehrt ein Gespraech im Terminal, `replay.py` spielt ein Transkript aus
`evals/cases/` ab, `noise.py` verrauscht Eingaben absichtlich. Beide Eingaenge
benutzen `api/agent/` direkt und schreiben in die lokale Datenbank, damit der
Durchstich ohne Telefon prueffbar ist: Terminal -> Agent -> Fachlogik -> DB.
"""
