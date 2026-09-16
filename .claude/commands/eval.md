Führe die Eval-Suite aus und bewerte das Ergebnis.

1. `make eval` (optional gefiltert: $ARGUMENTS).
2. Vergleiche mit dem letzten Lauf in `evals/reports/`: Genauigkeit, die drei harten Metriken (geratene Positionen, unbestätigte Vorgänge, verpasste Eskalationen), Tokens und Kosten je Fall.
3. Liste jeden neu fehlgeschlagenen Fall mit erwartet/bekommen.
4. Urteil in einem Satz: mergefähig oder nicht, und warum.
Ein Verstoß gegen eine harte Metrik bedeutet: nicht mergefähig, egal wie gut die Genauigkeit ist.
