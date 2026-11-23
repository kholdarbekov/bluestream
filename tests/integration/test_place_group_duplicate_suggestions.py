"""Same-customer place-group suggestions (spec 2026-11-18-duplicate-address-guard §6.10): one customer's two saved
addresses that may be one place appear in the admin's place-group suggestions as a `same_customer` row, after today's
`shared_place` rows, and "Not the same place" hides exactly the row it was pressed on.

Driven through the routes the admin panels call: `GET /api/v1/admin/users/<id>/place-group-suggestions`
(PlaceGroupPanel), `GET /api/v1/admin/place-group-suggestions` (the Grouped addresses tab, with its `kind` filter)
and `POST /api/v1/admin/place-group-suggestions/dismiss` with the row's first two members, as both panels send it.
The customer keeps a second address near a saved one through `POST /api/v1/auth/addresses` with
`confirm_duplicate`; a shared-place case opens through the staff bot's Delivered tap and is settled through the
admin's resolve route. Every radius is read from `app.config`.
"""

import pytest

from business_app import db as _db
from business_app.models.customer_link import PlaceSuggestionDismissal
from business_app.models.shared_place_case import SHARED_PLACE_OPEN_STATUSES
from business_app.models.user import UserAddress
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.address_helpers import address_display_label
from shared.enums import UserType
from tests.integration.address_guard_geometry import BASE, at
from tests.integration.shared_place_builders import customer, pinned_address
from tests.integration.test_address_duplicate_guard_e2e import bot_pin_body, uy  # noqa: F401  uy is a fixture
from tests.integration.test_shared_place_admin_review_api import _delivered

pytestmark = pytest.mark.integration

FOR_CUSTOMER = "/api/v1/admin/users/{}/place-group-suggestions"
ESTATE = "/api/v1/admin/place-group-suggestions"
DISMISS = "/api/v1/admin/place-group-suggestions/dismiss"
CREATE = "/api/v1/auth/addresses"
RESOLVE = "/api/v1/admin/shared-place-cases/{}/resolve"
KIND_ERROR = "kind must be shared_place or same_customer"


@pytest.fixture
def radii(app):
    """`(place-suggestion radius, duplicate radius)` in metres."""
    return app.config["PLACE_SUGGESTION_RADIUS_M"], app.config["ADDRESS_DUPLICATE_RADIUS_M"]


def _read(client, url, headers, **params):
    response = client.get(url, headers=headers, query_string=params)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _for_customer(client, headers, user_id, **params):
    """PlaceGroupPanel's read for one customer."""
    return _read(client, FOR_CUSTOMER.format(user_id), headers, **params)["suggestions"]


def _estate(client, headers, **params):
    """The Grouped addresses tab's read: no customer anchor."""
    return _read(client, ESTATE, headers, **params)


