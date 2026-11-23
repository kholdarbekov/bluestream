"""Saved addresses exactly as the backend sends them, for the bots' fake backends.

Every address row the bots read is `UserAddress.to_dict()`: `GET`/`POST /api/v1/staff/operator/users/<id>/addresses`
(operator) answers exactly that, and `GET /api/v1/auth/addresses` (customer bot) answers it plus `pin_move_refusal`
(`customer_list_row`, `CUSTOMER_LIST_KEYS`). A row built here goes through that same serializer, so it carries the
producer's keys and nothing else: `display_label` is the backend's answer, never a test's guess, and a key the model
does not have (`address_line1`, `label`) is refused by the model itself.
tests/contract/test_bot_address_fakes_match_producer.py holds these rows to the live endpoints.
"""

from typing import Optional

from business_app.models.user import UserAddress


def address_row(address_id, **columns) -> dict:
    """One saved address as an address endpoint answers it. Only `UserAddress` columns are accepted; an unset
    `is_default` / `is_business` is False, as the database column default writes it."""
    columns.setdefault("is_default", False)
    columns.setdefault("is_business", False)
    return UserAddress(id=address_id, **columns).to_dict()


# The keys of `UserAddress.to_dict()`: exactly what the operator address endpoints send, and what the customer's list
# sends before it adds `pin_move_refusal`. tests/contract/test_bot_address_fakes_match_producer.py holds them to the
# live endpoints; the bots' fake backends refuse any stored row without exactly these keys (`producer_rows`).
ADDRESS_KEYS = frozenset(address_row(0, full_address="").keys())


# `GET /api/v1/auth/addresses` (the customer's own list) adds the pin rule that would refuse a move of each row.
CUSTOMER_LIST_KEYS = ADDRESS_KEYS | {"pin_move_refusal"}


def customer_list_row(row: dict, pin_move_refusal: Optional[str] = None) -> dict:
    """`row` as the customer's address list serves it: the address plus `pin_move_refusal`."""
    return {**row, "pin_move_refusal": pin_move_refusal}


# The 409 an address write answers when its point lands next to one of the customer's saved addresses
# (`address_guard_service.assert_not_duplicate`): the customer's create, edit and move routes and the operator's create
# all send it. Each candidate it names is the saved address as `UserAddress.to_dict()` sends it, plus its whole-metre
# `distance_m` from the new point.
POSSIBLE_DUPLICATE = "ADDRESS_POSSIBLE_DUPLICATE"
DUPLICATE_CANDIDATE_KEYS = ADDRESS_KEYS | {"distance_m"}


def possible_duplicate_body(candidates, *, move_address_id: Optional[int] = None) -> dict:
    """The part of that 409's body the bots read: the top-level `error_code`, and `details` with the `move_address_id`
    the backend offers (None when it offers no Move) and the `candidates`, nearest first, each built as
    `address_row(...) | {"distance_m": n}`. A candidate without exactly `DUPLICATE_CANDIDATE_KEYS` is refused.

    The one envelope both bots' fakes serve: the customer bot's as the error body (`backend_failure(data=...)`), the
    operator journeys' through `staff_backend_failure(error_code=..., details=...)`.
    tests/contract/test_bot_address_fakes_match_producer.py holds it to the live routes, so a renamed key fails there
    instead of leaving both bot suites green.
    """
    candidates = list(candidates)
    for row in candidates:
        keys = set(row)
        if keys != DUPLICATE_CANDIDATE_KEYS:
            raise AssertionError(
                f"a duplicate candidate the 409 never sends (extra {sorted(keys - DUPLICATE_CANDIDATE_KEYS)}, "
                f"missing {sorted(DUPLICATE_CANDIDATE_KEYS - keys)}): "
                "build it as address_row(...) | {'distance_m': n}"
            )
    return {
        "error_code": POSSIBLE_DUPLICATE,
        "details": {"move_address_id": move_address_id, "candidates": candidates},
    }


def producer_rows(rows, where: str):
    """`rows` unchanged, once every one has exactly `ADDRESS_KEYS` (the serializer's keys, before the customer list
    adds `pin_move_refusal`); else AssertionError naming `where` and the keys that differ. The fakes call it when a
    test seeds rows and when they serve them, so a hand-written row (`address_line1`, `label`, a missing
    `display_label`) can never reach a screen."""
    for row in rows:
        keys = set(row)
        if keys != ADDRESS_KEYS:
            raise AssertionError(
                f"{where} holds an address row the endpoint never sends (extra {sorted(keys - ADDRESS_KEYS)}, "
                f"missing {sorted(ADDRESS_KEYS - keys)}): build it with address_row()"
            )
    return rows


def stored_row(address_id, body: dict, **columns) -> dict:
    """The row a fake backend keeps for a create or update `body` the bot sent: the body's `UserAddress` columns
    (the backend ignores any other key), over `columns`."""
    known = set(UserAddress.__table__.columns.keys())
    return address_row(address_id, **{**columns, **{k: v for k, v in (body or {}).items() if k in known}})


def edited_row(row: dict, changes: dict) -> dict:
    """`row` after an update `changes` (`PUT /api/v1/auth/addresses/<id>`), re-serialized so its `display_label`
    follows a new title."""
    known = set(UserAddress.__table__.columns.keys())
    current = {k: v for k, v in row.items() if k in known and k != "id"}
    return stored_row(row["id"], changes, **current)
