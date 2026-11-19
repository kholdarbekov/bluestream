"""The customer-bot fake backend only serves address rows in the producer's shape (address-label contract).

`FakeBackend` answers `GET /api/v1/auth/addresses` from the rows a test seeds. Before 2026-11-17 it served any dict,
so journeys seeded `address_line1`/`city` (the keys the broken subscription picker read) and stayed green while the
real endpoint never sent them. A row is now refused unless it has exactly the keys of
`tests/integration/address_row_builders.address_row` (the real `UserAddress.to_dict()`), and
tests/contract/test_bot_address_fakes_match_producer.py holds those keys to the live endpoints. The seed is refused
in the test's own line, where no handler's `except Exception` can swallow it.
"""

import pytest

from tests.integration.address_row_builders import address_row
from tests.telegram_bot.ptb_harness import FakeBackend

# The row the subscription-picker journeys used to seed: the removed raw-SQL table's vocabulary.
HAND_WRITTEN = {"id": 91, "address_line1": "15 Chilonzor", "city": "Toshkent", "is_default": True}


def test_a_hand_written_address_row_is_refused_however_it_is_seeded():
    backend = FakeBackend()

    with pytest.raises(AssertionError, match="address_line1"):
        backend.addresses[91] = dict(HAND_WRITTEN)
    with pytest.raises(AssertionError, match="address_line1"):
        backend.addresses.update({91: dict(HAND_WRITTEN)})
    with pytest.raises(AssertionError, match="address_line1"):
        backend.addresses.setdefault(91, dict(HAND_WRITTEN))

    assert backend.addresses == {}


def test_a_row_missing_a_key_the_endpoint_sends_is_refused():
    backend = FakeBackend()
    row = address_row(91, title="Uy", full_address="15 Chilonzor")
    del row["display_label"]

    with pytest.raises(AssertionError, match="display_label"):
        backend.addresses[91] = row


@pytest.mark.anyio
async def test_the_address_list_serves_producer_rows_and_refuses_a_row_that_drifted_after_seeding():
    backend = FakeBackend()
    row = address_row(91, title="Uy", full_address="15 Chilonzor")
    backend.addresses[91] = row

    served = await backend.handle("GET", "/api/v1/auth/addresses")
    assert served.data == {"data": {"addresses": [row]}}

    row["label"] = "Uy"  # an in-place edit can still add a key no endpoint sends
    with pytest.raises(AssertionError, match="label"):
        await backend.handle("GET", "/api/v1/auth/addresses")
