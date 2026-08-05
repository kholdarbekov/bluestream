"""Canonical customer identity — links multiple phone-account Users as one real person.

Phase 1 (docs/superpowers/specs/2026-07-20-multi-phone-customer-identity-linking-design.md):
a reversible LINK, never a destructive merge. A CanonicalCustomer groups Users
(via User.canonical_customer_id); an AddressGroup marks "same physical place" and MAY span customers
(Phase 2 place groups; via UserAddress.address_group_id). No balances are
stored here — cluster figures are always derived.
"""

from sqlalchemy import JSON, Column, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from business_app import db
from business_app.models import TimestampMixin


class CanonicalCustomer(db.Model, TimestampMixin):
    __tablename__ = "canonical_customers"
    __table_args__ = (Index("idx_canonical_customers_primary_user", "primary_user_id"),)

    id = Column(Integer, primary_key=True)
    # Display "face" + notification target for anything cluster-level. Nullable so
    # the row can exist transiently; the service keeps it pointed at the oldest
    # ACTIVE member (fallback: the oldest member) via
    # CustomerLinkService._refresh_primary.
    # ondelete on every users FK in this module (migration f7c3b9e1d5a2): these
    # rows outlive an account merge, which deletes the secondary User. A display
    # or audit stamp yields (SET NULL); an assertion whose subject is gone is
    # deleted with it (CASCADE, below).
    primary_user_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_canonical_customers_primary_user", ondelete="SET NULL"),
        nullable=True,
    )
    created_by_admin_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_canonical_customers_created_by_admin", ondelete="SET NULL"),
        nullable=True,
    )
    notes = Column(Text, nullable=True)


class AddressGroup(db.Model, TimestampMixin):
    """An ownerless "same physical place" group of addresses (may span customers).

    Phase 2 (docs/superpowers/specs/2026-07-24-place-groups-and-cluster-wallet-design.md):
    membership is the single FK UserAddress.address_group_id. canonical_customer_id
    is DEPRECATED and nullable. New place groups will be written NULL once
    CustomerLinkService is rewired later in this plan; pre-existing rows retain
    their (now inert) values, kept only so migration f7c3b9e1d5a2 stays reversible.
    """

    __tablename__ = "address_groups"
    __table_args__ = (Index("idx_address_groups_canonical_customer", "canonical_customer_id"),)

    id = Column(Integer, primary_key=True)
    canonical_customer_id = Column(
        Integer,
        ForeignKey("canonical_customers.id", name="fk_address_groups_canonical_customer"),
        nullable=True,
    )
    label = Column(String(100), nullable=True)
    # THE FORWARDING POINTER (spec §7.3). Which address this place's history was
    # RELEASED ONTO when it dissolved — NULL for every live place.
    #
    # A dissolved group is kept for ever (`bottle_ledger.address_group_id` is a
    # foreign key that every DEPARTED member's rows still carry) and can never be
    # re-populated (`PLACE_GROUP_DISSOLVED`), so this is written EXACTLY ONCE, by
    # `BottleTrackingService.release_group_history_to_address`, and never changes.
    #
    # WHY IT EXISTS. §7.1/§7.3 deliberately do NOT re-stamp a DEPARTED member's
    # frozen references — NULLing them would drop the place's history into a
    # departed address's own scope and mint bottles onto someone who left with
    # nothing. So a fine issued to a member who left, or a delivery it recorded,
    # keeps naming a group that later dissolved: a place with no members and no
    # `bottle_balances` row. Settling that fine and correcting that order used to
    # be REFUSED outright (`BOTTLE_SCOPE_UNREACHABLE`,
    # `BOTTLE_CORRECTION_SCOPE_NOT_LIVE`) because booking to the frozen scope
    # would re-mint precisely the orphan §7.3's dissolve exists to eliminate.
    # This column is how those two operations find the surviving scope instead.
    #
    # It names an ADDRESS, never a group, and the address's LIVE scope is
    # re-resolved at read time — so a survivor that has since joined a new place
    # forwards to THAT place, and the pointer never has to chain or be rewritten.
    #
    # ondelete SET NULL: an ungrouped survivor is deletable (only a GROUPED
    # address is fenced, see `assert_address_not_in_place_group`). Losing the
    # address means losing the destination, and the two operations fall back to
    # the refusal they had before — which is honest, and is why the refusals stay
    # in the code rather than being replaced.
    dissolved_onto_address_id = Column(
        Integer,
        ForeignKey(
            "addresses.id",
            name="fk_address_groups_dissolved_onto_address",
            ondelete="SET NULL",
        ),
        nullable=True,
    )


