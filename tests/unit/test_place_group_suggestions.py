"""Place-group suggestion engine (Plan 2c, spec §10).

The inverse signal of the LINK channel: co-location is a POSITIVE signal
("different people at the same place"), whereas `get_link_suggestions`
deliberately DAMPENS shared geolocations. The two channels must never
contaminate each other — in particular a place-suggestion dismissal must
never write a `CustomerDistinctPair` (that is the link channel's "not the
same person" assertion, which hard-blocks linking).
"""
from datetime import datetime, UTC

import pytest

from business_app import db as _db
from business_app.models.customer_link import (
    CustomerDistinctPair,
    CustomerLinkEvent,
    PlaceSuggestionDismissal,
)
from business_app.models.user import User, UserAddress
from business_app.services import customer_link_service as cls_module
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.password_security import hash_password
from shared.enums import EntitySubtype, UserRole, UserStatus, UserType

# Tashkent-center coordinates (inside TASHKENT_POLYGON — the UserAddress
# before_insert zone listener rejects out-of-zone coords).
LAT, LNG = 41.3111, 69.2797


def _customer(db, email, phone, *, grocery=False):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name=email.split("@")[0],
             user_type=UserType.ENTITY if grocery else UserType.INDIVIDUAL,
             entity_subtype=EntitySubtype.GROCERY_STORE if grocery else None,
             role=UserRole.CUSTOMER, status=UserStatus.ACTIVE, is_verified=True,
             created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


def _address(db, user, lat, lng, title="work"):
    a = UserAddress(user_id=user.id, title=title, full_address="Test addr",
                    latitude=lat, longitude=lng)
    db.session.add(a); db.session.commit()
    return a


