"""/my-subscriptions, the read side (plan 2026-10-26 Task 4a; spec 2026-10-26 §4, §12; UX brief).

The page is a server-rendered SHELL (containers + a page-data island with every word the page adds) and ONE
JS renderer over the API's published fields. `admin_ui/src/__tests__/web/mySubscriptions.test.js` runs the
shipped script in jsdom against the files under `tests/fixtures/web_subscription_management/`, which THIS
module writes and guards:

- `shell.json`: the `.subs` shell and its island, rendered here in en, uz and ru;
- `<case>.json`: for each state of the UX brief's state matrix, the exact requests the page's script sends
  (`GET /api/v1/subscriptions/?per_page=50`, then `/history?page=N&per_page=5` and, while it can be changed,
  the payment menu `/payments/methods?context=subscription&delivery_address_id=N` per subscription) and what
  the real endpoints answer, built through the endpoints the website calls (cookie JWT + `X-CSRF-TOKEN`, the
  language fixed by the page visit as a browser fixes it).

Each fixture test regenerates its file and fails when it differs (a stale fixture would let the JS test pass
against answers the backend no longer gives). Regenerate with `UPDATE_WEB_SUBSCRIPTION_FIXTURES=1`.

Determinism: the business clock is frozen at Monday 26 October 2026 10:00 Tashkent (and moved to Wednesday for
the daily run), and Pay reads the same instant. What only the wall clock stamps is pinned or blanked: `start_date`
is sent in the create body, `paused_at` / `end_date` (whose labels the page shows) and a placed order's `created_at`
and `online_payable_since` (which its payment deadline counts from) are set to the frozen instant, other `*_at`
stamps are blanked, and order numbers (random on SQLite, which has no order sequence) are renamed by order id.
Only external I/O is mocked: Celery, the route notifications, the Click link (T1's `_external_io`).
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Dict

import pytest
from bs4 import BeautifulSoup
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.product import Product
from business_app.models import subscription as subscription_model
from business_app.models.subscription import Subscription
from business_app.models.translation import Translation
from business_app.services import subscription_service as subscription_service_module
from business_app.tasks.subscription_tasks import process_daily_subscription_billing
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from shared.enums import LIVE_SUBSCRIPTION_STATUSES, DeliveryStatus, OrderStatus, PaymentStatus, SubscriptionStatus
from tests.integration.test_subscription_action_guards import (  # noqa: F401  (fixtures)
    BASE,
    TASHKENT,
    _Clock,
    _cap_the_place,
    _create,
    _external_io,
    _mark_paid,
    _picked_up,
    _state,
    _Web,
    cups,
    every_day_slot,
)

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "web_subscription_management"
TEMPLATE = REPO / "business_app" / "templates" / "frontend" / "my_subscriptions.html"
SCRIPT = REPO / "business_app" / "static" / "js" / "pages" / "my-subscriptions.js"
UPDATE = os.environ.get("UPDATE_WEB_SUBSCRIPTION_FIXTURES") == "1"
LANGUAGES = ("en", "uz", "ru")

API = "/api/v1"
# The requests are the script's own: its list URL and its history page size, read from the shipped source.
LIST_PATH = re.search(r"var LIST_URL = '([^']+)'", SCRIPT.read_text("utf-8")).group(1)
HISTORY_PAGE_SIZE = int(re.search(r"var HISTORY_PAGE_SIZE = (\d+)", SCRIPT.read_text("utf-8")).group(1))
# The payment menu of a subscription the customer can still change (its address completes the URL).
METHODS_PATH = re.search(r"var METHODS_URL = '([^']+)'", SCRIPT.read_text("utf-8")).group(1)
COPY = "cabinet.subs."
# Rendered by the shell itself (the page title, the strip's label, the loading line), not handed to the script.
NOT_IN_ISLAND = {"title", "strip.label", "loading"}
SEEDED_PREFIXES = (
    "api.subscriptions.",
    "api.common.",
    "api.orders.",
    "web.display_labels.",
    "api.payments.",
    "web.order_actions.",
    COPY,
)
HELP_URL = "__TELEGRAM_BOT_URL__"  # the configured bot link differs per environment

MONDAY = date(2026, 10, 26)
WEDNESDAY = MONDAY + timedelta(days=2)
START = datetime.combine(MONDAY, time(10, 0), tzinfo=TASHKENT).astimezone(UTC)


@pytest.fixture(autouse=True)
def seeded(db):
    """The page's, the subscription answers' and the display labels' copy, exactly as the seeder ships it."""
    for key, values in BACKEND_TRANSLATIONS.items():
        if not key.startswith(SEEDED_PREFIXES):
            continue
        for language, value in values.items():
            if value:
                db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()


# --------------------------------------------------------------------------- the customer's world


class _FrozenDatetimeMeta(type(datetime)):
    """So `isinstance(real_datetime, FrozenDatetime)` stays true for the module-level names we replace."""

    def __instancecheck__(cls, obj):
        return isinstance(obj, datetime)


def _frozen_datetime(instant: datetime):
    class FrozenDatetime(datetime, metaclass=_FrozenDatetimeMeta):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    return FrozenDatetime


