"""SQLAlchemy-Modelle. Alle Tabellen importieren, damit Base.metadata vollständig ist."""

from api.models.access import (
    GuiAccount,
    GuiDevice,
    GuiLoginFailure,
    GuiSession,
    GuiSetupLink,
)
from api.models.audit import AuditLog
from api.models.base import Base
from api.models.calls import Call, Callback, ComplaintRule, ConsentLog, Recording
from api.models.customers import (
    Address,
    Customer,
    DataSubjectRequest,
    DeliveryZone,
    RecognitionObjection,
)
from api.models.floor_plan import DiningTable, FloorArea, TableCombination
from api.models.menu import ItemAlias, ItemAllergen, ItemOption, MenuItem
from api.models.orders import Order, OrderItem
from api.models.outbox import OutboxEvent
from api.models.reservations import Reservation, ReservationTable
from api.models.tenants import (
    BridgeHeartbeat,
    CallLimitWindow,
    CallModeWindow,
    Capacity,
    OpeningHours,
    ServiceConfig,
    SpecialDay,
    Tenant,
)

__all__ = [
    "Address",
    "AuditLog",
    "Base",
    "BridgeHeartbeat",
    "Call",
    "CallLimitWindow",
    "CallModeWindow",
    "Callback",
    "Capacity",
    "ComplaintRule",
    "ConsentLog",
    "Customer",
    "DataSubjectRequest",
    "DeliveryZone",
    "DiningTable",
    "FloorArea",
    "GuiAccount",
    "GuiDevice",
    "GuiLoginFailure",
    "GuiSession",
    "GuiSetupLink",
    "ItemAlias",
    "ItemAllergen",
    "ItemOption",
    "MenuItem",
    "OpeningHours",
    "Order",
    "OrderItem",
    "OutboxEvent",
    "RecognitionObjection",
    "Recording",
    "Reservation",
    "ReservationTable",
    "ServiceConfig",
    "SpecialDay",
    "TableCombination",
    "Tenant",
]