class CustomerLinkEvent(db.Model, TimestampMixin):
    """Append-only audit of every link/unlink/group/dismiss/set-primary action."""

    __tablename__ = "customer_link_events"
    __table_args__ = (Index("idx_customer_link_events_canonical", "canonical_customer_id"),)

    id = Column(Integer, primary_key=True)
    # link | unlink | add_to_group | eject | set_primary | dismiss
    # | create_place_group | add_to_place_group | remove_from_place_group
    # | dismiss_place_suggestion
    event_type = Column(String(30), nullable=False)
    canonical_customer_id = Column(
        Integer, ForeignKey("canonical_customers.id", name="fk_customer_link_events_canonical"), nullable=True
    )
    acting_admin_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_customer_link_events_admin", ondelete="SET NULL"),
        nullable=True,
    )
    member_user_ids = Column(JSON, nullable=False, default=list)
    reason = Column(String(500), nullable=False, default="")
    # Structured audit payload. `reason` is String(500) and already carries the
    # "[group N] " scope prefix, so it cannot hold a list of re-scoped ledger
    # entry ids (spec §7.2). Nullable: every pre-existing row has none.
    event_metadata = Column(JSON, nullable=True, default=dict)


class CustomerDistinctPair(db.Model, TimestampMixin):
    """Negative assertion: two accounts confirmed by an admin to be DIFFERENT people.

    Hard constraint at link time (blocks transitive over-merge) and suppresses
    re-suggestion until a new signal appears (signal_fingerprint change).
    Stored order-normalized (user_id_low < user_id_high).
    """

    __tablename__ = "customer_distinct_pairs"
    __table_args__ = (UniqueConstraint("user_id_low", "user_id_high", name="uq_customer_distinct_pairs"),)

    id = Column(Integer, primary_key=True)
    # CASCADE, not SET NULL: both participants are NOT NULL, and "A is not B" has
    # no meaning once A is gone.
    user_id_low = Column(
        Integer,
        ForeignKey("users.id", name="fk_customer_distinct_pairs_low", ondelete="CASCADE"),
        nullable=False,
    )
    user_id_high = Column(
        Integer,
        ForeignKey("users.id", name="fk_customer_distinct_pairs_high", ondelete="CASCADE"),
        nullable=False,
    )
    dismissed_by_admin_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_customer_distinct_pairs_admin", ondelete="SET NULL"),
        nullable=True,
    )
    signal_fingerprint = Column(String(64), nullable=True)


class PlaceSuggestionDismissal(db.Model, TimestampMixin):
    """Negative assertion for PLACE-GROUP suggestions ONLY.

    "These two addresses are not the same place." Order-normalized
    (address_id_low < address_id_high, app convention). NEVER writes or reads
    CustomerDistinctPair — dismissing a place suggestion must not block linking
    people, and vice versa. Re-surfaced when signal_fingerprint changes.
    The suggestion engine lands in Plan 2c; this is the registry only.
    """

    __tablename__ = "place_suggestion_dismissals"
    __table_args__ = (UniqueConstraint("address_id_low", "address_id_high", name="uq_place_suggestion_dismissals"),)

    id = Column(Integer, primary_key=True)
    address_id_low = Column(
        Integer,
        ForeignKey("addresses.id", name="fk_place_suggestion_dismissals_low", ondelete="CASCADE"),
        nullable=False,
    )
    address_id_high = Column(
        Integer,
        ForeignKey("addresses.id", name="fk_place_suggestion_dismissals_high", ondelete="CASCADE"),
        nullable=False,
    )
    dismissed_by_admin_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_place_suggestion_dismissals_admin", ondelete="SET NULL"),
        nullable=True,
    )
    signal_fingerprint = Column(String(64), nullable=True)