@pytest.mark.unit
class TestPlaceGroupSuggestions:
    def test_two_customers_at_same_point_suggested(self, db):
        u1 = _customer(db, "a@example.com", "+998900000001")
        u2 = _customer(db, "b@example.com", "+998900000002")
        a1 = _address(db, u1, LAT, LNG)
        a2 = _address(db, u2, LAT + 0.00003, LNG)  # 3.332 m — inside the 10 m radius
        svc = CustomerLinkService()
        suggestions = svc.get_place_group_suggestions()
        assert len(suggestions) == 1
        s = suggestions[0]
        assert sorted(s["address_ids"]) == sorted([a1.id, a2.id])
        assert s["distinct_customer_count"] == 2
        member_uids = {m["user_id"] for m in s["members"]}
        assert member_uids == {u1.id, u2.id}

    def test_grouped_and_grocery_addresses_excluded(self, db):
        u1 = _customer(db, "a@example.com", "+998900000001")
        u2 = _customer(db, "b@example.com", "+998900000002")
        u3 = _customer(db, "g@example.com", "+998900000003", grocery=True)
        a1 = _address(db, u1, LAT, LNG)
        a2 = _address(db, u2, LAT, LNG)
        _address(db, u3, LAT, LNG)  # grocery owner — never suggested
        svc = CustomerLinkService()
        # Group the first two — the point drops below 2 ungrouped distinct customers.
        svc.create_place_group([a1.id, a2.id], acting_admin_id=u1.id, reason="test group")
        assert svc.get_place_group_suggestions() == []

    def test_dismiss_suppresses_and_new_signal_resurfaces(self, db):
        u1 = _customer(db, "a@example.com", "+998900000001")
        u2 = _customer(db, "b@example.com", "+998900000002")
        a1 = _address(db, u1, LAT, LNG)
        a2 = _address(db, u2, LAT, LNG)
        svc = CustomerLinkService()
        admin = _customer(db, "adm@example.com", "+998900000009")
        svc.dismiss_place_suggestion(a1.id, a2.id, acting_admin_id=admin.id, reason="not coworkers")
        assert svc.get_place_group_suggestions() == []
        # New signal: a third customer appears at the point -> fingerprint changes -> resurfaces.
        u3 = _customer(db, "c@example.com", "+998900000004")
        _address(db, u3, LAT, LNG)
        resurfaced = svc.get_place_group_suggestions()
        assert len(resurfaced) == 1
        assert resurfaced[0]["distinct_customer_count"] == 3

    def test_dismiss_never_writes_customer_distinct_pair(self, db):
        u1 = _customer(db, "a@example.com", "+998900000001")
        u2 = _customer(db, "b@example.com", "+998900000002")
        a1 = _address(db, u1, LAT, LNG)
        a2 = _address(db, u2, LAT, LNG)
        admin = _customer(db, "adm@example.com", "+998900000009")
        svc = CustomerLinkService()
        row = svc.dismiss_place_suggestion(a1.id, a2.id, acting_admin_id=admin.id, reason="different firms")
        assert isinstance(row, PlaceSuggestionDismissal)
        assert CustomerDistinctPair.query.count() == 0
        event = CustomerLinkEvent.query.filter_by(event_type="dismiss_place_suggestion").one()
        assert event.acting_admin_id == admin.id
        assert event.reason == "different firms"

    def test_dismiss_is_idempotent_and_still_writes_no_distinct_pair(self, db):
        """Re-dismissing the same pair upserts the single normalized row.

        Order-normalization means (a, b) and (b, a) are the same assertion.
        The link channel's negative registry must stay empty either way.
        """
        u1 = _customer(db, "a@example.com", "+998900000001")
        u2 = _customer(db, "b@example.com", "+998900000002")
        a1 = _address(db, u1, LAT, LNG)
        a2 = _address(db, u2, LAT, LNG)
        admin = _customer(db, "adm@example.com", "+998900000009")
        svc = CustomerLinkService()
        first = svc.dismiss_place_suggestion(a1.id, a2.id, acting_admin_id=admin.id, reason="one")
        second = svc.dismiss_place_suggestion(a2.id, a1.id, acting_admin_id=admin.id, reason="two")
        assert first.id == second.id
        assert PlaceSuggestionDismissal.query.count() == 1
        assert second.address_id_low == min(a1.id, a2.id)
        assert second.address_id_high == max(a1.id, a2.id)
        assert CustomerDistinctPair.query.count() == 0

    def test_link_channel_dismissal_does_not_suppress_place_suggestions(self, db):
        """The two channels are independent: a CustomerDistinctPair ("these are
        different people") is exactly the coworker case and must NOT hide a
        place-group suggestion."""
        u1 = _customer(db, "a@example.com", "+998900000001")
        u2 = _customer(db, "b@example.com", "+998900000002")
        admin = _customer(db, "adm@example.com", "+998900000009")
        _address(db, u1, LAT, LNG)
        _address(db, u2, LAT, LNG)
        svc = CustomerLinkService()
        svc.dismiss_suggestion(u1.id, u2.id, actor_admin_id=admin.id)
        assert CustomerDistinctPair.query.count() == 1
        assert len(svc.get_place_group_suggestions()) == 1

    def test_user_scoped_suggestions_only_include_users_points(self, db):
        u1 = _customer(db, "a@example.com", "+998900000001")
        u2 = _customer(db, "b@example.com", "+998900000002")
        u3 = _customer(db, "c@example.com", "+998900000003")
        u4 = _customer(db, "d@example.com", "+998900000004")
        _address(db, u1, LAT, LNG)
        _address(db, u2, LAT, LNG)
        # A second, unrelated co-located pair ~1 km away.
        _address(db, u3, LAT + 0.01, LNG)
        _address(db, u4, LAT + 0.01, LNG)
        svc = CustomerLinkService()
        assert len(svc.get_place_group_suggestions()) == 2
        mine = svc.get_place_group_suggestions(user_id=u1.id)
        assert len(mine) == 1
        assert u1.id in {m["user_id"] for m in mine[0]["members"]}


