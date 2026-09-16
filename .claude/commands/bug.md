Behandle diesen Fehler aus dem Betrieb: $ARGUMENTS

Reihenfolge ist Pflicht:
1. Lege ZUERST einen Eval-Fall in `evals/cases/` an, der den Fehler beschreibt (Format: `docs/08_EVALS.md`). Erwartet wird das richtige Verhalten.
2. Führe `make eval` für diesen Fall aus. Er MUSS rot sein. Ist er grün, ist der Fall falsch beschrieben – korrigiere ihn, bevor du weitergehst.
3. Finde die Ursache: welches Modul aus `docs/11_MODULE.md`? Nenne Datei und Funktion.
4. Schreibe einen Unit-Test im betroffenen Modul, der die Ursache isoliert trifft.
5. Behebe minimal. Keine Nebenänderungen.
6. Führe `make test` und `make eval` vollständig aus. Alles grün, Genauigkeit nicht gesunken.
7. Commit: `fix(<modul>): <was> (#eval <fall-id>)`.
Kein Fix ohne vorherigen roten Testfall.
