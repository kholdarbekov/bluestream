"""The two nightly sales jobs: the 01:00 due-date recompute and the 01:10 stage sweep.

Every instant here is frozen and handed IN (`now=`) rather than patched, because both
services take the job's moment as an argument — the same contract
`VisitService.abandon_stale` keeps, and the reason a stage threshold can be asserted as a
calendar fact instead of "roughly a month after whenever the suite happened to run".

Nothing below re-implements a rule. The thresholds are the spec's own literals (D23:
`SALES_AT_RISK_RATIO` 1.5, `SALES_DORMANT_DAYS` 45) applied to dates chosen so that one day
either side of the boundary is the whole assertion.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.order import Order, OrderItem, OrderStatusHistory
from business_app.models.sales import Outlet, OutletStageHistory
from business_app.models.user import User, UserAddress
from business_app.services.sales.outlet_service import OutletService
from business_app.services.sales.replenishment_service import ReplenishmentService
from business_app.services.tryout_service import TryoutService
from business_app.tasks import sales_agent_tasks
from business_app.utils.password_security import hash_password
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import EntitySubtype, OrderStatus, UserRole, UserType

pytestmark = pytest.mark.unit

# 2026-10-01 06:00 UTC = 11:00 Tashkent (UTC+5).
FROZEN_UTC = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)


def _owner(db, phone):
    """The store's customer account — an outlet has no delivery history until it is linked."""
    user = User(
        phone=phone,
        password_hash=hash_password("Pw123456!"),
        first_name="Bahor",
        last_name="Owner",
        user_type=UserType.ENTITY,
        role=UserRole.CUSTOMER,
        company_name="Bahor market",
        entity_subtype=EntitySubtype.GROCERY_STORE,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _outlet(db, owner=None, *, name="Bahor market", stage="active", outlet_class="A", **columns):
    outlet = Outlet(
        name=name,
        outlet_type="grocery_store",
        stage=stage,
        outlet_class=outlet_class,
        user_id=owner.id if owner else None,
        **columns,
    )
    db.session.add(outlet)
    db.session.commit()
    return outlet


def _branch_address(db, owner, full_address, *, is_default=False):
    """One branch's own address. Coordinate-less on purpose: the delivery-zone listener
    skips an un-pinned row, and the stage rule reads the address as an IDENTITY, not a place."""
    address = UserAddress(user_id=owner.id, full_address=full_address, is_default=is_default)
    db.session.add(address)
    db.session.commit()
    return address


def _delivered(db, owner, product, *, days_ago, number, anchor=FROZEN_UTC, delivery_address_id=None):
    """A DELIVERED order whose landing instant lives on its status-history row.

    `orders` has no `delivered_at` column, so this row IS "when the water arrived" —
    the same fact `ReplenishmentService.delivered_qty` reads. `anchor` is the moment
    `days_ago` counts back from: the frozen instant everywhere except the task-wiring test,
    which drives a job that reads the wall clock.
    """
    landed = anchor - timedelta(days=days_ago)
    order = Order(
        user_id=owner.id,
        order_number=number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=delivery_address_id,
        created_at=landed,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=1,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("15000.00"),
        )
    )
    db.session.add(
        OrderStatusHistory(
            order_id=order.id,
            old_status=OrderStatus.OUT_FOR_DELIVERY,
            new_status=OrderStatus.DELIVERED,
            changed_at=landed,
            created_at=landed,
        )
    )
    db.session.commit()
    return order


def _enter_stage(db, outlet, stage, *, days_ago, anchor=FROZEN_UTC):
    """Put the outlet in `stage` and back-date the history row the job reads as "since when"."""
    OutletService.transition(outlet, stage, None, reason_code="test_setup")
    db.session.commit()
    row = (
        OutletStageHistory.query.filter_by(outlet_id=outlet.id)
        .order_by(OutletStageHistory.id.desc())
        .first()
    )
    row.created_at = anchor - timedelta(days=days_ago)
    db.session.commit()
    return row


def _converted_tryout_outlet(db, product, admin_user):
    """A `trial` outlet whose try-out has converted — the `activated_from_trial` fixture."""
    product.is_tryout_eligible = True
    db.session.commit()
    outlet = _outlet(db, None, name="Trial shop", stage="trial", outlet_class="A")
    tryout = TryoutService.create_tryout(
        {
            "trial_contact": {
                "first_name": "Trial",
                "last_name": "Customer",
                "phone": "+998901234610",
                "preferred_language": "uz",
            },
            "address": {
                "label": "Shop",
                "full_address": "12 Sample Street",
                "city": "Tashkent",
                "is_default": True,
            },
            "items": [{"product_id": product.id, "quantity": 1}],
            "complete_handoff": False,
        },
        admin_user.id,
        source="sales_agent",
    )
    converted = _owner(db, "+998901234611")
    tryout.outlet_id = outlet.id
    tryout.converted_user_id = converted.id
    db.session.commit()
    return outlet, converted