@pytest.mark.unit
class TestBboxPrefilterNarrowsCandidates:
    """The prefilter must actually shrink the scanned set, not merely leave
    results correct — the candidate scan is the hot path this exists for."""

    def test_bbox_prefilter_excludes_out_of_box_rows(self, db):
        u1 = _customer(db, "a@example.com", "+998900000001")
        u2 = _customer(db, "b@example.com", "+998900000002")
        u3 = _customer(db, "far@example.com", "+998900000003")
        a1 = _address(db, u1, LAT, LNG)
        a2 = _address(db, u2, LAT + 0.0003, LNG)   # ~33 m — inside
        a3 = _address(db, u3, LAT + 0.05, LNG)     # ~5.5 km — outside

        base = _db.session.query(UserAddress.id)
        assert {r[0] for r in base.all()} == {a1.id, a2.id, a3.id}

        narrowed = CustomerLinkService._bbox_prefilter(base, [(LAT, LNG)], 0.1)
        assert {r[0] for r in narrowed.all()} == {a1.id, a2.id}

    def test_link_suggestions_skip_distance_math_for_out_of_box_rows(self, db, monkeypatch):
        u1 = _customer(db, "a@example.com", "+998900000001")
        u2 = _customer(db, "b@example.com", "+998900000002")
        u3 = _customer(db, "far@example.com", "+998900000003")
        _address(db, u1, LAT, LNG)
        _address(db, u2, LAT + 0.0003, LNG)
        far_lat = LAT + 0.05
        _address(db, u3, far_lat, LNG)

        calls = []
        original = cls_module.calculate_distance

        def _spy(lat1, lng1, lat2, lng2):
            calls.append((lat1, lng1, lat2, lng2))
            return original(lat1, lng1, lat2, lng2)

        monkeypatch.setattr(cls_module, "calculate_distance", _spy)
        results = CustomerLinkService().get_link_suggestions(u1.id)

        assert u2.id in {r["user_id"] for r in results}
        # The far row was dropped by the bbox BEFORE any haversine call.
        assert calls, "expected at least one distance computation for the near candidate"
        assert all(round(c[2], 4) != round(far_lat, 4) for c in calls)

    def test_place_group_user_scoped_query_is_bbox_narrowed(self, db, monkeypatch):
        u1 = _customer(db, "a@example.com", "+998900000001")
        u2 = _customer(db, "b@example.com", "+998900000002")
        u3 = _customer(db, "c@example.com", "+998900000003")
        u4 = _customer(db, "d@example.com", "+998900000004")
        _address(db, u1, LAT, LNG)
        _address(db, u2, LAT, LNG)
        _address(db, u3, LAT + 0.01, LNG)
        _address(db, u4, LAT + 0.01, LNG)

        # A1.1 / plan invariant 9: the PLACE channel no longer draws a bbox at
        # all, so this test now pins its ABSENCE. (The name is kept because only
        # the spy scaffolding and its two count assertions were authorised to
        # move — see baseline-a1/grid-assumptions.md §3.)
        #
        # Why the bbox had to go: connected components are transitively
        # unbounded, so any box drawn around the anchor can truncate a chain —
        # and `dismiss_place_suggestion`, which never had an anchor or a box,
        # would then compute a DIFFERENT component and stamp a fingerprint the
        # suggestion engine never produces, silently voiding the admin's "not
        # the same place" (plan E19). Both paths now cluster the same unanchored
        # pool; anchoring is a post-hoc filter on finished components.
        #
        # `_bbox_prefilter` itself survives — the LINK channel still calls it
        # (customer_link_service.py:2139), pinned by the two tests around this
        # one. Only the PLACE call site went.
        seen = []
        original = CustomerLinkService._bbox_prefilter

        def _spy(query, coords, radius_km):
            seen.append((coords, radius_km))
            return original(query, coords, radius_km)

        monkeypatch.setattr(CustomerLinkService, "_bbox_prefilter", staticmethod(_spy))
        assert len(CustomerLinkService().get_place_group_suggestions(user_id=u1.id)) == 1
        assert seen == [], (
            "the PLACE channel must not draw a bbox — it truncates transitive "
            "components and silently voids dismissals (E19)"
        )


@pytest.mark.unit
class TestLinkSuggestionBboxRegression:
    def test_link_suggestions_still_find_near_and_exclude_far(self, db):
        u1 = _customer(db, "a@example.com", "+998900000001")
        u2 = _customer(db, "b@example.com", "+998900000002")
        u3 = _customer(db, "far@example.com", "+998900000003")
        _address(db, u1, LAT, LNG)
        _address(db, u2, LAT + 0.0003, LNG)      # ~33 m — inside 50 m radius
        _address(db, u3, LAT + 0.05, LNG)        # ~5.5 km — outside
        results = CustomerLinkService().get_link_suggestions(u1.id)
        ids = {r["user_id"] for r in results}
        assert u2.id in ids
        assert u3.id not in ids

    def test_shared_geo_count_unchanged_for_in_radius_points(self, db):
        """The bbox margin must cover the ~11 m rounding grid so the LINK
        channel's shared-office dampening still sees every co-located account."""
        target = _customer(db, "t@example.com", "+998900000001")
        _address(db, target, LAT, LNG)
        coworkers = []
        for i in range(6):
            c = _customer(db, f"c{i}@example.com", f"+99890010000{i}")
            _address(db, c, LAT, LNG)
            coworkers.append(c)
        by_id = {s["user_id"]: s for s in CustomerLinkService().get_link_suggestions(target.id)}
        for c in coworkers:
            assert by_id[c.id]["shared_geo_customer_count"] >= 6
