"""A1 — the place-suggestion radius is configurable, defaults to 10 m, and is a
TRUE distance, not the old ~11 m snap-to-grid.

Plan decisions E17 (metres, default 10.0), E18 (connected components) and E19
(one clusterer shared with the dismissal fingerprint).

ARITHMETIC USED THROUGHOUT — measured in the business_app container with the
repo's geo SSOT (``business_app/utils/helpers.py:80-82`` -> geopy geodesic) at
the fixture latitude ``41.3111`` that every place-suggestion test uses:

    0.0001 deg latitude  = 11.1060 m      0.0001 deg longitude = 8.3738 m

so, per degree of latitude, 1 deg = 111 060 m. Every offset below is stated as
degrees AND as the metres it actually measures, because the entire point of
this task is a distance and "about 3 m" is unfalsifiable.
"""

import importlib
import time
from datetime import UTC, datetime

import pytest

from business_app.models.user import User, UserAddress
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserStatus, UserType

# Tashkent-center coordinates (inside TASHKENT_POLYGON — the UserAddress
# before_insert zone listener rejects out-of-zone coords). Same pin the
# existing place-suggestion suite uses.
LAT, LNG = 41.3111, 69.2797

# 0.000054 deg lat = 5.997 m  -> inside the 10 m radius.
# Anchored at 41.31106 rather than at LAT so BOTH pins round to the SAME
# 0.0001-degree grid cell (41.3111): this test must isolate "inside the radius",
# not accidentally re-test the cell-boundary case that has its own test below.
NEAR_A_LAT = 41.31106
NEAR_B_LAT = 41.311114  # +0.000054 deg = 5.997 m

# 0.00036 deg lat = 39.982 m -> outside the 10 m radius, inside 50 m.
FAR_LAT = LAT + 0.00036

# THE RADIUS BRACKET. Without these two, the suite's nearest in-radius pin is
# 5.997 m and its nearest out-of-radius pin is 22.2 m (test 12's closest lattice
# neighbour), so the EFFECTIVE radius is only constrained to somewhere in
# (5.997 m, 22.2 m) -- a 2x unit-conversion slip at
# `_place_suggestion_radius_km` (`/ 500.0` instead of `/ 1000.0`) passes every
# other test in this file. Verified in-container: at radius_km = 0.020 every
# expected component set below is still produced. These two pins close the band
# to +/-5% of 10 m, so any conversion error bigger than that fails immediately.
JUST_INSIDE = 0.0000855  # = 9.4956 m -> MUST be suggested at the 10 m default
JUST_OUTSIDE = 0.0000945  # = 10.4952 m -> MUST NOT be suggested at the default

# 0.000072 deg lat = 7.996 m -> one hop of the transitive chain.
HOP = 0.000072

# The cell-boundary straddle. Both axes cross BOTH boundaries that matter:
#   * the OLD engine's round(x, 4) grid boundary  (lat 41.31115 / lng 69.27975)
#   * the NEW clusterer's coarse pre-bucket boundary at radius/80 = 0.000125 deg
#     (lat 41.311125 / lng 69.27975)
# so this pair is the pin on E17's good direction AND on invariant 9's
# 8-neighbour sweep. Separation: 0.00004 deg lat (4.442 m) + 0.00004 deg lng
# (3.350 m) = 5.564 m geodesic — comfortably inside the 10 m radius.
STRADDLE_A = (41.31112, 69.27973)  # round() -> (41.3111, 69.2797); cell (330488, 554237)
STRADDLE_B = (41.31116, 69.27977)  # round() -> (41.3112, 69.2798); cell (330489, 554238)


