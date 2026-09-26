"""Speisekarte (docs/03 Stufe 2, Importformat docs/14).

Preise, Optionen und Allergene kommen nur von hier (CLAUDE.md §2 Regel 1). Die
Kartennummer ist der fachliche Schlüssel je Mandant: sie ist der robusteste Weg
durch eine schlechte Leitung.

Die Kindtabellen (Optionen, Allergene, Aliase) tragen kein eigenes tenant_id:
sie hängen an genau einem Gericht und erreichen den Mandanten über dessen
Zeile, wie es docs/03 je Tabelle festlegt. Anders order_items (orders.py): die
Position hat zwei Eltern und muss beide im selben Mandanten halten.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey
from api.models.tenants import _in

ALIAS_SOURCES = ("manual", "call", "import")
# Die 14 Hauptallergene der LMIV (Anhang II) mit den in Deutschland üblichen
# Kennbuchstaben. Kein I, J, K, Q: die Lücken sind Absicht, nicht Versehen.
# A Gluten, B Krebstiere, C Eier, D Fisch, E Erdnüsse, F Soja, G Milch,
# H Schalenfrüchte, L Sellerie, M Senf, N Sesam, O Sulfite, P Lupinen,
# R Weichtiere.
ALLERGEN_CODES = ("A", "B", "C", "D", "E", "F", "G", "H", "L", "M", "N", "O", "P", "R")


class MenuItem(UUIDPrimaryKey, TenantScoped, Timestamps, Base):
    __tablename__ = "menu_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_menu_items_tenant_number"),
        # Ziel des zusammengesetzten Fremdschluessels aus order_items: eine
        # Position darf nur auf ein Gericht desselben Mandanten zeigen.
        UniqueConstraint("id", "tenant_id", name="uq_menu_items_id_tenant"),
        CheckConstraint("price_cents >= 0", name="ck_menu_items_price_cents"),
        # Trigram-Suche fuer search_menu (docs/04): "Fruehlingsrolle" findet
        # "Frühlingsrollen (4 Stück)" auch bei Tippfehlern der Erkennung.
        Index(
            "ix_menu_items_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    # Text, nicht Zahl: "23a" kommt vor (docs/14).
    number: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # Schalter "Gericht aus" (docs/06 §3): ausverkauft bis zu diesem Zeitpunkt.
    sold_out_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    description: Mapped[str | None] = mapped_column(Text)


class ItemOption(UUIDPrimaryKey, Timestamps, Base):
    """Variante oder Extra mit Preisdifferenz. Die Differenz darf negativ sein."""

    __tablename__ = "item_options"
    __table_args__ = (
        UniqueConstraint(
            "menu_item_id",
            "group_name",
            "option_name",
            name="uq_item_options_item_group_option",
        ),
    )

    menu_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("menu_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_name: Mapped[str] = mapped_column(Text, nullable=False)
    option_name: Mapped[str] = mapped_column(Text, nullable=False)
    price_delta_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # Warum die Option mehr kostet (Migration 003, T-4.10). Der Agent nennt nur
    # diesen Satz, nie eine eigene Begruendung.
    price_reason: Mapped[str | None] = mapped_column(Text)


class ItemAllergen(Timestamps, Base):
    """Ein gepflegter Allergen-Wert. Keine Zeile heisst "keine Auskunft", nie "frei davon"."""

    __tablename__ = "item_allergens"
    __table_args__ = (_in("allergen_code", ALLERGEN_CODES, "ck_item_allergens_code"),)

    menu_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("menu_items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # LMIV-Kennbuchstabe, nur aus ALLERGEN_CODES.
    allergen_code: Mapped[str] = mapped_column(Text, primary_key=True)
    # Pflicht: ein Allergen-Wert ohne Namen dahinter ist keine Auskunft.
    confirmed_by: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ItemAlias(UUIDPrimaryKey, Timestamps, Base):
    """Kundensprache zur Karte. Normalisiert gespeichert (docs/14), deshalb eindeutig je Gericht.

    Derselbe Alias darf an zwei Gerichten hängen - der Importer warnt dann, und
    search_menu fragt nach statt zu raten (CLAUDE.md §2 Regel 2).
    """

    __tablename__ = "item_aliases"
    __table_args__ = (
        _in("source", ALIAS_SOURCES, "ck_item_aliases_source"),
        UniqueConstraint("menu_item_id", "alias", name="uq_item_aliases_item_alias"),
        CheckConstraint("hits >= 0", name="ck_item_aliases_hits"),
        Index(
            "ix_item_aliases_alias_trgm",
            "alias",
            postgresql_using="gin",
            postgresql_ops={"alias": "gin_trgm_ops"},
        ),
    )

    menu_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("menu_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
