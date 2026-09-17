"""Der kalte Pfad: Ereignisse in der Outbox und ihr Versand nach n8n (docs/11 §events)."""

from api.events.outbox import enqueue

__all__ = ["enqueue"]
