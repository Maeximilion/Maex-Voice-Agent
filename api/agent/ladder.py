"""Verständnis-Leiter als Zustandsmaschine (docs/05 §2, docs/11 §agent).

Zählt Fehlversuche je Information ("dieselbe Information" = ein Feldname wie
`party_size` oder `reserved_for`) und steigt eine Stufe, wenn zwei Versuche auf
derselben Stufe scheitern (docs/05 §2 Regel). Stufen 4 (Tastatur) und 5 (SMS)
sind laut docs/05 §2 kontextabhängige Werkzeuge, keine Pflichtstationen einer
festen Kette — für Stufe 1 (nur Reservierung: Datum, Personenzahl, Name,
Rufnummer) bleibt die Leiter bei den ersten drei Stufen (gezielt nachfragen,
bestätigen lassen, buchstabieren lassen). Nach drei Stufenwechseln ohne Erfolg
endet der Anruf bei Stufe 6 oder 7 (docs/05 §2), unabhängig davon, ob 4/5
zwischendurch verwendet wurden — die Leiter ist ein Versuchsbudget, keine
Pflicht, jede Stufe zu durchlaufen.
"""

from dataclasses import dataclass

MAX_LEVEL = 7
FAILURES_BEFORE_LEVEL_UP = 2
LEVEL_UPS_BEFORE_END = 3


@dataclass
class _FieldState:
    level: int = 1
    failures_at_level: int = 0
    level_ups: int = 0


class UnderstandingLadder:
    """Ein Feldname pro unterscheidbarer Information (z. B. `party_size`,
    `reserved_for`, `guest_name`). Getrennter Zustand je Feld: ein Kunde, der
    beim Datum zweimal scheitert, verliert dadurch keinen bereits verstandenen
    Namen."""

    def __init__(self) -> None:
        self._fields: dict[str, _FieldState] = {}

    def level_for(self, field_name: str) -> int:
        return self._fields.get(field_name, _FieldState()).level

    def record_failure(self, field_name: str) -> int:
        """Ein gescheiterter Versuch für `field_name`. Gibt die Stufe zurück,
        auf der als Nächstes operiert wird."""
        state = self._fields.setdefault(field_name, _FieldState())
        state.failures_at_level += 1
        if (
            state.failures_at_level >= FAILURES_BEFORE_LEVEL_UP
            and state.level < MAX_LEVEL
        ):
            state.level += 1
            state.failures_at_level = 0
            state.level_ups += 1
        return state.level

    def record_success(self, field_name: str) -> None:
        """Information verstanden: ihr Leiter-Zustand verfällt, ein späterer
        neuer Fehlversuch an derselben Information beginnt wieder bei Stufe 1."""
        self._fields.pop(field_name, None)

    def should_end_call(self, field_name: str) -> bool:
        """Drei Stufenwechsel ohne Erfolg an derselben Information (docs/05 §2)."""
        return (
            self._fields.get(field_name, _FieldState()).level_ups
            >= LEVEL_UPS_BEFORE_END
        )

    def active_levels(self) -> dict[str, int]:
        """Nur Felder über Stufe 1, fürs Prompt-Hint an das Modell (`loop.py`) —
        ein frisches Feld braucht keine Erwähnung im knappen Zustand (docs/05 §5)."""
        return {name: st.level for name, st in self._fields.items() if st.level > 1}
