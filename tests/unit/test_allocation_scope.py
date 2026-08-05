"""AllocationScope: pure scope container for the Phase-2 allocation engine.

No DB — events/payments/orders are stubbed with SimpleNamespace because the
class reads plain attributes only.
"""
from types import SimpleNamespace

import pytest

from business_app.services.allocation_scope import AllocationScope


def _event(**kw):
    base = {"scope_type": "personal", "scope_snapshot": None, "customer_id": 10}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.unit
class TestConstructorsAndSnapshots:
    def test_personal_snapshot_is_none(self):
        scope = AllocationScope.personal(10)
        assert scope.scope_type == "personal"
        assert scope.group_id is None
        assert scope.address_ids == ()
        assert scope.orderer_cluster_user_ids == (10,)
        assert scope.to_snapshot() is None

    def test_cluster_snapshot_shape_and_sorted_ids(self):
        scope = AllocationScope.cluster([30, 10, 20])
        assert scope.scope_type == "cluster"
        assert scope.orderer_cluster_user_ids == (10, 20, 30)
        assert scope.to_snapshot() == {"user_ids": [10, 20, 30]}

    def test_place_snapshot_shape(self):
        scope = AllocationScope.place(7, [2, 1], [20, 10], [10, 55])
        assert scope.scope_type == "place"
        assert scope.to_snapshot() == {
            "group_id": 7,
            "address_ids": [1, 2],
            "place_user_ids": [10, 20],
            "orderer_cluster_user_ids": [10, 55],
        }

    def test_frozen(self):
        scope = AllocationScope.personal(10)
        with pytest.raises(Exception):
            scope.scope_type = "cluster"


@pytest.mark.unit
class TestFromEvent:
    def test_personal_event(self):
        scope = AllocationScope.from_event(_event())
        assert scope == AllocationScope.personal(10)

    def test_cluster_event(self):
        e = _event(scope_type="cluster", scope_snapshot={"user_ids": [10, 20]})
        assert AllocationScope.from_event(e) == AllocationScope.cluster([10, 20])

    def test_place_event(self):
        snap = {"group_id": 7, "address_ids": [1, 2],
                "place_user_ids": [10, 20], "orderer_cluster_user_ids": [10]}
        e = _event(scope_type="place", scope_snapshot=snap)
        scope = AllocationScope.from_event(e)
        assert scope == AllocationScope.place(7, [1, 2], [10, 20], [10])

    def test_scoped_event_missing_snapshot_degrades_to_personal(self):
        # Defensive money rule: never guess current topology. The nightly
        # reconcile flags these events (Task 5).
        e = _event(scope_type="cluster", scope_snapshot=None, customer_id=42)
        assert AllocationScope.from_event(e) == AllocationScope.personal(42)

    def test_unknown_scope_type_degrades_to_personal(self):
        e = _event(scope_type="galaxy", scope_snapshot={"user_ids": [1]}, customer_id=42)
        assert AllocationScope.from_event(e) == AllocationScope.personal(42)


@pytest.mark.unit
class TestCoversPayment:
    def test_personal_covers_only_own_payment(self):
        scope = AllocationScope.personal(10)
        assert scope.covers_payment(SimpleNamespace(user_id=10), None) is True
        assert scope.covers_payment(SimpleNamespace(user_id=11), None) is False

    def test_cluster_covers_sibling_payment(self):
        scope = AllocationScope.cluster([10, 20])
        assert scope.covers_payment(SimpleNamespace(user_id=20), None) is True
        assert scope.covers_payment(SimpleNamespace(user_id=30), None) is False

    def test_place_cluster_arm_uses_orderer_cluster(self):
        scope = AllocationScope.place(7, [1, 2], [10, 20], [10, 55])
        # 55 is in the orderer's cluster but owns no group address.
        payment = SimpleNamespace(user_id=55)
        order = SimpleNamespace(delivery_address_id=99)
        assert scope.covers_payment(payment, order) is True

    def test_place_address_arm_covers_coworker_payment(self):
        scope = AllocationScope.place(7, [1, 2], [10, 20], [10])
        # Coworker 20 is NOT in the orderer's cluster; their order is delivered
        # to a member address of the frozen group => in scope (spec §5.4).
        payment = SimpleNamespace(user_id=20)
        order = SimpleNamespace(delivery_address_id=2)
        assert scope.covers_payment(payment, order) is True

    def test_place_out_of_scope(self):
        scope = AllocationScope.place(7, [1, 2], [10, 20], [10])
        payment = SimpleNamespace(user_id=20)
        order = SimpleNamespace(delivery_address_id=99)  # not a member address
        assert scope.covers_payment(payment, order) is False
        assert scope.covers_payment(payment, None) is False

    def test_cluster_never_uses_address_arm(self):
        scope = AllocationScope.cluster([10])
        payment = SimpleNamespace(user_id=20)
        order = SimpleNamespace(delivery_address_id=1)
        assert scope.covers_payment(payment, order) is False