class _World:
    """One customer (sample_user) on the website, at a frozen business clock."""

    def __init__(self, app, db, client, monkeypatch, user, address, water, cups, slot, admin=None):
        self.db, self.client, self.monkeypatch = db, client, monkeypatch
        self.app, self.admin = app, admin
        self.web, self.user = _Web(app, client, user), user
        self.address, self.water, self.cups, self.slot = address, water, cups, slot
        self.at(MONDAY)

    def at(self, day: date) -> None:
        self.now = _Clock(self.monkeypatch).at(day)
        # The pause / cancel write paths stamp `paused_at` and `end_date` from `datetime.now(UTC)` bound by name
        # in the model and the service; freeze those two names so no label reads the wall clock. The remaining
        # wall-clock stamps (`updated_at`, log `created_at`) are blanked by `_blank_stamps`.
        frozen = _frozen_datetime(self.now.astimezone(UTC))
        self.monkeypatch.setattr(subscription_model, "datetime", frozen)
        self.monkeypatch.setattr(subscription_service_module, "datetime", frozen)

    def create(self, name: str, items=None, **fields) -> int:
        lines = items or [{"product_id": self.water.id, "quantity": 2}]
        fields.setdefault("delivery_time_slot_id", self.slot.id)  # `None` is Any time
        sub_id = _create(
            self.web,
            self.address.id,
            lines,
            name=name,
            start_date=START.isoformat(),
            **fields,
        )
        # The list is newest-first by `created_at`; on a frozen clock every row ties, so keep creation order.
        self._row(sub_id).created_at = self.now.astimezone(UTC) + timedelta(seconds=sub_id)
        self.db.session.commit()
        return sub_id

    def daily_run(self, day: date = WEDNESDAY) -> None:
        self.at(day)
        placed_before = {order_id for (order_id,) in self.db.session.query(Order.id)}
        process_daily_subscription_billing()
        # The model stamps `created_at` and the payment service `online_payable_since` with the wall clock; the
        # payment deadline counts from them.
        for order in Order.query.filter(Order.id.notin_(placed_before)):
            order.created_at = self.now.astimezone(UTC)
            order.online_payable_since = self.now.astimezone(UTC)
        self.db.session.commit()

    def order(self, sub_id: int) -> Order:
        self.db.session.expire_all()
        [order] = Order.query.filter_by(subscription_id=sub_id).all()
        return order

    def _row(self, sub_id: int) -> Subscription:
        self.db.session.expire_all()
        return self.db.session.get(Subscription, sub_id)

    def pause(self, sub_id: int) -> None:
        assert self.web.post(f"{BASE}/{sub_id}/pause", {"expected_state": _state(self.web, sub_id)}).status_code == 200
        assert self._row(sub_id).paused_at.replace(tzinfo=UTC) == self.now.astimezone(UTC)  # the frozen clock

    def cancel(self, sub_id: int) -> None:
        assert self.web.post(f"{BASE}/{sub_id}/cancel", {"expected_state": _state(self.web, sub_id)}).status_code == 200
        assert self._row(sub_id).end_date.replace(tzinfo=UTC) == self.now.astimezone(UTC)  # the frozen clock

    def skip(self, sub_id: int, reason_code=None, reason_text=None) -> None:
        published = self.web.get(f"{BASE}/{sub_id}").get_json()["data"]["subscription"]
        body = {"expected_date": published["allowed_actions"]["skip"]["date"]}
        if reason_code:
            body["reason_code"] = reason_code
        if reason_text:
            body["reason_text"] = reason_text
        resp = self.web.post(f"{BASE}/{sub_id}/skip-next-delivery", body)
        assert resp.status_code == 200, resp.get_json()

    def discontinue(self, product: Product) -> None:
        product.is_active = False
        self.db.session.commit()

    def admin_headers(self) -> Dict[str, str]:
        """The admin UI's bearer token: `validate_admin_action` reads the role claim (CLAUDE.md)."""
        with self.app.app_context():
            token = create_access_token(identity=str(self.admin.id), additional_claims={"role": "admin"})
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def failed_attempt(self, sub_id: int) -> None:
        """What a declined Click attempt leaves: the payment FAILED, the order still unpaid."""
        order = self.order(sub_id)
        order.payment.status = PaymentStatus.FAILED
        self.db.session.commit()


@pytest.fixture
def world(app, db, client, monkeypatch, sample_user, user_address, sample_product, cups, every_day_slot, admin_user):
    return _World(
        app, db, client, monkeypatch, sample_user, user_address, sample_product, cups, every_day_slot, admin_user
    )


# --------------------------------------------------------------------------- the state matrix


def _needs_payment(w: _World):
    w.create("Home water")
    w.daily_run()  # Wednesday: Thursday's Click order is placed, unpaid


def _paid(w: _World):
    sub = w.create("Home water")
    w.daily_run()
    _mark_paid(w.db, w.order(sub).id)


def _cash(w: _World):
    w.create("Home water", payment_method="cash")
    w.daily_run()


def _on_the_way(w: _World):
    sub = w.create("Home water")
    w.daily_run()
    _picked_up(w.db, w.order(sub).id)


def _payment_failed(w: _World):
    sub = w.create("Home water")
    w.daily_run()
    w.failed_attempt(sub)


def _delivered_unpaid(w: _World):
    """Spec 2026-10-27 §8: the driver delivered the Click order and nothing was collected. It still needs payment, so
    the card's Pay is for that order, with the backend's note naming its delivery."""
    sub = w.create("Home water")
    w.daily_run()
    w.order(sub).status = OrderStatus.DELIVERED
    w.db.session.commit()