def _stage_rows(outlet):
    return OutletStageHistory.query.filter_by(outlet_id=outlet.id).order_by(OutletStageHistory.id.asc()).all()


def _last_stage_row(outlet):
    row = _stage_rows(outlet)[-1]
    return (row.from_stage, row.to_stage, row.reason_code, row.actor_user_id)


class TestRecomputeAll:
    def test_recompute_all_publishes_a_due_date_for_every_non_lost_outlet(self, db):
        """D7 through the job: the due list only READS `next_visit_due_at`, so an outlet
        nothing happened to overnight is invisible until this republishes it."""
        never_visited = _outlet(db, None, name="Never visited", stage="active")
        prospect = _outlet(db, None, name="Cold prospect", stage="prospect")
        dated = _outlet(db, None, name="Agent dated", stage="active", agent_next_visit_at=date(2026, 10, 5))
        lost = _outlet(db, None, name="Gone", stage="lost", next_visit_due_at=FROZEN_UTC - timedelta(days=99))

        count = ReplenishmentService.recompute_all(now=FROZEN_UTC)

        assert count == 3
        assert ensure_utc(never_visited.next_visit_due_at) == FROZEN_UTC
        assert prospect.next_visit_due_at is None
        # 2026-10-05 09:00 Tashkent (AGENT_VISIT_LOCAL_HOUR) = 04:00 UTC.
        assert ensure_utc(dated.next_visit_due_at) == datetime(2026, 10, 5, 4, 0, tzinfo=UTC)
        assert ensure_utc(lost.next_visit_due_at) == FROZEN_UTC - timedelta(days=99)

    def test_the_stock_check_catalogue_is_read_once_per_run_not_once_per_outlet(self, db, monkeypatch):
        """L30. `next_visit_due_at` asks for the primary returnable SKU, and that answer is a
        full catalogue scan — per outlet it is one estate-wide product query per shop, every
        night. The run holds ONE answer and hands it down."""
        for index in range(3):
            _outlet(db, None, name=f"Shop {index}", stage="active")
        reads = []
        real = ReplenishmentService.stock_check_products

        def counting():
            reads.append(1)
            return real()

        monkeypatch.setattr(ReplenishmentService, "stock_check_products", counting)

        assert ReplenishmentService.recompute_all(now=FROZEN_UTC) == 3
        assert len(reads) == 1
        assert {ensure_utc(outlet.next_visit_due_at) for outlet in Outlet.query.all()} == {FROZEN_UTC}


