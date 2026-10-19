"""Ereignistypen des kalten Pfads (docs/03 §outbox, docs/11 §events).

Namen statt Zeichenketten im Fachcode: ein Tippfehler faellt beim Import auf und
nicht erst an der CHECK-Bedingung der Tabelle.
"""

ORDER_CONFIRMED = "order.confirmed"
RESERVATION_CONFIRMED = "reservation.confirmed"
CALLBACK_CREATED = "callback.created"
ORDER_HANDOVER_FAILED = "order.handover_failed"
DAILY_REPORT = "daily.report"

ALL = (
    ORDER_CONFIRMED,
    RESERVATION_CONFIRMED,
    CALLBACK_CREATED,
    ORDER_HANDOVER_FAILED,
    DAILY_REPORT,
)