def _no_order_yet(w: _World):
    w.create("Home water")  # Monday: Thursday's order is placed on Wednesday


def _any_time(w: _World):
    """Step 4 Task 11b (spec §6.2): a subscription with no delivery time chosen (the slot is null): Any time."""
    w.create("Home water", delivery_time_slot_id=None)


def _paused(w: _World):
    w.pause(w.create("Home water"))


def _paused_kept_paid(w: _World):
    sub = w.create("Home water")
    w.daily_run()
    _mark_paid(w.db, w.order(sub).id)
    w.pause(sub)


def _cancelled(w: _World):
    sub = w.create("Home water")
    w.skip(sub, "away")
    w.cancel(sub)


def _cancelled_kept_paid(w: _World):
    sub = w.create("Home water")
    w.daily_run()
    _mark_paid(w.db, w.order(sub).id)
    w.cancel(sub)


def _paused_on_the_way(w: _World):
    """Paused while the driver has the unpaid Click order: it is kept, shown On the way, and still payable."""
    sub = w.create("Home water")
    w.daily_run()
    _picked_up(w.db, w.order(sub).id)
    w.pause(sub)


def _paused_on_the_way_paid(w: _World):
    """Paused while the driver has the paid order: it is kept and shown On the way."""
    sub = w.create("Home water")
    w.daily_run()
    _mark_paid(w.db, w.order(sub).id)
    _picked_up(w.db, w.order(sub).id)
    w.pause(sub)


def _paused_unpaid_leftover(w: _World):
    """D11: the unpaid order the pause kept for a new date (its delivery failed) was then rescheduled by staff. It is
    unpaid and nobody has it: the paused card must not call it paid, and Pay is still offered."""
    _needs_payment(w)
    order = w.order(1)
    _fail_delivery(w, order)
    w.pause(1)
    resp = w.client.patch(
        f"{API}/admin/orders/{order.id}/schedule", json={"delivery_date": "2026-10-30"}, headers=w.admin_headers()
    )
    assert resp.status_code == 200, resp.get_json()


def _cancelled_on_the_way(w: _World):
    """Cancelled while the driver has the unpaid Click order: it is kept, shown On the way, and still payable."""
    sub = w.create("Home water")
    w.daily_run()
    _picked_up(w.db, w.order(sub).id)
    w.cancel(sub)


def _fail_delivery(w: _World, order: Order) -> None:
    """The delivery failed: the order waits for a new date."""
    if order.delivery is None:
        w.db.session.add(
            Delivery(
                order_id=order.id,
                status=DeliveryStatus.FAILED,
                scheduled_date=w.now.astimezone(UTC),
                scheduled_time_slot="anytime",
                delivery_attempts=1,
                failed_delivery_reason="customer_unavailable",
            )
        )
    else:
        order.delivery.status = DeliveryStatus.FAILED
    w.db.session.commit()


def _awaiting_new_date(w: _World):
    """The placed unpaid order's delivery failed: it waits for a new date. Pause and cancel keep it, and the
    page must not call it paid (final review Minor 2)."""
    sub = w.create("Home water")
    w.daily_run()
    _fail_delivery(w, w.order(sub))


def _not_cancellable(w: _World):
    """Re-review G8: the placed unpaid order is past PENDING over a leftover final delivery row (returned), so the
    customer's own cancel rule keeps it as NOT_CANCELLABLE: not paid, not with the driver, not waiting for a new
    date. The cancel modal words it with its own line and never calls it paid."""
    sub = w.create("Home water")
    w.daily_run()
    order = w.order(sub)
    order.status = OrderStatus.CONFIRMED
    if order.delivery is None:
        w.db.session.add(
            Delivery(
                order_id=order.id,
                status=DeliveryStatus.RETURNED,
                scheduled_date=w.now.astimezone(UTC),
                scheduled_time_slot="anytime",
                delivery_attempts=1,
            )
        )
    else:
        order.delivery.status = DeliveryStatus.RETURNED
    w.db.session.commit()


def _past_dated(w: _World):
    """The clock has passed the next delivery before the 09:00 job moved it on (final review Minor 12): the
    date is in the past, so the backend publishes no "in N days"."""
    w.create("Home water")
    w.at(MONDAY + timedelta(days=4))  # Friday, after Thursday's delivery date; no daily run yet


def _discontinued_some(w: _World):
    """Review Focus 2: one of two products is discontinued; the order goes out without it."""
    w.create("Home water", items=[{"product_id": w.water.id, "quantity": 2}, {"product_id": w.cups.id, "quantity": 1}])
    w.discontinue(w.cups)
    w.daily_run()


def _discontinued_all(w: _World):
    """Review Focus 2: the only product is discontinued; the delivery is skipped."""
    w.create("Spring water", items=[{"product_id": w.cups.id, "quantity": 3}])
    w.discontinue(w.cups)
    w.daily_run()


def _history_paging(w: _World):
    """Seventeen skipped deliveries: five rows, "Show more orders" appends ten, then the last two."""
    sub = w.create("Home water")
    reasons = [("still_have_water", None), ("away", None), ("other", "Moving house"), (None, None)]
    for n in range(17):
        code, words = reasons[n % len(reasons)]
        w.skip(sub, code, words)


