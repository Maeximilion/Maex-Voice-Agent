"""Druckprotokoll der Bruecke: welche Bestellung schon in welcher Revision gedruckt ist.

Zwei Faelle, in denen der Server einen Bon ein zweites Mal ausliefert:
- gedruckt, aber die Rueckmeldung ging verloren (Netz weg): nach der Leihfrist
  kommt derselbe Bon wieder;
- "Nochmal senden" im Tablet, waehrend der erste Versuch doch noch durchkam.
Die Kueche soll dann keinen zweiten Zettel bekommen, und nie einen aelteren Stand
nach einem neueren (docs/04 §confirm, Vertrag b).

Gespeichert werden nur Bestell-ids und Revisionen, keine Namen oder Nummern.
"""

import json
import os
from pathlib import Path

KEEP = 2000


class PrintedLog:
    def __init__(self, path: Path, keep: int = KEEP):
        self.path = path
        self.keep = keep
        self._printed: dict[str, int] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._printed = {
                    str(k): int(v) for k, v in data.get("printed", {}).items()
                }
            except (OSError, ValueError, AttributeError):
                # Kaputte Datei: lieber einmal doppelt drucken als nie.
                self._printed = {}

    def already(self, order_id: str, revision: int) -> bool:
        """True, wenn diese oder eine neuere Revision schon gedruckt ist."""
        return self._printed.get(order_id, -1) >= revision

    def record(self, order_id: str, revision: int) -> None:
        self._printed.pop(order_id, None)  # ans Ende: die Aeltesten fallen zuerst weg
        self._printed[order_id] = revision
        while len(self._printed) > self.keep:
            self._printed.pop(next(iter(self._printed)))
        self._save()

    def _save(self) -> None:
        # Erst schreiben, dann ersetzen: ein Stromausfall mittendrin laesst die alte Datei heil.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"printed": self._printed}), encoding="utf-8")
        os.replace(tmp, self.path)