def _customer(db, email, phone):
    u = User(
        email=email,
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="T",
        last_name=email.split("@")[0],
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(u)
    db.session.commit()
    return u


def _address(db, user, lat, lng, title="work"):
    a = UserAddress(
        user_id=user.id, title=title, full_address="Test addr", latitude=lat, longitude=lng
    )
    db.session.add(a)
    db.session.commit()
    return a


def _pair(db, coord_a, coord_b, *, start=1):
    """Two ungrouped INDIVIDUAL/CUSTOMER addresses owned by DIFFERENT people."""
    u1 = _customer(db, f"p{start}@example.com", f"+99890000{start:04d}")
    u2 = _customer(db, f"p{start + 1}@example.com", f"+99890000{start + 1:04d}")
    a = _address(db, u1, *coord_a)
    b = _address(db, u2, *coord_b)
    return u1, u2, a, b


@pytest.fixture
def restore_business_config():
    """Undo the process-wide damage `importlib.reload` does.

    Mirrors `tests/unit/test_place_cod_collection_gate.py:13-38`. `monkeypatch`
    restores `os.environ`, but nothing puts the MODULE back: `reload` rebinds
    the constant on the live module object, so a leaked 25.0 would break
    `test_flask_config_mirrors_the_shared_literal`, whose session-scoped `app`
    was bound from the true configured value.
    """
    import shared.business_config as bc

    original = bc.PLACE_SUGGESTION_RADIUS_M
    yield
    importlib.reload(bc)
    bc.PLACE_SUGGESTION_RADIUS_M = original


@pytest.mark.unit
def test_radius_defaults_to_ten_metres(monkeypatch, restore_business_config):
    monkeypatch.delenv("PLACE_SUGGESTION_RADIUS_M", raising=False)
    import shared.business_config as bc

    assert importlib.reload(bc).PLACE_SUGGESTION_RADIUS_M == 10.0


@pytest.mark.unit
def test_radius_is_env_overridable(monkeypatch, restore_business_config):
    monkeypatch.setenv("PLACE_SUGGESTION_RADIUS_M", "25")
    import shared.business_config as bc

    assert importlib.reload(bc).PLACE_SUGGESTION_RADIUS_M == 25.0


@pytest.mark.unit
def test_flask_config_mirrors_the_shared_literal(app):
    """Single-default rule: one literal, in business_config.py."""
    from shared import business_config

    assert app.config["PLACE_SUGGESTION_RADIUS_M"] == business_config.PLACE_SUGGESTION_RADIUS_M


@pytest.mark.unit
def test_two_addresses_inside_the_radius_are_suggested(app, db):
    """~6 m apart, DIFFERENT owners, both ungrouped => one candidate.

    1 deg lat = 111 060 m, so 0.000054 deg = 5.997 m — inside 10 m.
    """
    _u1, _u2, a, b = _pair(db, (NEAR_A_LAT, LNG), (NEAR_B_LAT, LNG))

    results = CustomerLinkService().get_place_group_suggestions(limit=20)

    assert len(results) == 1
    assert results[0]["address_ids"] == sorted([a.id, b.id])
    assert results[0]["distinct_customer_count"] == 2


@pytest.mark.unit
def test_two_addresses_outside_the_radius_are_not_suggested(app, db):
    """~40 m apart at the default 10 m => no candidate. Under the old grid this
    was already true; the test exists so the radius is proven to BITE.

    0.00036 deg lat = 39.982 m — 4x the radius.
    """
    _pair(db, (LAT, LNG), (FAR_LAT, LNG))

    assert CustomerLinkService().get_place_group_suggestions(limit=20) == []


@pytest.mark.unit
def test_a_pair_just_inside_the_radius_is_suggested(app, db):
    """🔴 LOWER BRACKET on the EFFECTIVE radius. Do not delete it.

    9.4956 m -- 95% of the 10 m default. Paired with
    `test_a_pair_just_outside_the_radius_is_not_suggested` (10.4952 m) it pins
    what the clusterer actually COMPARES AGAINST, not merely what the constant
    says: `test_radius_defaults_to_ten_metres` asserts on
    `business_config.PLACE_SUGGESTION_RADIUS_M`, which a broken metres->km
    conversion never touches.

    0.0000855 deg lat = 9.4956 m.
    """
    _u1, _u2, a, b = _pair(db, (LAT, LNG), (LAT + JUST_INSIDE, LNG))

    results = CustomerLinkService().get_place_group_suggestions(limit=20)

    assert len(results) == 1, "9.50 m is inside a 10 m radius"
    assert results[0]["address_ids"] == sorted([a.id, b.id])


@pytest.mark.unit
def test_a_pair_just_outside_the_radius_is_not_suggested(app, db):
    """🔴 UPPER BRACKET on the EFFECTIVE radius. Do not delete it.

    10.4952 m -- 105% of the 10 m default. A `/ 500.0` (or a missing
    `/ 1000.0`) in `_place_suggestion_radius_km` makes this pair a candidate
    and this test the only one in the file that notices.

    0.0000945 deg lat = 10.4952 m.
    """
    _pair(db, (LAT, LNG), (LAT + JUST_OUTSIDE, LNG))

    assert CustomerLinkService().get_place_group_suggestions(limit=20) == [], (
        "10.50 m is outside a 10 m radius"
    )


@pytest.mark.unit
def test_a_boundary_straddling_pair_is_now_suggested(app, db):
    """THE GRID REGRESSION, in the good direction (E17). Two pins ~5.6 m apart
    placed either side of a 0.0001-degree cell boundary: the old clusterer put
    them in different buckets and suggested nothing; a radius catches them.

    (41.31112, 69.27973) rounds to (41.3111, 69.2797);
    (41.31116, 69.27977) rounds to (41.3112, 69.2798) — different grid cells,
    yet only 0.00004 deg lat (4.442 m) + 0.00004 deg lng (3.350 m) = 5.564 m
    apart. They also sit in different COARSE pre-bucket cells
    (radius/80 = 0.000125 deg), so this is simultaneously the pin on invariant
    9's 8-neighbour sweep.
    """
    _u1, _u2, a, b = _pair(db, STRADDLE_A, STRADDLE_B)

    results = CustomerLinkService().get_place_group_suggestions(limit=20)

    assert len(results) == 1, "a radius has no cell boundaries to fall through"
    assert results[0]["address_ids"] == sorted([a.id, b.id])


@pytest.mark.unit
def test_widening_the_radius_widens_the_candidate_set(app, db, monkeypatch):
    """Configurability that actually does something: the ~40 m pair above
    becomes a candidate at PLACE_SUGGESTION_RADIUS_M=50."""
    _u1, _u2, a, b = _pair(db, (LAT, LNG), (FAR_LAT, LNG))  # 39.982 m apart

    service = CustomerLinkService()
    assert service.get_place_group_suggestions(limit=20) == []

    monkeypatch.setitem(app.config, "PLACE_SUGGESTION_RADIUS_M", 50.0)
    widened = service.get_place_group_suggestions(limit=20)

    assert len(widened) == 1
    assert widened[0]["address_ids"] == sorted([a.id, b.id])


@pytest.mark.unit
def test_transitive_chain_is_one_candidate_not_two(app, db):
    """E18. A-B 8 m, B-C 8 m, A-C ~16 m => ONE candidate containing all three,
    so the admin sees one physical place, not two overlapping suggestions.

    HOP = 0.000072 deg lat = 7.996 m; A-C = 0.000144 deg = 15.993 m, which is
    OUTSIDE the 10 m radius and INSIDE the component.
    """
    u3 = _customer(db, "p3@example.com", "+998900000003")
    _u1, _u2, a, b = _pair(db, (LAT, LNG), (LAT + HOP, LNG))
    c = _address(db, u3, LAT + 2 * HOP, LNG)

    results = CustomerLinkService().get_place_group_suggestions(limit=20)

    assert len(results) == 1, "connected components, not pairs"
    assert results[0]["address_ids"] == sorted([a.id, b.id, c.id])
    assert results[0]["distinct_customer_count"] == 3


@pytest.mark.unit
def test_a_dismissal_still_suppresses_under_radius_clustering(app, db, admin_user):
    """🔴 E19 — THE COUPLING TEST. Do not delete it.

    dismiss_place_suggestion recomputes the point's membership itself
    (customer_link_service.py:2344-2362) and its own comment warns that if that
    recomputation and the clusterer ever disagree, the dismissal SILENTLY
    NO-OPS. Dismiss a co-located pair, then re-query: it must not come back.
    """
    _u1, _u2, a, b = _pair(db, (NEAR_A_LAT, LNG), (NEAR_B_LAT, LNG))  # 5.997 m

    service = CustomerLinkService()
    assert len(service.get_place_group_suggestions(limit=20)) == 1

    service.dismiss_place_suggestion(a.id, b.id, admin_user.id, "not the same place")

    assert service.get_place_group_suggestions(limit=20) == [], (
        "the dismissal's fingerprint and the clusterer disagree about the point"
    )


@pytest.mark.unit
def test_a_new_address_at_the_point_resurfaces_a_dismissal(app, db, admin_user):
    """The other half of E19/spec 10: the fingerprint is the sorted address-id
    set, so a genuinely new signal must revive a dismissed suggestion."""
    _u1, _u2, a, b = _pair(db, (NEAR_A_LAT, LNG), (NEAR_B_LAT, LNG))  # 5.997 m

    service = CustomerLinkService()
    service.dismiss_place_suggestion(a.id, b.id, admin_user.id, "not the same place")
    assert service.get_place_group_suggestions(limit=20) == []

    # A THIRD ungrouped address at the same point, owned by a THIRD customer —
    # a new signal, so a new fingerprint. 0.000027 deg = 2.999 m from NEAR_A.
    u3 = _customer(db, "p3@example.com", "+998900000003")
    c = _address(db, u3, NEAR_A_LAT + 0.000027, LNG)

    revived = service.get_place_group_suggestions(limit=20)

    assert len(revived) == 1
    assert revived[0]["address_ids"] == sorted([a.id, b.id, c.id])


@pytest.mark.unit
def test_an_anchored_query_and_the_dismissal_agree_on_a_transitive_chain(app, db, admin_user):
    """🔴 E18 x E19 — THE INTERACTION TEST. Do not delete it.

    Connected components are transitively unbounded, so a chain member can sit
    far outside any bbox drawn around the anchor. Under the earlier "keep the
    bbox, widen it by the radius" design, A-B-C-D at 8 m hops puts D ~24 m from
    A while the margin is 10 m: D is EXCLUDED from the anchored computation and
    INCLUDED in the dismissal's (which has no anchor and never had a bbox), the
    two fingerprints differ, and the admin's dismissal is silently forgotten.

    Invariant 9 removes the bbox so both paths cluster the same unanchored pool.

    HOP = 0.000072 deg lat = 7.996 m per hop; A-D = 0.000216 deg = 23.989 m.
    """
    owners = [_customer(db, f"chain{i}@example.com", f"+99890010000{i}") for i in range(4)]
    a, b, c, d = [_address(db, owners[i], LAT + i * HOP, LNG) for i in range(4)]
    owner_a = owners[0]

    service = CustomerLinkService()
    before = service.get_place_group_suggestions(limit=20, user_id=owner_a.id)

    assert len(before) == 1, "the anchored path must not truncate the component"
    assert before[0]["address_ids"] == sorted([a.id, b.id, c.id, d.id])

    service.dismiss_place_suggestion(a.id, b.id, admin_user.id, "not the same place")

    assert service.get_place_group_suggestions(limit=20, user_id=owner_a.id) == [], (
        "the anchored and dismissal paths computed different components"
    )


def _bulk_customers(db, prefix, count, phone_block):
    """`count` committed INDIVIDUAL/CUSTOMER rows, cheaply.

    A literal password hash, NOT hash_password(): bcrypt at the repo's cost
    factor would make the FIXTURE dominate a wall-clock test about clustering.
    """
    owners = [
        User(
            email=f"{prefix}{i}@example.com",
            phone=f"+998{phone_block}{i:05d}",
            password_hash="$2b$12$fixture.only.not.a.real.bcrypt.hash.value",
            first_name=prefix[0].upper(),
            last_name=str(i),
            user_type=UserType.INDIVIDUAL,
            role=UserRole.CUSTOMER,
            status=UserStatus.ACTIVE,
            is_verified=True,
            created_at=datetime.now(UTC),
        )
        for i in range(count)
    ]
    db.session.add_all(owners)
    db.session.commit()
    return owners


@pytest.mark.unit
def test_the_clusterer_stays_within_its_wall_clock_budget(app, db):
    """🔴 THE O(n^2) ALARM. Do not delete it, and do not relax the budget without
    saying so in the report.

    A NAIVE pairwise geodesic scan is O(n^2) -- at 2 000 addresses that is ~2 M
    geopy.geodesic calls, over a minute inside a gunicorn worker, on EVERY
    estate-wide suggestion request (A1.2's GET /admin/place-group-suggestions,
    which passes user_id=None) and on EVERY admin "Not the same place" click.
    The coarse-cell pre-bucket (invariant 9) is what keeps it linear-ish; this
    test is what proves the pre-bucket is actually there.

    BUDGET: ~2 000 ungrouped addresses, clustered in UNDER 3.0 SECONDS wall
    clock, measured with time.perf_counter() around the service call only (not
    fixture setup). The number is deliberately loose -- it is an order-of-
    magnitude alarm against an O(n^2) regression, not a benchmark. If it lands
    anywhere near 3 s, something is wrong -- report the number.

    🔴 THE CROWD IS 500 *DISTINCT* PINS, NOT 500 COPIES OF ONE PIN, AND THAT IS
    LOAD-BEARING. The clusterer skips pairs already in one component, so a crowd
    of byte-identical coordinates short-circuits on the first `a` and costs O(m)
    -- an earlier version of this test crowded 200 identical pins, ran in
    0.037 s, and could not detect ANY regression in the in-cell O(m^2) term.
    Distinct pins keep every one of the m^2 union-find lookups on the clock.
    Measured in-container on this exact fixture: 0.097 s as shipped; 5.90 s with
    the already-connected skip removed (RED); 95.8 s with the coarse-cell
    pre-bucket removed (RED). Both regressions now breach the budget.

    Fixture shape (2 030 addresses):
      * 500 addresses at 500 DISTINCT coordinates on a 25 x 20 grid stepping
        0.0000036 deg lat (0.400 m) x 0.0000045 deg lng (0.377 m) -- a 9.6 m x
        7.2 m box, so all 500 are ONE connected component under a 10 m radius
        and all land in one 3 x 3 cell neighbourhood. Owned by 50 customers
        (10 addresses each), because the m^2 term counts ADDRESSES and the
        owner count only has to clear the >= 2 distinct-customer bar.
      * 1 530 addresses on a 45 x 34 lattice spaced 0.0004 deg lat (44.4 m) x
        0.0005 deg lng (41.9 m), every one a singleton component under a 10 m
        radius (nearest lattice pin to the crowd is 12.6 m away). Verified
        in-container: all 2 030 fixture points lie inside TASHKENT_POLYGON.
    """
    crowd_owners = _bulk_customers(db, "hot", 50, "9001")
    scatter_owners = _bulk_customers(db, "scatter", 10, "9002")

    addresses = []
    k = 0
    for i in range(25):
        for j in range(20):
            addresses.append(
                UserAddress(
                    user_id=crowd_owners[k % 50].id,
                    title="hq",
                    full_address="One building",
                    latitude=LAT + i * 0.0000036,
                    longitude=LNG + j * 0.0000045,
                )
            )
            k += 1
    n = 0
    for i in range(45):
        for j in range(34):
            addresses.append(
                UserAddress(
                    user_id=scatter_owners[n % 10].id,
                    title="scatter",
                    full_address="Lattice",
                    latitude=LAT - 0.009 + i * 0.0004,
                    longitude=LNG - 0.010 + j * 0.0005,
                )
            )
            n += 1
    assert len(addresses) == 2030
    db.session.add_all(addresses)
    db.session.commit()

    started = time.perf_counter()
    results = CustomerLinkService().get_place_group_suggestions(limit=20)
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0, f"clusterer took {elapsed:.2f}s over 2030 addresses"
    # And it still did its job, so a fast no-op cannot pass.
    assert results, "the 500-address building must yield at least one candidate"
    assert results[0]["distinct_customer_count"] == 50
    assert len(results[0]["address_ids"]) == 500, "one component, not 500 singletons"


@pytest.mark.unit
def test_a_crowd_of_identical_coordinates_does_not_blow_up_the_clusterer(app, db):
    """🔴 THE DEGENERATE-COORDINATE ALARM. Do not delete it.

    A geocoder falling back to a building/district/city centroid is the standard
    way thousands of rows land on ONE byte-identical float pair. Those rows all
    sit in a single cell, so the pre-bucket prunes nothing and the pair sweep is
    a flat m^2: measured in-container, 5 000 such rows cost 4.52 s -- OVER this
    file's own 3.0 s budget -- at the 5 000-address estate size the plan states.
    That cost is paid synchronously in a gunicorn worker, on the unanchored
    estate-wide path and on every mount of the admin's place panel.

    The clusterer therefore unions byte-identical coordinates outright and sends
    ONE representative per distinct coordinate into the sweep. Exactly
    partition-preserving (separation 0.0 km <= any positive radius, and an
    outside row is the same distance from every row at that coordinate), so this
    test asserts the FULL component, not just the clock.

    Measured on this exact fixture: 0.003 s with the collapse, 1.85 s without --
    so the 1.0 s budget below is RED without it and ~300x clear with it.
    """
    owners = _bulk_customers(db, "dupe", 30, "9003")
    addresses = [
        UserAddress(
            user_id=owners[i % 30].id,
            title="hq",
            full_address="Geocoder centroid",
            latitude=LAT,
            longitude=LNG,
        )
        for i in range(3000)
    ]
    db.session.add_all(addresses)
    db.session.commit()

    started = time.perf_counter()
    results = CustomerLinkService().get_place_group_suggestions(limit=20)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"clusterer took {elapsed:.2f}s over 3000 identical pins"
    assert len(results) == 1, "one coordinate is one place"
    assert len(results[0]["address_ids"]) == 3000, "the collapse must not drop members"
    assert results[0]["distinct_customer_count"] == 30
