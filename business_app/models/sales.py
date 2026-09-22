"""Sales-agent domain models: agent profile, outlets, contacts, stage history.

Outlets are deliberately NOT users: a prospect has no phone or contract yet.
An outlet links to a User/UserAddress only once approved (see OutletService).
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
)

from business_app import db
from business_app.models.base import TimestampMixin
from business_app.utils.geo_validation import register_delivery_zone_listeners
from business_app.utils.timezone_utils import get_utc_now

OUTLET_TYPES = ("grocery_store", "workplace", "individual")
OUTLET_STAGES = ("prospect", "trial", "activation_requested", "active", "at_risk", "dormant", "lost")
OUTLET_CLASSES = ("A", "B", "C")
CONTACT_ROLES = ("owner", "decision_maker", "receiver", "payer")
LOST_REASONS = (
    "price",
    "has_supplier",
    "no_space",
    "owner_absent",
    "low_footfall",
    "payment_terms",
    "quality",
    "closed",
    "not_reached",
    "other",
)
EMPLOYMENT_TYPES = ("employee", "contractor")


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


class SalesAgentProfile(db.Model, TimestampMixin):
    """Activation, territory hints and targets for a user holding the sales_agent role."""

    __tablename__ = "sales_agent_profiles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_sales_agent_profiles_user_id"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", name="fk_sales_agent_profiles_user_id"), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    districts = Column(JSON, nullable=False, default=list)
    weekly_new_outlet_target = Column(Integer, nullable=True)
    employment_type = Column(String(20), nullable=True)
    notes = Column(Text, nullable=True)
    created_by_user_id = Column(
        Integer, ForeignKey("users.id", name="fk_sales_agent_profiles_created_by"), nullable=True
    )

    user = db.relationship("User", foreign_keys=[user_id], backref=db.backref("sales_agent_profile", uselist=False))

    def __repr__(self):
        return f"<SalesAgentProfile user={self.user_id} active={self.is_active}>"


class Outlet(db.Model, TimestampMixin):
    """A place an agent serves. Links to a customer account once approved."""

    __tablename__ = "outlets"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_outlets_user_id"),
        UniqueConstraint("address_id", name="uq_outlets_address_id"),
        CheckConstraint(f"outlet_type IN ({_quoted(OUTLET_TYPES)})", name="ck_outlets_outlet_type"),
        CheckConstraint(f"stage IN ({_quoted(OUTLET_STAGES)})", name="ck_outlets_stage"),
        CheckConstraint(f'"class" IS NULL OR "class" IN ({_quoted(OUTLET_CLASSES)})', name="ck_outlets_class"),
        CheckConstraint("(latitude IS NULL) = (longitude IS NULL)", name="ck_outlets_coords_pair"),
        Index("ix_outlets_stage", "stage"),
        Index("ix_outlets_assigned_agent_user_id", "assigned_agent_user_id"),
        Index("ix_outlets_onboarded_by_user_id", "onboarded_by_user_id"),
        Index("ix_outlets_next_visit_due_at", "next_visit_due_at"),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    outlet_type = Column(String(20), nullable=False)
    channel = Column(String(30), nullable=True)
    stage = Column(String(30), nullable=False, default="prospect")
    outlet_class = Column("class", String(1), nullable=True)
    cadence_days_override = Column(Integer, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", name="fk_outlets_user_id"), nullable=True)
    address_id = Column(Integer, ForeignKey("addresses.id", name="fk_outlets_address_id"), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    address_text = Column(Text, nullable=True)
    district = Column(String(100), nullable=True)
    assigned_agent_user_id = Column(
        Integer, ForeignKey("users.id", name="fk_outlets_assigned_agent_user_id"), nullable=True
    )
    onboarded_by_user_id = Column(
        Integer, ForeignKey("users.id", name="fk_outlets_onboarded_by_user_id"), nullable=True
    )
    next_visit_due_at = Column(DateTime(timezone=True), nullable=True)
    agent_next_visit_at = Column(Date, nullable=True)
    last_visit_at = Column(DateTime(timezone=True), nullable=True)
    last_order_at = Column(DateTime(timezone=True), nullable=True)
    opening_hours = Column(JSON, nullable=True)
    preferred_visit_window = Column(String(50), nullable=True)
    delivery_window_start = Column(Time, nullable=True)
    delivery_window_end = Column(Time, nullable=True)
    payment_terms = Column(String(20), nullable=False, default="cash")
    legal_form = Column(String(30), nullable=True)
    tax_id = Column(String(20), nullable=True)
    preferred_language = Column(String(5), nullable=False, default="uz")
    storefront_photo_path = Column(String(500), nullable=True)
    competitor_note = Column(Text, nullable=True)
    status_warning = Column(Text, nullable=True)
    dedupe_candidates = Column(JSON, nullable=False, default=list)
    activation_requested_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id", name="fk_outlets_approved_by_user_id"), nullable=True)
    rejected_reason = Column(Text, nullable=True)
    lost_reason = Column(String(30), nullable=True)
    lost_note = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])
    address = db.relationship("UserAddress", foreign_keys=[address_id])
    assigned_agent = db.relationship("User", foreign_keys=[assigned_agent_user_id])
    contacts = db.relationship(
        "OutletContact", backref="outlet", cascade="all, delete-orphan", order_by="OutletContact.id"
    )
    stage_history = db.relationship(
        "OutletStageHistory", backref="outlet", cascade="all, delete-orphan", order_by="OutletStageHistory.id"
    )

    @property
    def primary_contact(self):
        for contact in self.contacts:
            if contact.is_primary:
                return contact
        return self.contacts[0] if self.contacts else None

    def __repr__(self):
        return f"<Outlet {self.id} {self.name!r} {self.stage}>"


class OutletContact(db.Model, TimestampMixin):
    __tablename__ = "outlet_contacts"
    __table_args__ = (
        Index("ix_outlet_contacts_outlet_id", "outlet_id"),
        Index("ix_outlet_contacts_phone", "phone"),
    )

    id = Column(Integer, primary_key=True)
    outlet_id = Column(Integer, ForeignKey("outlets.id", name="fk_outlet_contacts_outlet_id"), nullable=False)
    name = Column(String(100), nullable=False)
    phone = Column(String(20), nullable=True)
    role = Column(String(20), nullable=False, default="owner")
    is_primary = Column(Boolean, nullable=False, default=False)
    presence_window = Column(String(100), nullable=True)


class OutletStageHistory(db.Model):
    __tablename__ = "outlet_stage_history"
    __table_args__ = (Index("ix_outlet_stage_history_outlet_id", "outlet_id"),)

    id = Column(Integer, primary_key=True)
    outlet_id = Column(Integer, ForeignKey("outlets.id", name="fk_outlet_stage_history_outlet_id"), nullable=False)
    from_stage = Column(String(30), nullable=True)
    to_stage = Column(String(30), nullable=False)
    reason_code = Column(String(30), nullable=True)
    note = Column(Text, nullable=True)
    actor_user_id = Column(Integer, ForeignKey("users.id", name="fk_outlet_stage_history_actor_user_id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=get_utc_now, nullable=False)


# SSOT backstop, shared with UserAddress: never persist a pin outside TASHKENT_POLYGON.
register_delivery_zone_listeners(Outlet)
