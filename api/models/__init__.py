"""SQLAlchemy-Modelle. Alle Tabellen importieren, damit Base.metadata vollständig ist."""

from api.models.audit import AuditLog
from api.models.base import Base
from api.models.calls import Call, Callback
from api.models.menu import ItemAlias, ItemAllergen, ItemOption, MenuItem
from api.models.orders import Order, OrderItem
from api.models.outbox import OutboxEvent
from api.models.reservations import Reservation
from api.models.tenants import Capacity, OpeningHours, ServiceConfig, SpecialDay, Tenant

__all__ = [
    "AuditLog",
    "Base",
    "Call",
    "Callback",
    "Capacity",
    "ItemAlias",
    "ItemAllergen",
    "ItemOption",
    "MenuItem",
    "OpeningHours",
    "Order",
    "OrderItem",
    "OutboxEvent",
    "Reservation",
    "ServiceConfig",
    "SpecialDay",
    "Tenant",
]