class TestStageSweep:
    def test_an_active_outlet_past_the_at_risk_threshold_moves_to_at_risk(self, db, sample_product):
        """R4: one delivered order is not a rhythm, so the CLASS cadence is the interval.
        Class A is 7 days and the ratio is 1.5, so 11 quiet days trip 10.5 and 10 do not."""
        quiet_owner = _owner(db, "+998901234601")
        quiet = _outlet(db, quiet_owner, name="Quiet shop", outlet_class="A")
        _delivered(db, quiet_owner, sample_product, days_ago=11, number="SA_000101_26")
        chatty_owner = _owner(db, "+998901234602")
        chatty = _outlet(db, chatty_owner, name="Chatty shop", outlet_class="A")
        _delivered(db, chatty_owner, sample_product, days_ago=10, number="SA_000102_26")

        counts = OutletService.update_stages(now=FROZEN_UTC)

        assert counts == {
            "scanned": 2,
            "at_risk": 1,
            "dormant": 0,
            "reactivated": 0,
            "activated_from_trial": 0,
        }
        assert quiet.stage == "at_risk"
        assert chatty.stage == "active"
        assert _last_stage_row(quiet) == ("active", "at_risk", "job_at_risk", None)
        assert _stage_rows(chatty) == []

    def test_a_dead_branch_goes_at_risk_while_its_busy_sibling_stays_active(self, db, sample_product):
        """D25 rule 5 through the 01:10 sweep -- the surface the agent actually feels.

        Both branches belong to ONE account and are class A (cadence 7, ratio 1.5 => 10.5 days).
        Account-wide, the sweep saw two deliveries one day apart, read the median gap as 1 day,
        floored it to MIN_INTERVAL_DAYS and measured "days since the last delivery" from the
        SIBLING's order: 10 days, under the threshold, so the shop that had taken nothing for
        eleven days stayed `active` for ever. Per branch, each shop answers for itself.
        """
        owner = _owner(db, "+998901234612")
        quiet_address = _branch_address(db, owner, "Chilonzor 5", is_default=True)
        busy_address = _branch_address(db, owner, "Yunusobod 12")
        quiet = _outlet(db, owner, name="Bahor market, Chilonzor", outlet_class="A",
                        address_id=quiet_address.id)
        busy = _outlet(db, owner, name="Bahor market, Yunusobod", outlet_class="A",
                       address_id=busy_address.id)
        _delivered(db, owner, sample_product, days_ago=11, number="SA_000140_26",
                   delivery_address_id=quiet_address.id)
        _delivered(db, owner, sample_product, days_ago=10, number="SA_000141_26",
                   delivery_address_id=busy_address.id)

        counts = OutletService.update_stages(now=FROZEN_UTC)

        assert counts == {
            "scanned": 2,
            "at_risk": 1,
            "dormant": 0,
            "reactivated": 0,
            "activated_from_trial": 0,
        }
        assert quiet.stage == "at_risk"
        assert busy.stage == "active"
        assert _last_stage_row(quiet) == ("active", "at_risk", "job_at_risk", None)
        assert _stage_rows(busy) == []

    def test_the_interval_is_this_shops_own_median_not_its_class_cadence(self, db, sample_product):
        """Six deliveries 20 days apart give a median gap of 20 and a threshold of 30, so
        15 quiet days are normal HERE — the class-A cadence would have called the same shop
        at risk after 11."""
        owner = _owner(db, "+998901234603")
        outlet = _outlet(db, owner, name="Slow but steady", outlet_class="A")
        for index in range(6):
            _delivered(db, owner, sample_product, days_ago=15 + 20 * index, number=f"SA_00011{index}_26")

        counts = OutletService.update_stages(now=FROZEN_UTC)

        assert counts["at_risk"] == 0
        assert outlet.stage == "active"
        assert _stage_rows(outlet) == []

    def test_the_median_interval_never_drops_below_the_seven_day_floor(self, db, sample_product):
        """A shop that takes a bottle every other day has a median gap of 2; unfloored, its
        threshold would be 3 days and one quiet weekend would call it at risk. Floored at 7
        the threshold is 10.5, so 8 quiet days are still normal."""
        owner = _owner(db, "+998901234604")
        outlet = _outlet(db, owner, name="Daily shop", outlet_class="A")
        for index in range(6):
            _delivered(db, owner, sample_product, days_ago=8 + 2 * index, number=f"SA_00012{index}_26")

        assert OutletService.update_stages(now=FROZEN_UTC)["at_risk"] == 0
        assert outlet.stage == "active"

    def test_an_outlet_that_has_been_at_risk_past_the_dormant_window_goes_dormant(self, db, sample_product):
        """`SALES_DORMANT_DAYS` is 45 (D23), counted from the AT-RISK TRANSITION.

        46 days at risk trip it, 45 do not — and the delivery history is deliberately the
        same for both shops, which is what makes the anchor the whole assertion.
        """
        gone_owner = _owner(db, "+998901234605")
        gone = _outlet(db, gone_owner, name="Gone quiet", outlet_class="A")
        _delivered(db, gone_owner, sample_product, days_ago=60, number="SA_000130_26")
        _enter_stage(db, gone, "at_risk", days_ago=46)
        holding_owner = _owner(db, "+998901234606")
        holding = _outlet(db, holding_owner, name="Holding on", outlet_class="A")
        _delivered(db, holding_owner, sample_product, days_ago=60, number="SA_000131_26")
        _enter_stage(db, holding, "at_risk", days_ago=45)

        counts = OutletService.update_stages(now=FROZEN_UTC)

        assert counts["dormant"] == 1
        assert counts["reactivated"] == 0
        assert gone.stage == "dormant"
        assert holding.stage == "at_risk"
        assert _last_stage_row(gone) == ("at_risk", "dormant", "job_dormant", None)

    def test_a_class_less_outlet_gets_a_real_at_risk_window_not_a_single_night(self, db, sample_product):
        """The collision this anchor exists for.

        `class` is NULL on every outlet phase 1 imported; NULL means cadence C (30 d), and
        1.5 x 30 is exactly `SALES_DORMANT_DAYS`. Measured from the last delivery, such a
        shop would be at risk for one night and dormant by the next run — an alert nobody
        could ever act on. Two consecutive runs, one day apart, are the whole test.
        """
        owner = _owner(db, "+998901234612")
        outlet = _outlet(db, owner, name="No class", outlet_class=None)
        _delivered(db, owner, sample_product, days_ago=46, number="SA_000150_26")

        first = OutletService.update_stages(now=FROZEN_UTC)
        assert (first["at_risk"], first["dormant"]) == (1, 0)
        assert outlet.stage == "at_risk"

        second = OutletService.update_stages(now=FROZEN_UTC + timedelta(days=1))
        assert second["dormant"] == 0
        assert outlet.stage == "at_risk"
        assert [
            (row.from_stage, row.to_stage, row.reason_code, row.actor_user_id) for row in _stage_rows(outlet)
        ] == [("active", "at_risk", "job_at_risk", None)]

    def test_a_delivery_since_the_outlet_went_quiet_brings_it_back_to_active(self, db, sample_product):
        """R5. The anchor is the history row that PUT the outlet here, so a run that was
        skipped, retried or delayed cannot lose the delivery that landed in between."""
        back_owner = _owner(db, "+998901234607")
        back = _outlet(db, back_owner, name="Back in business", outlet_class="A")
        _enter_stage(db, back, "at_risk", days_ago=10)
        _delivered(db, back_owner, sample_product, days_ago=3, number="SA_000140_26")
        still_owner = _owner(db, "+998901234608")
        still = _outlet(db, still_owner, name="Still quiet", outlet_class="A")
        _delivered(db, still_owner, sample_product, days_ago=30, number="SA_000141_26")
        _enter_stage(db, still, "dormant", days_ago=10)

        counts = OutletService.update_stages(now=FROZEN_UTC)

        assert counts["reactivated"] == 1
        assert counts["at_risk"] == 0
        assert back.stage == "active"
        assert still.stage == "dormant"
        assert _last_stage_row(back) == ("at_risk", "active", "job_reactivated", None)

    def test_an_outlet_that_has_never_taken_a_delivery_is_left_alone(self, db):
        """"Days since the last delivered order" has no value at all here. Inventing one
        from the row's creation date would put every freshly approved shop at risk on the
        first night; a never-served active outlet is phase 3's exception feed, not a stage."""
        outlet = _outlet(db, _owner(db, "+998901234609"), name="Brand new", outlet_class="A")

        counts = OutletService.update_stages(now=FROZEN_UTC)

        assert counts == {
            "scanned": 1,
            "at_risk": 0,
            "dormant": 0,
            "reactivated": 0,
            "activated_from_trial": 0,
        }
        assert outlet.stage == "active"
        assert _stage_rows(outlet) == []

    def test_one_outlet_blowing_up_leaves_the_whole_sweep_uncommitted(self, db, sample_product, monkeypatch):
        """`update_stages` commits ONCE, at the end: the run is all-or-nothing.

        A half-written sweep is worse than none — the outlets before the bad row would be moved
        and the ones after untouched, with no record of where it stopped, and the retry would
        re-walk the moved ones against thresholds they have already crossed.
        """
        first_owner = _owner(db, "+998901234631")
        first = _outlet(db, first_owner, name="First quiet", outlet_class="A")
        _delivered(db, first_owner, sample_product, days_ago=11, number="SA_000301_26")
        second_owner = _owner(db, "+998901234632")
        second = _outlet(db, second_owner, name="Second quiet", outlet_class="A")
        _delivered(db, second_owner, sample_product, days_ago=11, number="SA_000302_26")
        real = ReplenishmentService.delivered_instants

        def exploding(outlet, **kwargs):
            if outlet.id == second.id:
                raise RuntimeError("this outlet's history blew up")
            return real(outlet, **kwargs)

        monkeypatch.setattr(ReplenishmentService, "delivered_instants", exploding)

        with pytest.raises(RuntimeError):
            OutletService.update_stages(now=FROZEN_UTC)

        db.session.rollback()
        assert OutletStageHistory.query.count() == 0
        assert (first.stage, second.stage) == ("active", "active")

    def test_a_converted_tryout_links_its_customer_and_activates_the_trial_outlet(
        self, db, sample_product, admin_user
    ):
        """D8/R5: `convert_tryout` already minted the account; nothing until now attached it
        to the shop the agent left the bottles at, so the outlet stayed `trial` for ever and
        never reached a due list."""
        outlet, converted = _converted_tryout_outlet(db, sample_product, admin_user)

        counts = OutletService.update_stages(now=FROZEN_UTC)

        assert counts["activated_from_trial"] == 1
        assert counts["scanned"] == 0
        assert outlet.stage == "active"
        assert outlet.user_id == converted.id
        assert outlet.address_id is not None
        assert UserAddress.query.get(outlet.address_id).user_id == converted.id
        assert _last_stage_row(outlet) == ("trial", "active", "job_tryout_converted", None)

    def test_a_converted_tryout_joins_an_account_that_already_has_a_branch(self, db, sample_product, admin_user):
        """The guard this replaces was written for `uq_outlets_user_id`: the link was skipped
        whenever another outlet already held that customer, so a chain's converted trial shop
        stayed account-less for ever and never reached a due list. It also needs an address of
        its own, or it activates into a shop no order can be placed for."""
        outlet, converted = _converted_tryout_outlet(db, sample_product, admin_user)
        existing = UserAddress(user_id=converted.id, full_address="Chilonzor 5", is_default=True)
        db.session.add(existing)
        db.session.flush()
        db.session.add(
            Outlet(
                name="Chain, Chilonzor",
                outlet_type="grocery_store",
                stage="active",
                user_id=converted.id,
                address_id=existing.id,
            )
        )
        db.session.commit()
        existing_id = existing.id

        counts = OutletService.update_stages(now=FROZEN_UTC)

        assert counts["activated_from_trial"] == 1
        assert (outlet.stage, outlet.user_id) == ("active", converted.id)
        branch = UserAddress.query.get(outlet.address_id)
        assert branch.id != existing_id
        assert (branch.user_id, branch.is_default, branch.title) == (converted.id, False, "Trial shop")

    def test_a_converted_tryout_already_carrying_its_customer_still_gets_an_address(
        self, db, sample_product, admin_user
    ):
        """R39: the address step keys on `address_id`, not on whether the link was written this
        run. A trial outlet whose `user_id` was set earlier (by hand, or by a partial run) used to
        skip the address entirely and activate into a shop no order can be placed for."""
        outlet, converted = _converted_tryout_outlet(db, sample_product, admin_user)
        outlet.user_id = converted.id
        db.session.commit()

        counts = OutletService.update_stages(now=FROZEN_UTC)

        assert counts["activated_from_trial"] == 1
        assert (outlet.stage, outlet.user_id) == ("active", converted.id)
        assert outlet.address_id is not None
        address = UserAddress.query.get(outlet.address_id)
        assert (address.user_id, address.is_default, address.title) == (converted.id, True, "Trial shop")