def _mixed(w: _World):
    """The strip's order: created needs-payment, active, paused, cancelled; listed newest first."""
    w.create("Home water")
    w.create("Office cooler set", payment_method="cash")
    w.pause(w.create("Dacha 10 L"))
    w.cancel(w.create("Old plan"))
    w.daily_run()


def _method_not_offered(w: _World):
    """B8 on the card (spec 2026-10-27 §7): cash was chosen at "Home", then that place reached the cash-debt limit, so
    cash is no longer on its menu. The cash subscription publishes the warning; the Click one beside it does not."""
    w.address.title = "Home"
    w.db.session.commit()
    w.create("Home water", payment_method="cash")
    w.create("Office cooler set")
    _cap_the_place(w.db, w.user, w.address)


def _all_cancelled(w: _World):
    w.cancel(w.create("Old plan"))
    w.cancel(w.create("Summer plan"))


def _empty(w: _World):
    pass


@dataclass(frozen=True)
class Case:
    name: str
    language: str
    build: Callable[[_World], None]
    about: str


CASES = [
    Case("needs_payment", "en", _needs_payment, "Active, Click order placed and unpaid (Pay is the primary action)."),
    Case("needs_payment_uz", "uz", _needs_payment, "needs_payment in Uzbek."),
    Case("needs_payment_ru", "ru", _needs_payment, "needs_payment in Russian."),
    Case("paid", "en", _paid, "Active, the placed order is paid."),
    Case("cash", "en", _cash, "Active, cash: pay on delivery."),
    Case("on_the_way", "en", _on_the_way, "Active, the driver has the order."),
    Case("payment_failed", "en", _payment_failed, "The last Click attempt FAILED; Pay is still offered (RF3)."),
    Case("delivered_unpaid", "en", _delivered_unpaid, "The Click order was delivered and is not paid yet (§8)."),
    Case("no_order_yet", "en", _no_order_yet, "Active, no order placed yet."),
    Case("any_time", "en", _any_time, "Active, no delivery time chosen: Any time (Task 11b)."),
    Case("paused", "en", _paused, "Paused, nothing kept."),
    Case("paused_kept_paid", "en", _paused_kept_paid, "Paused with a kept paid order (RF4)."),
    Case("paused_ru", "ru", _paused, "Paused, in Russian."),
    Case("cancelled", "en", _cancelled, "Cancelled after a skip; history stays visible."),
    Case("cancelled_kept_paid", "en", _cancelled_kept_paid, "Cancelled with a kept paid order (RF4)."),
    Case("paused_on_the_way", "en", _paused_on_the_way, "Paused while the driver has the unpaid order (§12)."),
    Case("paused_on_the_way_paid", "en", _paused_on_the_way_paid, "Paused while the driver has the paid order (§12)."),
    Case(
        "paused_unpaid_leftover",
        "en",
        _paused_unpaid_leftover,
        "Paused; the unpaid order kept for a new date was rescheduled by staff (D11).",
    ),
    Case("cancelled_on_the_way", "en", _cancelled_on_the_way, "Cancelled while the driver has the unpaid order (§12)."),
    Case("awaiting_new_date", "en", _awaiting_new_date, "The placed order's delivery failed; it awaits a new date."),
    Case("not_cancellable", "en", _not_cancellable, "The placed unpaid order is kept over a returned delivery row."),
    Case("past_dated", "en", _past_dated, "The next delivery date has passed before the daily job ran."),
    Case("discontinued_some", "en", _discontinued_some, "One of two products discontinued (RF2)."),
    Case("discontinued_all", "en", _discontinued_all, "The only product discontinued (RF2)."),
    Case("history_paging", "en", _history_paging, "Seventeen history rows for Show more orders."),
    Case("mixed", "en", _mixed, "Four subscriptions: needs payment, active, paused, cancelled."),
    Case(
        "method_not_offered",
        "en",
        _method_not_offered,
        "A cash subscription at an address whose place reached the cash-debt limit, beside a Click one (B8).",
    ),
    Case("all_cancelled", "en", _all_cancelled, "Every subscription cancelled."),
    Case("empty", "en", _empty, "No subscriptions."),
]


# --------------------------------------------------------------------------- capture


def _speaks(db, user, language: str) -> None:
    """The customer's own language: a Uzbek customer's `?lang=uz` (the default) is dropped from the URL, so the
    page then answers in their stored preference, as it does for them."""
    user.preferred_language = language
    db.session.commit()


def _visit(w: _World, language: str) -> None:
    """The page visit: it fixes the session's language, which the page's API calls then answer in."""
    _speaks(w.db, w.user, language)
    resp = w.web._client.get(f"/my-subscriptions?lang={language}", follow_redirects=True)
    assert resp.status_code == 200


