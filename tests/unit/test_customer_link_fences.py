"""Fences around the destructive account merge (`auto_link_accounts`).

`CrossPlatformSyncService.auto_link_accounts` deletes a user row and reparents
FKs onto the other. That is safe for two lone, unclustered accounts, but once
either side already belongs to a `CanonicalCustomer` (Task 1/2 multi-phone
customer-linking), a destructive merge would orphan the OTHER cluster members
still pointing at the deleted user and any address-group entries tied to it.
The non-destructive `CustomerLinkService` is the correct path for clustered
accounts, so `auto_link_accounts` must refuse instead of proceeding.

This file is extended by Task 4 with further fence cases.
"""

from datetime import datetime, UTC

import pytest

from business_app.models.user import User
from business_app.models.customer_link import (
    CanonicalCustomer,
    CustomerDistinctPair,
    CustomerLinkEvent,
)
from business_app.services.cross_platform_sync_service import cross_platform_sync_service
from business_app.services.loyalty_service import LoyaltyService
from shared.enums import UserRole, UserStatus, UserType
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password


def _make_user(db, email, phone, source):
    user = User(
        email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
        first_name="T", last_name="U", user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER, status=UserStatus.ACTIVE, is_verified=True,
        registration_source=source, created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.mark.unit
class TestMergeFence:
    def test_merge_refused_when_primary_is_clustered(self, db):
        web = _make_user(db, "web@example.com", "+998900000001", "web")
        tg = _make_user(db, "tg@example.com", "+998900000002", "telegram")
        canonical = CanonicalCustomer(primary_user_id=web.id)
        db.session.add(canonical)
        db.session.commit()
        web.canonical_customer_id = canonical.id
        db.session.commit()

        result = cross_platform_sync_service.auto_link_accounts(
            primary_user=web, secondary_user=tg, link_type="merge"
        )

        assert result["success"] is False
        assert "canonical" in result["error"].lower()
        # Nothing was deleted.
        assert User.query.get(tg.id) is not None
        assert User.query.get(web.id) is not None

    def test_merge_refused_when_secondary_is_clustered(self, db):
        web = _make_user(db, "web@example.com", "+998900000001", "web")
        tg = _make_user(db, "tg@example.com", "+998900000002", "telegram")
        canonical = CanonicalCustomer(primary_user_id=tg.id)
        db.session.add(canonical)
        db.session.commit()
        tg.canonical_customer_id = canonical.id
        db.session.commit()

        result = cross_platform_sync_service.auto_link_accounts(
            primary_user=web, secondary_user=tg, link_type="merge"
        )

        assert result["success"] is False
        assert "canonical" in result["error"].lower()
        assert User.query.get(tg.id) is not None


@pytest.mark.unit
class TestMergeClearsNewUserFks:
    """The merge deletes the secondary User; nothing may be left pointing at it.

    The Phase-1/2 tables added four NOT NULL / NO ACTION FKs to ``users`` that
    ``_transfer_user_references`` did not cover, so the terminal
    ``db.session.delete(secondary)`` would raise a ForeignKeyViolation on
    Postgres. ``customer_distinct_pairs`` rows are written for exactly the
    account pairs this flow targets, so it is the expected case — and it is
    invisible to this suite's SQLite backend (foreign keys OFF), hence these
    assertions check the ROWS, not the delete.
    """

    def test_distinct_pairs_and_admin_stamps_survive_the_merge(self, db):
        web = _make_user(db, "web@example.com", "+998900000021", "web")
        tg = _make_user(db, "tg@example.com", "+998900000022", "telegram")
        third = _make_user(db, "third@example.com", "+998900000023", "web")

        # The admin whose two accounts are about to be merged.
        db.session.add_all(
            [
                CustomerDistinctPair(user_id_low=min(web.id, tg.id), user_id_high=max(web.id, tg.id)),
                CustomerDistinctPair(user_id_low=min(tg.id, third.id), user_id_high=max(tg.id, third.id)),
                CanonicalCustomer(primary_user_id=tg.id, created_by_admin_id=tg.id),
                CustomerLinkEvent(event_type="link", acting_admin_id=tg.id, member_user_ids=[], reason="x"),
            ]
        )
        db.session.commit()

        result = cross_platform_sync_service.auto_link_accounts(
            primary_user=web, secondary_user=tg, link_type="merge"
        )
        assert result["success"] is True, result.get("error")
        assert User.query.get(tg.id) is None

        # No distinct-pair row still names the deleted account (both columns are
        # NOT NULL, so there is nothing to null out — the rows must be gone).
        assert (
            CustomerDistinctPair.query.filter(
                (CustomerDistinctPair.user_id_low == tg.id) | (CustomerDistinctPair.user_id_high == tg.id)
            ).count()
            == 0
        )

        # Audit rows survive with their stamp moved to the surviving account.
        event = CustomerLinkEvent.query.one()
        assert event.acting_admin_id == web.id
        canonical = CanonicalCustomer.query.one()
        assert canonical.created_by_admin_id == web.id
        # The "face" is NOT repointed at a non-member; it is cleared for
        # CustomerLinkService._refresh_primary to re-elect.
        assert canonical.primary_user_id is None


@pytest.mark.unit
class TestReferralFence:
    def test_referral_refused_within_same_cluster(self, db):
        referrer = _make_user(db, "ref@example.com", "+998900000010", "web")
        referee = _make_user(db, "ree@example.com", "+998900000011", "telegram")
        referrer.referral_code = "REF12345"
        canonical = CanonicalCustomer(primary_user_id=referrer.id)
        db.session.add(canonical)
        db.session.commit()
        referrer.canonical_customer_id = canonical.id
        referee.canonical_customer_id = canonical.id
        db.session.commit()

        with pytest.raises(ValidationError, match="Cannot refer yourself"):
            LoyaltyService().process_referral("REF12345", referee.id)

        # No referral row created, referee not marked as referred.
        db.session.refresh(referee)
        assert referee.referred_by_user_id is None
