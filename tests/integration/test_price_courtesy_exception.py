"""The manager exception: a manager or admin gives one person the old price while the window is open (spec §5.6, §6).

`grant_refusal` is the one rule; `grant_exception` writes behind it, and the admin page's grant button reads the
same rule (Task 29). A person gets one entry for a change, whatever the source.
"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from business_app.models.price_change import CourtesySource, PriceCourtesy
from business_app.services.price_change.courtesy import (
    apply_courtesy,
    courtesy_status,
    grant_exception,
    grant_refusal,
)
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from tests.integration.contract_pricing_factory import make_contract, make_workplace
from tests.integration.price_change_builders import (
    NEW_10,
    NEW_19,
    CAP_10,
    applied_change,
    bottles,
    courtesy_entry,
    local,
    make_price_change,
    priced_line,
    window_open_now,
)
from tests.integration.reorder_reminder_builders import link, live_order, make_customer

pytestmark = pytest.mark.integration

REASON = "Cancelled after the switch"


def _world(db, admin_user) -> SimpleNamespace:
    b = bottles(db)
    pc = window_open_now(db, b, created_by=admin_user)
    return SimpleNamespace(b=b, pc=pc)


def _refusal_worlds(db, admin_user, manager_user):
    """(name, user id, exception type) for every refusal the rule has, each in its own world step."""
    w = _world(db, admin_user)
    has_entry = make_customer(db)
    courtesy_entry(db, w.pc, has_entry)
    used = make_customer(db)
    courtesy_entry(db, w.pc, used, order=live_order(db, used, get_utc_now()))
    account, _office = make_workplace(db)
    contract, _row = make_contract(db, account, w.b.p19)
    contract.start_date = w.pc.applied_at - timedelta(days=30)
    db.session.commit()
    return [
        ("PRICE_COURTESY_USER_NOT_FOUND", 999_999, NotFoundError),
        ("PRICE_COURTESY_CONTRACT_ACCOUNT", account.id, ValidationError),
        ("PRICE_COURTESY_ALREADY_EXISTS", has_entry.id, ConflictError),
        ("PRICE_COURTESY_ALREADY_EXISTS", used.id, ConflictError),
    ]


def test_a_manager_gives_it_to_someone_the_rule_left_out(db, admin_user, manager_user):
    w = _world(db, admin_user)
    ali, ali2 = make_customer(db), make_customer(db)
    link(db, ali, ali2)
    assert courtesy_status(ali2.id, at=get_utc_now()).state == "none"

    before = get_utc_now()
    entry = grant_exception(ali.id, f"  {REASON}  ", manager_user.id)

    row = db.session.get(PriceCourtesy, entry.id)
    assert (row.price_change_id, row.user_id, row.source, row.reason) == (
        w.pc.id,
        ali.id,
        CourtesySource.MANAGER,
        REASON,
    )
    assert (row.granted_by_id, row.order_id, row.used_at) == (manager_user.id, None, None)
    assert before <= ensure_utc(row.granted_at) <= get_utc_now()
    assert ensure_utc(row.expires_at) == ensure_utc(w.pc.courtesy_ends_at)
    assert courtesy_status(ali2.id, at=get_utc_now()).state == "available"


def test_it_prices_like_the_automatic_one(db, admin_user, manager_user):
    w = _world(db, admin_user)
    ali = make_customer(db)
    grant_exception(ali.id, REASON, manager_user.id)

    line = priced_line(w.b.p19, 4, NEW_19)
    entry = apply_courtesy([line], user_id=ali.id, at=get_utc_now(), is_subscription_order=False, claim=False)

    assert entry is not None
    assert (line["courtesy_quantity"], float(line["discount_amount"]), float(line["total_price"])) == (
        2,
        4000.0,
        76000.0,
    )


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_reason_is_required(db, admin_user, manager_user, reason):
    _world(db, admin_user)
    ali = make_customer(db)
    with pytest.raises(ValidationError) as caught:
        grant_exception(ali.id, reason, manager_user.id)
    assert caught.value.error_code == "PRICE_COURTESY_REASON_REQUIRED"
    assert caught.value.message == "A reason is required to grant the old price."
    assert PriceCourtesy.query.count() == 0


def test_only_while_the_window_is_open(db, admin_user, manager_user):
    b = bottles(db)
    ali = make_customer(db)
    # Applied, but only in 2027: no window is open now.
    applied_change(db, b, created_by=admin_user)
    # Announced and never applied: no window either.
    make_price_change(
        db,
        [(b.p10, NEW_10, CAP_10, None)],
        created_by=admin_user,
        announce_on=date(2027, 3, 1),
        change_on=date(2027, 4, 1),
        announcement_started_at=local(2027, 3, 1, 9),
    )
    refusal = grant_refusal(ali.id)
    assert refusal is not None and refusal[0] == "PRICE_COURTESY_WINDOW_CLOSED"
    with pytest.raises(ValidationError) as closed:
        grant_exception(ali.id, REASON, manager_user.id)
    assert closed.value.error_code == "PRICE_COURTESY_WINDOW_CLOSED"
    assert PriceCourtesy.query.count() == 0


def test_once_per_person(db, admin_user, manager_user):
    w = _world(db, admin_user)
    ali, ali2 = make_customer(db), make_customer(db)
    link(db, ali, ali2)
    grant_exception(ali.id, REASON, manager_user.id)
    with pytest.raises(ConflictError) as again:
        grant_exception(ali2.id, REASON, manager_user.id)
    assert again.value.error_code == "PRICE_COURTESY_ALREADY_EXISTS"

    listed = make_customer(db)
    courtesy_entry(db, w.pc, listed)
    used = make_customer(db)
    courtesy_entry(db, w.pc, used, order=live_order(db, used, get_utc_now()))
    db.session.commit()  # a refused grant rolls back; the builders only flush
    for user in (listed, used):
        with pytest.raises(ConflictError) as caught:
            grant_exception(user.id, REASON, manager_user.id)
        assert caught.value.error_code == "PRICE_COURTESY_ALREADY_EXISTS"
    assert PriceCourtesy.query.count() == 3


def test_never_to_an_account_under_contract(db, admin_user, manager_user):
    w = _world(db, admin_user)
    account, _office = make_workplace(db)
    contract, _row = make_contract(db, account, w.b.p19)
    contract.start_date = w.pc.applied_at - timedelta(days=30)
    db.session.commit()
    with pytest.raises(ValidationError) as caught:
        grant_exception(account.id, REASON, manager_user.id)
    assert caught.value.error_code == "PRICE_COURTESY_CONTRACT_ACCOUNT"
    assert PriceCourtesy.query.count() == 0


def test_an_unknown_customer_is_not_found(db, admin_user, manager_user):
    _world(db, admin_user)
    with pytest.raises(NotFoundError) as caught:
        grant_exception(999_999, REASON, manager_user.id)
    assert caught.value.error_code == "PRICE_COURTESY_USER_NOT_FOUND"
    assert PriceCourtesy.query.count() == 0


def test_the_refusal_rule_is_the_one_the_write_raises(db, admin_user, manager_user):
    worlds = _refusal_worlds(db, admin_user, manager_user)
    for code, user_id, error_type in worlds:
        refusal = grant_refusal(user_id)
        with pytest.raises(error_type) as caught:
            grant_exception(user_id, REASON, manager_user.id)
        assert refusal == (caught.value.error_code, caught.value.message)
        assert refusal[0] == code

    eligible = make_customer(db)
    assert grant_refusal(eligible.id) is None
    grant_exception(eligible.id, REASON, manager_user.id)
    assert grant_refusal(eligible.id)[0] == "PRICE_COURTESY_ALREADY_EXISTS"


def test_the_window_closed_refusal_matches_the_write(db, admin_user, manager_user):
    ali = make_customer(db)
    refusal = grant_refusal(ali.id)
    with pytest.raises(ValidationError) as caught:
        grant_exception(ali.id, REASON, manager_user.id)
    assert refusal == (caught.value.error_code, caught.value.message)
    assert refusal[0] == "PRICE_COURTESY_WINDOW_CLOSED"