def _blank_stamps(value):
    if isinstance(value, dict):
        return {
            key: ("<wall-clock>" if key.endswith("_at") and isinstance(item, str) else _blank_stamps(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_blank_stamps(item) for item in value]
    return value


def _rename(value, names: Dict[str, str]):
    if isinstance(value, dict):
        return {key: _rename(item, names) for key, item in value.items()}
    if isinstance(value, list):
        return [_rename(item, names) for item in value]
    if isinstance(value, str):
        for real, stable in names.items():
            value = value.replace(real, stable)
    return value


def _capture(w: _World, language: str) -> Dict[str, dict]:
    """The requests my-subscriptions.js sends, in its order, with the real answers."""
    _visit(w, language)
    requests: Dict[str, dict] = {}

    def get(path):
        resp = w.web.get(API + path)
        requests[f"GET {API}{path}"] = {"status": resp.status_code, "body": resp.get_json()}
        assert resp.status_code == 200, resp.get_json()
        return resp.get_json()

    listed = get(LIST_PATH)
    for sub in listed["data"]["items"]:
        page = 1
        while True:
            body = get(f"/subscriptions/{sub['id']}/history?page={page}&per_page={HISTORY_PAGE_SIZE}")
            if not body["meta"]["has_next"]:
                break
            page += 1
        if sub["allowed_actions"]["edit"]["allowed"]:
            get(f"{METHODS_PATH}{sub['delivery_address_id']}")
    w.db.session.expire_all()
    names = {order.order_number: f"TG_{order.id:06d}_26" for order in Order.query.all()}
    return _rename(_blank_stamps(requests), names)


def _fixture(name: str, generated):
    path = FIXTURES / name
    if UPDATE:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(generated, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert path.exists(), f"{path} is missing: run with UPDATE_WEB_SUBSCRIPTION_FIXTURES=1"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(generated)), (
        f"{path.name} is stale: the endpoints answer differently now. Regenerate it with "
        "UPDATE_WEB_SUBSCRIPTION_FIXTURES=1 and re-run admin_ui web/mySubscriptions.test.js."
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_state_fixture_is_what_the_endpoints_answer(world, case):
    case.build(world)
    requests = _capture(world, case.language)

    _fixture(
        f"{case.name}.json",
        {
            "case": case.name,
            "language": case.language,
            "about": case.about,
            "requests": requests,
        },
    )
    # Every subscription in the list answers from the real serializer (never its partial fallback).
    listed = requests[f"GET {API}{LIST_PATH}"]["body"]["data"]["items"]
    assert all("error" not in sub for sub in listed)


@pytest.mark.parametrize(
    "case, reason", [("awaiting_new_date", "AWAITING_NEW_DATE"), ("not_cancellable", "NOT_CANCELLABLE")]
)
def test_each_kept_order_fixture_publishes_the_reason_the_cancel_modal_words(case, reason):
    """The cancel modal words a kept order by its published `reason_code` (mySubscriptions.test.js renders these
    two fixtures): each case must really reach the branch it is named for."""
    listed = json.loads((FIXTURES / f"{case}.json").read_text("utf-8"))["requests"][f"GET {API}{LIST_PATH}"]
    [sub] = listed["body"]["data"]["items"]
    assert [order["reason_code"] for order in sub["allowed_actions"]["cancel"]["kept_orders"]] == [reason]


def _fixture_sub(case: str) -> dict:
    """The one subscription the saved fixture of `case` lists (the answer the page and the bot's web twin render)."""
    listed = json.loads((FIXTURES / f"{case}.json").read_text("utf-8"))["requests"][f"GET {API}{LIST_PATH}"]
    [sub] = listed["body"]["data"]["items"]
    return sub


SENTENCES = {
    "needs_payment": {
        ("skip", "confirm_text"): "Skip Thursday, 29 October? We'll cancel that order. Your next delivery will be Thursday, 5 November.",
        ("skip", "confirm_label"): "Skip 29 October",
        ("skip", "keep_label"): "Keep this delivery",
        ("pause", "confirm_text"): "Pause deliveries? We won't place new orders until you resume.",
        ("pause", "cancel_lines"): ["We'll cancel your order for 29 October."],
        ("pause", "needs_confirm"): True,
        ("cancel", "confirm_text"): "We won't place any more automatic orders.",
        ("cancel", "cancel_lines"): ["We'll cancel your order for 29 October."],
        ("cancel", "kept_lines"): [],
        ("cancel", "coins_line"): "Your AquaCoins stay with you.",
        ("skip", "label"): "Skip this delivery",
        ("pause", "label"): "Pause",
        ("cancel", "label"): "Cancel subscription",
    },
    "paid": {
        ("skip", "confirm_text"): "This order is paid, so it will still arrive on 29 October. We'll skip the one after it, on 5 November.",
        ("skip", "keep_label"): "Keep both",
        ("pause", "needs_confirm"): False,
        ("cancel", "kept_lines"): ["Your paid order for Thursday, 29 October, 10:00–12:00 will still be delivered."],
    },
    "on_the_way": {
        ("skip", "confirm_text"): "Your driver already has your order for Thursday, 29 October, so it will still arrive. We'll skip the one after it, on 5 November.",
        ("cancel", "kept_lines"): ["Your order for Thursday, 29 October, 10:00–12:00 is on the way."],
    },
    "no_order_yet": {
        ("skip", "confirm_text"): "Skip Thursday, 29 October? Your next delivery will be Thursday, 5 November.",
    },
    "awaiting_new_date": {
        ("cancel", "kept_lines"): ["Your order TG_000001_26 stays: we'll deliver it on its new date."],
    },
    "not_cancellable": {("cancel", "kept_lines"): ["Your order TG_000001_26 stays as it is."]},
    "needs_payment_ru": {
        ("skip", "confirm_text"): "Пропустить доставку: четверг, 29 октября? Мы отменим этот заказ. Следующая доставка: четверг, 5 ноября.",
    },
    "paused": {("resume", "label"): "Resume deliveries"},
    "delivered_unpaid": {("pay", "allowed"): True, ("pay", "note"): "For your order delivered on 29 October."},
}


@pytest.mark.parametrize("case", SENTENCES)
def test_each_state_fixture_publishes_its_finished_sentences(case):
    sub = _fixture_sub(case)
    for (action, field), expected in SENTENCES[case].items():
        assert sub["allowed_actions"][action][field] == expected, (case, action, field)


def test_the_pay_note_is_the_upcoming_payment_note():
    sub = _fixture_sub("needs_payment")
    assert sub["allowed_actions"]["pay"]["note"] == sub["upcoming_delivery"]["payment_note"]
    assert sub["allowed_actions"]["pay"]["label"].startswith("Pay ")


def test_a_refused_action_publishes_no_label():
    """A published action's label is None whenever it is not allowed (plan Global Constraints)."""
    actions = _fixture_sub("paused")["allowed_actions"]
    for action in ("pay", "skip", "pause"):
        assert (actions[action]["allowed"], actions[action]["label"]) == (False, None), action
    for action in ("resume", "cancel"):
        assert actions[action]["allowed"] is True and actions[action]["label"], action


@pytest.mark.parametrize(
    "case, state, kept_line",
    [
        ("paused_kept_paid", "paid_kept", "Your paid order for Thursday, 29 October, 10:00–12:00 will still be delivered."),
        ("paused_on_the_way", "on_the_way", "Your order for Thursday, 29 October, 10:00–12:00 is on the way."),
        ("paused_on_the_way_paid", "on_the_way", "Your order for Thursday, 29 October, 10:00–12:00 is on the way."),
        ("cancelled_on_the_way", "on_the_way", "Your order for Thursday, 29 October, 10:00–12:00 is on the way."),
        ("paused_unpaid_leftover", "placed", None),
    ],
)
def test_a_paused_card_calls_an_order_paid_only_when_it_is(case, state, kept_line):
    sub = _fixture_sub(case)
    upcoming = sub["upcoming_delivery"]
    assert (upcoming["state"], upcoming["kept_line"]) == (state, kept_line)
    if case == "paused_unpaid_leftover":
        assert upcoming["payment_status"]["code"] == "awaiting_payment"
        assert sub["allowed_actions"]["pay"]["allowed"] is True


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_any_time_tile_says_what_the_card_says(world, seeded, language):
    """The Schedule editor's Any time tile is the island's `slot.any`; the card's own time is the backend's
    `time_label`. One wording (D14): the two are the same words in every language."""
    _any_time(world)
    _visit(world, language)
    resp = world.web.get(API + LIST_PATH)
    assert resp.status_code == 200, resp.get_json()
    [sub] = resp.get_json()["data"]["items"]

    tile = _island(_render(world.app, world.user, language))["i18n"]["slot.any"]

    assert sub["upcoming_delivery"]["time_label"] == BACKEND_TRANSLATIONS[COPY + "slot.any"][language] == tile


def _listed(w: _World) -> list:
    """The subscriptions as the page lists them, in the customer's language."""
    _visit(w, "en")
    resp = w.web.get(API + LIST_PATH)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["items"]


def test_a_method_the_address_no_longer_offers_is_published_as_a_warning(world, seeded):
    """The one published B8 answer (spec 2026-10-27 §7): the cabinet and the bot render it as it is."""
    _method_not_offered(world)

    by_method = {item["payment_method"]: item for item in _listed(world)}

    assert by_method["cash"]["payment_method_warning"] == "Not available right now. Choose another method."
    assert by_method["cash"]["delivery_address_label"] == "Home"
    assert by_method["click"]["payment_method_warning"] is None
    assert by_method["click"]["delivery_address_label"] == "Home"


def test_the_warning_and_the_write_guard_are_one_rule(world, seeded):
    """A PUT naming the subscription's own address re-asks B8 there: it is refused with
    SUBSCRIPTION_PAYMENT_METHOD_REQUIRED exactly when the card publishes `payment_method_warning`."""
    _method_not_offered(world)

    outcomes = {}
    for item in _listed(world):
        resp = world.web.put(
            f"{BASE}/{item['id']}",
            {
                "delivery_address_id": item["delivery_address_id"],
                "expected_state": item["allowed_actions"]["expected_state"],
            },
        )
        assert resp.status_code in (200, 400), resp.get_json()
        refused = resp.get_json()["data"] if resp.status_code == 400 else {}
        required = refused.get("error_code") == "SUBSCRIPTION_PAYMENT_METHOD_REQUIRED"
        outcomes[item["payment_method"]] = (required, item["payment_method_warning"] is not None)

    assert outcomes == {"cash": (True, True), "click": (False, False)}


@pytest.mark.parametrize("status", list(SubscriptionStatus), ids=lambda status: status.value)
def test_the_warning_follows_the_edit_guard_in_every_status(world, seeded, status):
    """The warning promises a re-pick on the next save, so it is published only where the customer's edit is allowed:
    present exactly when the address PUT answers SUBSCRIPTION_PAYMENT_METHOD_REQUIRED, never beside the closed-edit
    refusal (cancelled, expired, trial)."""
    _method_not_offered(world)
    cash = next(item for item in _listed(world) if item["payment_method"] == "cash")
    row = world.db.session.get(Subscription, cash["id"])
    row.status = status
    world.db.session.commit()

    item = next(item for item in _listed(world) if item["id"] == cash["id"])
    resp = world.web.put(
        f"{BASE}/{item['id']}",
        {"delivery_address_id": item["delivery_address_id"], "expected_state": item["allowed_actions"]["expected_state"]},
    )

    assert resp.status_code == 400, resp.get_json()
    error_code = resp.get_json()["data"]["error_code"]
    assert error_code in ("SUBSCRIPTION_PAYMENT_METHOD_REQUIRED", "SUBSCRIPTION_ACTION_NOT_ALLOWED")
    assert (item["payment_method_warning"] is not None) == (error_code == "SUBSCRIPTION_PAYMENT_METHOD_REQUIRED")
    live = status in LIVE_SUBSCRIPTION_STATUSES
    assert (error_code == "SUBSCRIPTION_PAYMENT_METHOD_REQUIRED") == live


def test_the_state_matrix_is_covered():
    """The fixtures carry every state the brief's matrix names (spec §3), so the JS test renders each."""
    states = {}
    for case in CASES:
        listed = json.loads((FIXTURES / f"{case.name}.json").read_text("utf-8"))["requests"][f"GET {API}{LIST_PATH}"]
        for sub in listed["body"]["data"]["items"]:
            upcoming = sub["upcoming_delivery"] or {}
            states.setdefault(case.name, set()).add((sub["status"], upcoming.get("state"), sub["items_availability"]))
    flat = set().union(*states.values())
    for expected in [
        ("active", "placed", "all"),
        ("active", "on_the_way", "all"),
        ("active", "planned", "all"),
        ("paused", None, "all"),
        ("paused", "paid_kept", "all"),
        ("cancelled", None, "all"),
        ("cancelled", "paid_kept", "all"),
        ("paused", "on_the_way", "all"),
        ("cancelled", "on_the_way", "all"),
        ("active", "placed", "some"),
        ("active", "planned", "none"),
    ]:
        assert expected in flat, expected
    assert (
        json.loads((FIXTURES / "empty.json").read_text("utf-8"))["requests"][f"GET {API}{LIST_PATH}"]["body"]["data"][
            "items"
        ]
        == []
    )


# --------------------------------------------------------------------------- the shell


def _render(app, sample_user, language: str) -> BeautifulSoup:
    from business_app import db

    _speaks(db, sample_user, language)
    client = app.test_client()
    _Web(app, client, sample_user)
    app.jinja_env.cache.clear()
    resp = client.get(f"/my-subscriptions?lang={language}", follow_redirects=True)
    assert resp.status_code == 200
    return BeautifulSoup(resp.get_data(as_text=True), "html.parser")


def _island(soup: BeautifulSoup) -> dict:
    return json.loads(soup.select_one("script#page-data").string)


def _shell(app, sample_user) -> dict:
    shell = {}
    for language in LANGUAGES:
        soup = _render(app, sample_user, language)
        island = _island(soup)
        island["help_url"] = HELP_URL
        # The shared order modal the page includes, and its own island, for the modal's half of the JS test.
        modal = {
            "html": str(soup.select_one("#orderDetailModal")),
            "island": json.loads(soup.select_one("script#order-detail-modal-data").string),
        }
        shell[language] = {"html": str(soup.select_one("div.subs")), "island": island, "modal": modal}
    return shell


def test_the_shell_fixture_is_what_the_page_renders(app, db, sample_user):
    _fixture("shell.json", _shell(app, sample_user))


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_island_word_is_the_seeded_row_in_that_language(app, db, sample_user, language):
    soup = _render(app, sample_user, language)
    island = _island(soup)

    assert island["builder_url"] == "/#subscription-constructor"
    assert island["help_url"].startswith("http")
    words = island["i18n"]
    assert words, "the island carries no words"
    for key, value in words.items():
        assert value == BACKEND_TRANSLATIONS[COPY + key][language], (language, key, value)
        assert COPY not in value
    # The page title is the page's own key too.
    assert BACKEND_TRANSLATIONS[COPY + "title"][language] in soup.title.get_text()


def test_the_island_hands_over_every_page_key_and_no_other():
    seeded = {key[len(COPY) :] for key in BACKEND_TRANSLATIONS if key.startswith(COPY)}
    listed = re.search(r"\{% for key in \[(.*?)\] %\}", TEMPLATE.read_text("utf-8"), re.S).group(1)
    in_island = set(re.findall(r"'([a-z_.]+)'", listed))
    assert in_island == seeded - NOT_IN_ISLAND
    # Every key the script reads is in the island.
    script = SCRIPT.read_text("utf-8")
    read = set(re.findall(r"\btext\('([a-z_.]+)'", script)) | set(re.findall(r"\bhtml\('([a-z_.]+)'", script))
    # text('prefix.' + code)
    dynamic = {
        "change_label.",
        "wl.state.",
        "wl.legend.",
        "payment.note.",
        "frequency.",
        "schedule.hint.",
    }
    assert read - in_island - dynamic == set()
    for prefix in dynamic:
        assert any(key.startswith(prefix) for key in in_island), prefix


def test_the_script_chooses_no_sentence():
    """Spec 2026-10-27 §16 M3: every consequence sentence and action label is the backend's (`allowed_actions`,
    `customer_actions`, `upcoming_delivery`); the script renders them and picks none of its own."""
    for banned in (
        "skipWords",
        "keptLine",
        "text('skip.text_",
        "text('pause.text'",
        "text('cancel.kept_",
        "text('cancel.will_cancel'",
        "hero.time_tbc",
        "hero.kept_",
        "text('hero.pay'",
        "text('hero.skip'",
        "text('hero.pause'",
        "text('hero.resume'",
        "text('footer.cancel'",
        "offered.indexOf(sub.payment_method)",
        "skip.reason.",
    ):
        assert banned not in SCRIPT.read_text("utf-8"), banned


@pytest.mark.parametrize("key", sorted(k for k in BACKEND_TRANSLATIONS if k.startswith(COPY)))
def test_every_page_row_is_trilingual_with_the_same_placeholders(key):
    row = BACKEND_TRANSLATIONS[key]
    assert all(row.get(language) for language in LANGUAGES), key
    placeholders = {language: set(re.findall(r"\{(\w+)\}", row[language])) for language in LANGUAGES}
    assert placeholders["uz"] == placeholders["en"] == placeholders["ru"], (key, placeholders)
    for language in LANGUAGES:
        lowered = row[language].lower()
        for word in ("call us", "discount", "savings", "you saved", "позвон", "скидк", "qo'ng'iroq", "chegirma"):
            assert word not in lowered, (key, language, word)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_shell_has_the_containers_the_renderer_fills(app, db, sample_user, language):
    soup = _render(app, sample_user, language)
    subs = soup.select_one("div.subs")

    assert subs.select_one("symbol#subs-bottle") is not None
    status = subs.select_one("#subs-status")
    assert (status["role"], status["aria-live"]) == ("status", "polite")
    nav = subs.select_one("nav#subs-strip-nav")
    assert nav["aria-label"] == BACKEND_TRANSLATIONS[COPY + "strip.label"][language]
    assert nav.select_one("ul#subs-strip") is not None
    section = subs.select_one("section#subscription")
    assert (section["aria-labelledby"], section["aria-busy"]) == ("sub-h", "true")
    # The page's only two modals (UX brief): Cancel and the product picker, inside the .subs scope.
    for modal in ("subs-cancel-modal", "subs-picker-modal"):
        found = subs.select_one(f"div.modal#{modal}")
        assert found is not None and found["role"] == "dialog", modal
    island = _island(soup)
    assert len(island["weekday_names"]) == len(island["weekday_short_names"]) == 7
    assert island["method_labels"]["cash"] == BACKEND_TRANSLATIONS["web.display_labels.payment_method.cash"][language]
    # The shared order modal, after the base scripts (its script reads getPageData as it loads).
    assert soup.select_one("#orderDetailModal") is not None
    scripts = [tag.get("src", "") for tag in soup.find_all("script")]
    page_data = next(i for i, src in enumerate(scripts) if src.endswith("/_page-data.js"))
    modal = next(i for i, src in enumerate(scripts) if src.endswith("/order-detail-modal.js"))
    page = next(i for i, src in enumerate(scripts) if src.endswith("/my-subscriptions.js"))
    assert page_data < modal < page
    # The page's Pay and an Orders row's Skip/Cancel write through order-actions.js, which the modal's partial loads
    # once, before the page script (spec 2026-10-27 §5.3, §8).
    order_actions = [i for i, src in enumerate(scripts) if src.endswith("/order-actions.js")]
    assert len(order_actions) == 1 and order_actions[0] < page
    assert soup.select_one('link[href$="/css/pages/my-subscriptions.css"]') is not None


def test_the_fake_plans_and_the_dead_panel_are_gone():
    source = TEMPLATE.read_text("utf-8") + SCRIPT.read_text("utf-8")
    for dead in (
        "$29",
        "$49",
        "$99",
        "Modify Plan",
        "modifyModal",
        "upcoming-deliveries",
        "Upcoming Deliveries",
        "subscriptionModal",
        "Browse Plans",
        "stat-card",
    ):
        assert dead not in source, dead


def test_the_script_does_no_date_or_money_maths_and_no_dialogs():
    """Spec §3 anti-goals: every date and amount is a backend label; no native dialogs, no toasts."""
    script = SCRIPT.read_text("utf-8")
    for banned in (
        "new Date(",
        "Date.parse",
        "toLocale",
        "Intl.",
        "toUpperCase(",
        "confirm(",
        "prompt(",
        "alert(",
        "showNotification(",
        "toFixed(",
        "parseFloat(",
        "UZS",
    ):
        assert banned not in script, banned


def test_the_home_page_manage_link_opens_my_subscriptions(app, db, client):
    app.jinja_env.cache.clear()
    soup = BeautifulSoup(
        app.test_client().get("/?lang=en", follow_redirects=True).get_data(as_text=True), "html.parser"
    )
    [link] = soup.select("#subscription-constructor .more-btn a.theme-btn")
    assert link["href"] == "/my-subscriptions"


def test_the_index_stays_the_builder_home():
    """`builder_url` (New subscription, the empty state) is the builder on the home page."""
    index = (REPO / "business_app" / "templates" / "frontend" / "index.html").read_text("utf-8")
    assert 'id="subscription-constructor"' in index
