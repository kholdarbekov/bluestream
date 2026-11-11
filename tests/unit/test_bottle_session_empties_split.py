"""Full bottles and empties are separate counters on a bottle session and on a transfer (Task 1 of the bottle-aware
auto-assign plan). The properties are the one place the split is derived."""

from business_app.models.bottle import DriverBottleSession, DriverBottleTransfer


def test_empties_on_board_counts_collected_and_empty_transfers():
    s = DriverBottleSession(
        bottles_loaded=10,
        bottles_delivered=4,
        bottles_collected_from_customers=4,
        empties_transferred_in=3,
        empties_transferred_out=2,
    )
    assert s.full_bottles_on_board == 6
    assert s.empties_on_board == 5
    assert s.current_inventory == 11
    assert (s.transferable_full, s.transferable_empties) == (6, 5)


def test_discrepancy_is_zero_when_return_equals_full_plus_empties():
    s = DriverBottleSession(
        bottles_loaded=10,
        bottles_delivered=4,
        bottles_collected_from_customers=4,
        empties_transferred_out=4,
        bottles_returned_to_warehouse=6,
    )
    s.compute_discrepancy()
    assert s.discrepancy == 0


def test_discrepancy_counts_empties_moved_in_and_out():
    s = DriverBottleSession(
        bottles_loaded=10,
        bottles_collected_from_customers=2,
        empties_transferred_in=3,
        empties_transferred_out=1,
        bottles_returned_to_warehouse=10,
    )
    s.compute_discrepancy()
    assert s.discrepancy == 4  # 10 full + (2 + 3 - 1) empties - 10 returned


def test_transferable_clamps_legacy_negative_full():
    s = DriverBottleSession(
        bottles_loaded=10, bottles_delivered=4, bottles_transferred_out=10, bottles_collected_from_customers=4
    )
    assert s.full_bottles_on_board == -4
    assert s.transferable_full == 0
    assert s.transferable_empties == 4


def test_transferable_clamps_negative_empties():
    s = DriverBottleSession(bottles_loaded=5, empties_transferred_out=2)
    assert s.empties_on_board == -2
    assert s.transferable_empties == 0
    assert s.transferable_full == 5


def test_unset_counters_count_as_zero():
    s = DriverBottleSession()
    assert (s.empties_on_board, s.current_inventory, s.transferable_full, s.transferable_empties) == (0, 0, 0, 0)


def test_to_dict_publishes_split():
    s = DriverBottleSession(
        bottles_loaded=10,
        bottles_delivered=4,
        bottles_collected_from_customers=4,
        empties_transferred_in=3,
        empties_transferred_out=2,
    )
    d = s.to_dict()
    assert d["full_bottles_on_board"] == 6
    assert d["empties_on_board"] == 5
    assert d["empties_transferred_out"] == 2
    assert d["empties_transferred_in"] == 3
    assert d["transferable_full"] == 6
    assert d["transferable_empties"] == 5
    assert d["current_inventory"] == 11


def test_transfer_to_dict_publishes_empty_components():
    t = DriverBottleTransfer(
        sender_session_id=1,
        sender_driver_id=2,
        receiver_driver_id=3,
        declared_quantity=5,
        declared_empty_quantity=2,
        confirmed_quantity=5,
        confirmed_empty_quantity=1,
    )
    d = t.to_dict()
    assert (d["declared_quantity"], d["declared_empty_quantity"]) == (5, 2)
    assert (d["confirmed_quantity"], d["confirmed_empty_quantity"]) == (5, 1)


def test_transfer_confirmed_empty_quantity_is_none_until_confirmed():
    t = DriverBottleTransfer(sender_session_id=1, sender_driver_id=2, receiver_driver_id=3, declared_quantity=5)
    assert t.to_dict()["confirmed_empty_quantity"] is None
