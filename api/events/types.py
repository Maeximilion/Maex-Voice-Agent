"""Ereignistypen des kalten Pfads (docs/03 §outbox, docs/11 §events).

Namen statt Zeichenketten im Fachcode: ein Tippfehler faellt beim Import auf und
nicht erst an der CHECK-Bedingung der Tabelle.
"""

ORDER_CONFIRMED = "order.confirmed"
RESERVATION_CONFIRMED = "reservation.confirmed"
CALLBACK_CREATED = "callback.created"
ORDER_HANDOVER_FAILED = "order.handover_failed"
DAILY_REPORT = "daily.report"
# A change the staff make without the owner's PIN, and every change of the
# call mode: a push to the owner and to the operator (#588, docs/23 T12).
TEAM_PHONE_CHANGED = "team_phone.changed"
DAY_HOURS_CHANGED = "day_hours.changed"
CALL_MODE_CHANGED = "call_mode.changed"

ALL = (
    ORDER_CONFIRMED,
    RESERVATION_CONFIRMED,
    CALLBACK_CREATED,
    ORDER_HANDOVER_FAILED,
    DAILY_REPORT,
    TEAM_PHONE_CHANGED,
    DAY_HOURS_CHANGED,
    CALL_MODE_CHANGED,
)

# Diese Ereignisse holt die Druckbruecke im Restaurant ab (T-4.6), nicht der
# Dispatcher: der Server im Rechenzentrum erreicht den Drucker im Lokal nicht,
# die Bruecke den Server schon (docs/02 §Kuechenbon).
KITCHEN = (ORDER_CONFIRMED,)
