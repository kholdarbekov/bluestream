"""The operator journeys' fake backend only serves address rows in the producer's shape (address-label contract).

`OperatorWorld.addresses` answers `GET /api/v1/staff/operator/users/<id>/addresses` in
tests/staff_bot/test_staff_operator_journey_dispatcher.py. Its hand-written row carried `title` while the real
endpoint sent `label`, so every operator screen test stayed green over an empty name. A row is now refused unless it
has exactly the keys of `tests/integration/address_row_builders.address_row` (the real `UserAddress.to_dict()`), and
tests/contract/test_bot_address_fakes_match_producer.py holds those keys to the live endpoint. The seed is refused in
the test's own line, where no handler's `except Exception` can swallow it.
"""

from types import SimpleNamespace

import pytest

from tests.integration.address_row_builders import address_row
from tests.staff_bot.ptb_harness import FakeStaffBackend
from tests.staff_bot.test_staff_operator_journey_dispatcher import (
    ADDRESSES_ENDPOINT,
    HOME_ADDRESS,
    OperatorWorld,
    wire_backend,
)

# What the endpoint sent before 2026-11-17 (staff.py's hand-built dict), and what the fake held instead.
OLD_ENDPOINT_ROW = {"id": 9001, "label": "Uy", "full_address": "Chilonzor 9-kvartal, 14-uy", "city": "Toshkent",
                    "district": "Chilonzor", "latitude": None, "longitude": None}
OLD_FAKE_ROW = {"id": 9001, "title": "Uy", "full_address": "Chilonzor 9-kvartal, 14-uy", "district": "Chilonzor"}


@pytest.mark.parametrize("row", [OLD_ENDPOINT_ROW, OLD_FAKE_ROW], ids=["old-endpoint-shape", "old-fake-shape"])
def test_a_hand_written_address_row_is_refused_when_the_world_is_built_or_reseeded(row):
    with pytest.raises(AssertionError, match="address_row"):
        OperatorWorld(addresses=[dict(row)])

    world = OperatorWorld()
    with pytest.raises(AssertionError, match="address_row"):
        world.addresses = [dict(row)]
    assert world.addresses == [HOME_ADDRESS], "a refused seed must leave the world as it was"


@pytest.mark.anyio
async def test_the_address_list_serves_producer_rows_and_refuses_a_row_that_drifted_after_seeding():
    backend = FakeStaffBackend()
    world = wire_backend(SimpleNamespace(backend=backend), OperatorWorld())

    served = await backend.handle("GET", ADDRESSES_ENDPOINT)
    assert served.data == {"items": [HOME_ADDRESS]}

    world.addresses.append(address_row(9003, title="Ofis", full_address="Amir Temur 108"))
    world.addresses[0]["label"] = "Uy"  # an in-place edit can still add a key no endpoint sends
    with pytest.raises(AssertionError, match="label"):
        await backend.handle("GET", ADDRESSES_ENDPOINT)
