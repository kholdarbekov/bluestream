"""Saved addresses exactly as the backend sends them, for the bots' fake backends.

Every address list the bots read answers `UserAddress.to_dict()`: `GET /api/v1/auth/addresses` (customer bot) and
`GET`/`POST /api/v1/staff/operator/users/<id>/addresses` (operator). A row built here goes through that same
serializer, so it carries the producer's keys and nothing else: `display_label` is the backend's answer, never a
test's guess, and a key the model does not have (`address_line1`, `label`) is refused by the model itself.
tests/contract/test_bot_address_fakes_match_producer.py holds these rows to the live endpoints.
"""

from business_app.models.user import UserAddress


def address_row(address_id, **columns) -> dict:
    """One saved address as an address endpoint answers it. Only `UserAddress` columns are accepted; an unset
    `is_default` / `is_business` is False, as the database column default writes it."""
    columns.setdefault("is_default", False)
    columns.setdefault("is_business", False)
    return UserAddress(id=address_id, **columns).to_dict()


# The keys every address endpoint sends. tests/contract/test_bot_address_fakes_match_producer.py holds them to the
# live endpoints; the bots' fake backends refuse any row without exactly these keys (`producer_rows`).
ADDRESS_KEYS = frozenset(address_row(0, full_address="").keys())


def producer_rows(rows, where: str):
    """`rows` unchanged, once every one has exactly the keys an address endpoint sends; else AssertionError naming
    `where` and the keys that differ. The fakes call it when a test seeds rows and when they serve them, so a
    hand-written row (`address_line1`, `label`, a missing `display_label`) can never reach a screen."""
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