class TestNightlyJobWiring:
    def test_the_recompute_task_runs_the_service_and_reports_the_count(self, db):
        _outlet(db, None, name="One shop", stage="active")

        assert sales_agent_tasks.recompute_all_outlets.run() == {"success": True, "outlets": 1}

    def test_the_stage_task_runs_the_service_and_reports_every_counter(self, db, sample_product, admin_user):
        """The task is a thin shell: the counters the job logs are the service's own.

        One outlet per branch, anchored on the WALL clock (the task passes no `now`): against an
        empty database every counter is zero, which is exactly what a shell that never called
        the service would also answer.
        """
        now = datetime.now(UTC)
        quiet_owner = _owner(db, "+998901234621")
        _outlet(db, quiet_owner, name="Quiet shop", outlet_class="A")
        _delivered(db, quiet_owner, sample_product, days_ago=11, number="SA_000201_26", anchor=now)
        gone_owner = _owner(db, "+998901234622")
        gone = _outlet(db, gone_owner, name="Gone quiet", outlet_class="A")
        _delivered(db, gone_owner, sample_product, days_ago=60, number="SA_000202_26", anchor=now)
        _enter_stage(db, gone, "at_risk", days_ago=46, anchor=now)
        back_owner = _owner(db, "+998901234623")
        back = _outlet(db, back_owner, name="Back in business", outlet_class="A")
        _enter_stage(db, back, "at_risk", days_ago=10, anchor=now)
        _delivered(db, back_owner, sample_product, days_ago=3, number="SA_000203_26", anchor=now)
        _converted_tryout_outlet(db, sample_product, admin_user)

        assert sales_agent_tasks.update_outlet_stages.run() == {
            "success": True,
            "scanned": 3,
            "at_risk": 1,
            "dormant": 1,
            "reactivated": 1,
            "activated_from_trial": 1,
        }