def _dismiss(client, headers, row):
    """The admin's "Not the same place" on `row`, as both panels send it: the row's first two members."""
    first, second = row["members"][:2]
    response = client.post(
        DISMISS,
        json={"addressIdA": first["address_id"], "addressIdB": second["address_id"], "reason": "not the same place"},
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()


def _keep(client, auth_headers, point, **extra):
    """The customer saves an address near a saved one and confirms it: the bot's pin body with `confirm_duplicate`."""
    response = client.post(CREATE, json=bot_pin_body(point, confirm_duplicate=True, **extra), headers=auth_headers)
    assert response.status_code == 201, response.get_json()
    return _db.session.get(UserAddress, response.get_json()["data"]["address"]["id"])


def _member(address, owner):
    return {
        "address_id": address.id,
        "user_id": owner.id,
        "first_name": owner.first_name,
        "last_name": owner.last_name,
        "phone": owner.phone,
        "title": address.title,
        "full_address": address.full_address,
        "display_label": address_display_label(address),
    }


def _pair_row(a, b, owner, distance_m):
    return {
        "kind": "same_customer",
        "address_ids": [a.id, b.id],
        "members": [_member(a, owner), _member(b, owner)],
        "distance_m": distance_m,
        "distinct_customer_count": 1,
        "signal_fingerprint": CustomerLinkService._point_fingerprint([a.id, b.id]),
    }


def test_a_confirmed_pair_is_suggested_as_a_same_customer_row(
    client, auth_headers, admin_claim_headers, uy, sample_user, radii  # noqa: F811
):
    assert 14 <= radii[1] - 2
    ish = _keep(client, auth_headers, at(14), title="Ish")

    expected = [
        {
            "kind": "same_customer",
            "address_ids": [uy.id, ish.id],
            "members": [
                {
                    "address_id": uy.id,
                    "user_id": sample_user.id,
                    "first_name": "Test",
                    "last_name": "User",
                    "phone": "+998901234567",
                    "title": "Uy",
                    "full_address": "1 Test St, Tashkent",
                    "display_label": "Uy",
                },
                {
                    "address_id": ish.id,
                    "user_id": sample_user.id,
                    "first_name": "Test",
                    "last_name": "User",
                    "phone": "+998901234567",
                    "title": "Ish",
                    "full_address": "15, Chilonzor dahasi",
                    "display_label": "Ish",
                },
            ],
            "distance_m": 14,
            "distinct_customer_count": 1,
            "signal_fingerprint": CustomerLinkService._point_fingerprint([uy.id, ish.id]),
        }
    ]
    assert _for_customer(client, admin_claim_headers, sample_user.id) == expected
    assert _estate(client, admin_claim_headers) == expected


def test_each_member_carries_the_name_the_customer_knows_the_address_by(
    client, db, admin_claim_headers, uy, sample_user, radii  # noqa: F811
):
    """`display_label` is published on every member, so the panels print it and derive nothing: an untitled address
    with no written line is named by its street line."""
    assert 14 <= radii[1] - 2
    street_only = pinned_address(db, sample_user, *at(14))
    street_only.title, street_only.full_address, street_only.street_address = None, "", "Navoi 5"
    db.session.commit()

    [row] = _for_customer(client, admin_claim_headers, sample_user.id)
    assert [member["display_label"] for member in row["members"]] == ["Uy", "Navoi 5"]
    [estate_row] = _estate(client, admin_claim_headers)
    assert [member["display_label"] for member in estate_row["members"]] == ["Uy", "Navoi 5"]


def test_a_stored_row_with_no_valid_point_never_breaks_the_read(
    client, db, auth_headers, admin_claim_headers, uy, sample_user, radii  # noqa: F811
):
    """A row whose stored latitude is not a place on Earth (written past the ORM's zone listener) is no point to the
    guard: the pair loop skips it, and the customer's real pair is still suggested on both reads."""
    assert 14 <= radii[1] - 2
    ish = _keep(client, auth_headers, at(14), title="Ish")
    broken = pinned_address(db, sample_user, *at(500))
    db.session.execute(
        UserAddress.__table__.update().where(UserAddress.__table__.c.id == broken.id).values(latitude=200.0)
    )
    db.session.commit()

    expected = [_pair_row(uy, ish, sample_user, 14)]
    assert _for_customer(client, admin_claim_headers, sample_user.id, kind="same_customer") == expected
    assert _estate(client, admin_claim_headers) == expected


def test_entity_accounts_and_lone_addresses_get_nothing(client, db, admin_claim_headers, radii):
    assert 14 <= radii[1] - 2
    firm = customer(db, "Firma", user_type=UserType.ENTITY)
    pinned_address(db, firm, *BASE)
    pinned_address(db, firm, *at(14))
    lone = customer(db, "Lola")
    pinned_address(db, lone, *at(2000))
    # The same two pins 1 km away, owned by an individual: the one pair the estate holds.
    person = customer(db, "Aziz")
    near = pinned_address(db, person, *at(1000))
    far = pinned_address(db, person, *at(1014))

    assert [row["address_ids"] for row in _estate(client, admin_claim_headers)] == [[near.id, far.id]]
    assert _for_customer(client, admin_claim_headers, firm.id) == []
    assert _for_customer(client, admin_claim_headers, lone.id) == []


def test_three_own_addresses_give_two_pairs_and_never_flat_5_with_flat_7(
    client, auth_headers, admin_claim_headers, uy, sample_user, radii  # noqa: F811
):
    # Flat 5 and flat 7 are 34 m apart, inside the radius: only their apartments keep them apart.
    assert 34 <= radii[1] - 2
    flat_5 = _keep(client, auth_headers, at(14), title="5-xonadon", apartment_number="5")
    flat_7 = _keep(client, auth_headers, at(-20), title="7-xonadon", apartment_number="7")

    assert _for_customer(client, admin_claim_headers, sample_user.id) == [
        _pair_row(uy, flat_5, sample_user, 14),
        _pair_row(uy, flat_7, sample_user, 20),
    ]


def test_a_pair_inside_a_cross_customer_component_appears_once_as_shared_place(
    client, db, auth_headers, admin_claim_headers, uy, sample_user, radii  # noqa: F811
):
    # Three pins within 4 m of the base pin: one clusterer component holding two customers.
    assert 4 + 2 <= radii[0]
    neighbour = customer(db, "Nodira")
    theirs = pinned_address(db, neighbour, *at(-4))
    mine = _keep(client, auth_headers, at(4), title="Ish")
    ids = [uy.id, theirs.id, mine.id]

    rows = _for_customer(client, admin_claim_headers, sample_user.id)
    assert rows == [
        {
            "kind": "shared_place",
            "address_ids": ids,
            "distinct_customer_count": 2,
            "score": 2.0,
            "signal_fingerprint": CustomerLinkService._point_fingerprint(ids),
            "members": [_member(uy, sample_user), _member(theirs, neighbour), _member(mine, sample_user)],
        }
    ]

    # Dismissed, the place stays the shared-place arm's: the customer's own pair inside it never comes back.
    _dismiss(client, admin_claim_headers, rows[0])
    assert _for_customer(client, admin_claim_headers, sample_user.id) == []
    assert _estate(client, admin_claim_headers) == []


@pytest.mark.parametrize(
    "decision",
    [{"kind": "own_bottles", "reason": "his own bottles"}, {"kind": "close", "reason": "handled in the chat"}],
)
def test_an_open_shared_place_case_hides_the_pair_until_it_is_resolved(
    client, db, admin_claim_headers, radii, decision
):
    assert 14 <= radii[1] - 2
    jasur = customer(db, "Jasur")
    home = pinned_address(db, jasur, *BASE)
    flat = pinned_address(db, jasur, *at(14))
    # His first order, delivered with empties the place's balance does not explain: a case on `home`.
    case, *_ = _delivered(db, jasur, home)
    assert (case.address_id, case.status in SHARED_PLACE_OPEN_STATUSES) == (home.id, True)

    assert _for_customer(client, admin_claim_headers, jasur.id) == []
    assert _estate(client, admin_claim_headers) == []

    settled = client.post(RESOLVE.format(case.id), json=decision, headers=admin_claim_headers)
    assert settled.status_code == 200, settled.get_json()

    assert _for_customer(client, admin_claim_headers, jasur.id) == [_pair_row(home, flat, jasur, 14)]


def test_dismissing_a_same_customer_pair_hides_it_on_the_next_read(
    client, auth_headers, admin_claim_headers, uy, sample_user, radii  # noqa: F811
):
    """The three addresses sit within 6 m, so the clusterer puts them in ONE component of one customer, which the
    shared-place arm never shows. The dismissal must stamp the fingerprint of the row the admin pressed on (the
    pair's), not that component's, or the pair comes straight back (plan E19)."""
    assert 6 + 2 <= radii[0]
    north = _keep(client, auth_headers, at(3), title="Shimol")
    south = _keep(client, auth_headers, at(-3), title="Janub")
    rows = _for_customer(client, admin_claim_headers, sample_user.id)
    assert rows == [
        _pair_row(uy, north, sample_user, 3),
        _pair_row(uy, south, sample_user, 3),
        _pair_row(north, south, sample_user, 6),
    ]

    _dismiss(client, admin_claim_headers, rows[0])

    remaining = [_pair_row(uy, south, sample_user, 3), _pair_row(north, south, sample_user, 6)]
    assert _for_customer(client, admin_claim_headers, sample_user.id) == remaining
    assert _estate(client, admin_claim_headers) == remaining
    stored = PlaceSuggestionDismissal.query.one()
    assert (stored.address_id_low, stored.address_id_high, stored.signal_fingerprint) == (
        uy.id,
        north.id,
        CustomerLinkService._point_fingerprint([uy.id, north.id]),
    )


def test_kind_filters_before_limit(client, db, auth_headers, admin_claim_headers, uy, sample_user, radii):  # noqa: F811
    assert 3 + 2 <= radii[0] and 14 <= radii[1] - 2
    places = []
    for n in (1, 2, 3):
        first = pinned_address(db, customer(db, f"Ofis{n}a"), *at(500 * n))
        second = pinned_address(db, customer(db, f"Ofis{n}b"), *at(500 * n + 3))
        places.append([first.id, second.id])
    ish = _keep(client, auth_headers, at(14), title="Ish")
    pair = _pair_row(uy, ish, sample_user, 14)

    everything = _estate(client, admin_claim_headers)
    assert [(row["kind"], row["address_ids"]) for row in everything] == [
        *(("shared_place", ids) for ids in places),
        ("same_customer", [uy.id, ish.id]),
    ]
    assert [row["kind"] for row in _estate(client, admin_claim_headers, limit=1)] == ["shared_place"]
    assert _estate(client, admin_claim_headers, kind="same_customer", limit=1) == [pair]
    assert _estate(client, admin_claim_headers, kind="shared_place") == everything[:3]
    assert _for_customer(client, admin_claim_headers, sample_user.id, kind="same_customer") == [pair]
    assert _for_customer(client, admin_claim_headers, sample_user.id, kind="shared_place") == []


@pytest.mark.parametrize("kind", ["duplicates", "SAME_CUSTOMER", ""])
@pytest.mark.parametrize("route", ["estate", "for_customer"])
def test_an_unknown_kind_is_a_400(client, admin_claim_headers, sample_user, route, kind):
    url = ESTATE if route == "estate" else FOR_CUSTOMER.format(sample_user.id)

    response = client.get(url, headers=admin_claim_headers, query_string={"kind": kind})

    assert response.status_code == 400, response.get_json()
    assert response.get_json() == {"success": False, "message": "Validation failed", "errors": [KIND_ERROR]}
