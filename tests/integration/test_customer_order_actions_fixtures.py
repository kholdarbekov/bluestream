"""The shared order-actions fixtures (plan 2026-10-27 Task 10a; spec 2026-10-27 §5.3, §8, D1, D10-D13).

`customer_actions` is the backend's finished answer to "what may this customer do with this order": Pay, the
removal (skip, cancel or decline) and the store's Confirm. The website's three order surfaces (the My orders cards,
the order modal and the tracking page) render it through ONE script, `static/js/pages/order-actions.js`, and the
bot renders the same answers (Task 13). This module is the ONE emitter of `tests/fixtures/customer_order_actions/
<case>.json`, which three suites read: `admin_ui/src/__tests__/web/orderSurfaces.test.js` (the shipped script in
jsdom), Task 13's bot tests and Task 20b's. Each file holds

    {"case", "language", "order_id",
     "requests":  {"GET /api/v1/orders/<id>", "GET /api/v1/orders?page=1&per_page=10",
                   "GET /api/v1/orders/?page=1&per_page=10" (the bot's spelling, same answer), "tracking_island"},
     "requests_after": the order and list reads once the screen went stale (`click_unpaid_stale`, `stale_via` only),
     "exchanges": {<name>: {"method", "path", "body", "status", "response"}}}

`requests` are the reads the pages send (answers as `{status, body}`; `tracking_island` is the `order` the tracking
page's own island carries), taken through the website (cookie JWT + CSRF) AND through the bot (bearer token), which
must answer the same. Each exchange is one write, replayed on a freshly built world (so every exchange starts from
the same state), through the endpoint the client calls with the body it sends. A case on a subscription also holds
`GET /api/v1/subscriptions/1` and `/items` as they answer after the case's `sub_after` write: the screen the bot
opens after a skip or a cancel. `shell.json` holds `{en, ru, uz} x {orders, modal, tracking} -> {html, island}`.

Each test regenerates its slice of the file and fails when it differs (a stale fixture would let the JS test pass
against answers the backend no longer gives). Regenerate with `UPDATE_CUSTOMER_ORDER_ACTIONS_FIXTURES=1`.

Determinism: the business clock is frozen (it starts at Monday 26 October 2026 10:00 Tashkent, the world's; a case that
runs the daily billing moves it to that run's day, so `subscription_later_order` reads Tuesday 27 October), the payment window
starts at a fixed offset from it, wall-clock stamps are blanked, and order numbers (random on SQLite) are renamed by
order id. Only external I/O is mocked: Celery, the route notifications, the Click link.
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple, Union
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from flask_jwt_extended import create_access_token

from business_app.frontend.routes import ORDER_ACTIONS_WORDS, order_actions_words
from business_app.models.order import Order, OrderItem
from business_app.models.payment import Payment
from business_app.models.product import ProductFiscalProfile
from business_app.models.sales_visits import OrderConfirmationRequest
from business_app.models.subscription import Subscription, SubscriptionLog
from business_app.tasks import sales_agent_tasks
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, SubscriptionStatus
from shared.staff_constants import SALES_EVENT_AGENT_ORDER_DECLINED
from tests.integration.customer_action_matrix import ROW, Row, seed
from tests.integration.test_my_subscriptions_page import (  # noqa: F401  (fixtures)
    LANGUAGES,
    MONDAY,
    _blank_stamps,
    _external_io,
    _needs_payment,
    _paused_unpaid_leftover,
    _rename,
    _speaks,
    _World,
    cups,
    every_day_slot,
    seeded,
    world,
)
from tests.integration.test_subscription_action_guards import _Web
from tests.integration.test_web_subscription_management_contract import _blank_timestamps
from tests.unit.test_agent_order_confirmation import _place_order, _spy, agent, linked_outlet  # noqa: F401

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "customer_order_actions"
ORDERS_SCRIPT = REPO / "business_app" / "static" / "js" / "pages" / "orders.js"
UPDATE = os.environ.get("UPDATE_CUSTOMER_ORDER_ACTIONS_FIXTURES") == "1"
API = "/api/v1"
# orders.js `loadOrders(1)`: apiRequest('/orders?' + URLSearchParams({page: 1, per_page: N})), N read from the source.
PER_PAGE = re.search(r"\{ page: page, per_page: (\d+) \}", ORDERS_SCRIPT.read_text("utf-8")).group(1)
LIST_KEY = f"GET {API}/orders?page=1&per_page={PER_PAGE}"
# The bot's orders_menu reads the list with a trailing slash (telegram_bot/api_client.py); the same answer, recorded so a
# bot replay can look its own spelling up.
BOT_LIST_KEY = f"GET {API}/orders/?page=1&per_page={PER_PAGE}"
CLICK_URL = "http://localhost/my-orders#click-checkout"
STALE_PRICE = "32000.00"


def detail_key(order_id: int) -> str:
    return f"GET {API}/orders/{order_id}"


@pytest.fixture(autouse=True)
def click_link():
    """The Click checkout link is external I/O; here it is a same-document address the Vitest run can follow."""
    with patch(
        "business_app.services.payment_service.PaymentService.create_payment_link",
        lambda self, payment_id, **_: {"payment_url": CLICK_URL},
    ):
        yield


# --------------------------------------------------------------------------- the cases


@dataclass(frozen=True)
class Built:
    """The order a case is about, and the customer who holds it (the store, for an agent's order)."""

    order_id: int
    user: object


@dataclass(frozen=True)
class Exchange:
    """One write: `path` is the route under /api/v1/orders/<id>/, `via` the client that sends it ("web": cookie JWT
    + CSRF; "bot": bearer token), `status` what it answers. `prepare` moves the world after the screen was read
    (what makes a screen stale); `effect` asserts what the write did to the database."""

    name: str
    path: str
    body: dict
    status: int
    via: str = "web"
    prepare: Optional[Callable] = None
    effect: Optional[Callable] = None


@dataclass(frozen=True)
class Case:
    name: str
    build: Callable[[_World, object], Built]
    expected: Tuple[bool, Optional[str], bool]  # (pay.allowed, cancel.via, confirm.allowed)
    exchanges: Tuple[Exchange, ...] = ()
    language: str = "en"
    after_read: Optional[Callable] = None
    sub_after: Optional[str] = None
    # Record the order and list reads again once `after_read` has moved the world (`requests_after`): what a client
    # that reloads a stale screen is shown.
    reads_after: bool = False


def _row_case(row: Row):
    def build(w: _World, request) -> Built:
        order = seed(w.db, w.user, row, now=w.now.astimezone(UTC))
        return Built(order.id, w.user)

    return build


def _daily_later_order(w: _World, request) -> Built:
    """A daily Click subscription: creating it places order 1 (Tuesday 27 October) and Tuesday's run places order 2
    (Wednesday 28 October), so two unpaid orders stand and order 2 is the later one. The clock reads Tuesday."""
    w.create("Home water", frequency="daily", delivery_day_of_week=None)
    w.daily_run(MONDAY + timedelta(days=1))
    unpaid = Order.query.filter_by(subscription_id=1, status=OrderStatus.PENDING).order_by(Order.id).all()
    assert [o.id for o in unpaid] == [1, 2], "the case holds two unpaid orders and order 2 is the later one"
    assert unpaid[1].delivery_date > unpaid[0].delivery_date
    return Built(2, w.user)


def _needs_payment_order(w: _World, request) -> Built:
    _needs_payment(w)
    return Built(1, w.user)


def _leftover_order(w: _World, request) -> Built:
    _paused_unpaid_leftover(w)
    return Built(1, w.user)


def _agent_order(w: _World, request) -> Built:
    """An order a sales agent placed for the store, through the agent's real flow, with the store's question open."""
    agent_user = request.getfixturevalue("agent")
    outlet = request.getfixturevalue("linked_outlet")
    order, _ = _place_order(agent_user, outlet, w.water)
    # The agent's flow stamps `created_at` from the wall clock; the order card prints it as "Placed on <day>".
    order.created_at = w.now.astimezone(UTC)
    w.db.session.commit()
    return Built(order.id, outlet.user)


def _total_moves(w: _World, order_id: int) -> None:
    order = w.db.session.get(Order, order_id)
    order.total_amount = STALE_PRICE
    w.db.session.commit()


def _subscription_pauses(w: _World, order_id: int) -> None:
    """The subscription row is paused directly, after the screen was read."""
    w.db.session.get(Subscription, 1).status = SubscriptionStatus.PAUSED
    w.db.session.commit()


def _pool_runs_short(w: _World, order_id: int) -> None:
    """The Payme order holds a marking-code product and the code pool is empty (Task 2b)."""
    w.db.session.add(
        OrderItem(order_id=order_id, product_id=w.water.id, quantity=2, unit_price=15000, total_price=30000)
    )
    w.db.session.add(
        ProductFiscalProfile(
            product_id=w.water.id, fiscalization_enabled=True, requires_marking_codes=True, spic="SPIC-10A"
        )
    )
    w.db.session.commit()


def _pay(name, channel, status, **kw):
    return Exchange(name, "retry-payment", {"channel": channel, "expected_amount": "30000.00"}, status, **kw)


def _cancel_effect(w: _World, order_id: int, response: dict) -> None:
    """A plain cancel ends the order and writes no skip."""
    assert _status(w, order_id) is OrderStatus.CANCELLED
    assert _skip_logs(w) == []


def _skip_effect(reason_code, reason_text=None):
    def effect(w: _World, order_id: int, response: dict) -> None:
        assert _status(w, order_id) is OrderStatus.CANCELLED
        [log] = _skip_logs(w)
        extra = log.extra_data
        assert (extra["skipped_by"], extra["reason_code"], extra["reason_text"]) == (
            "customer",
            reason_code,
            reason_text,
        )
        assert extra["cancelled_order_ids"] == [order_id]

    return effect


def _pay_effect(w: _World, order_id: int, response: dict) -> None:
    assert response["data"]["payment_url"] == CLICK_URL


def _moved_to_click(w: _World, order_id: int, response: dict) -> None:
    _pay_effect(w, order_id, response)
    order = _order(w, order_id)
    assert (order.payment_method, order.payment.payment_method) == (PaymentMethod.CLICK, PaymentMethod.CLICK)


def _declined(w: _World, order_id: int, response: dict) -> None:
    assert _status(w, order_id) is OrderStatus.CANCELLED
    row = OrderConfirmationRequest.query.filter_by(order_id=order_id).one()
    assert (row.status, row.response_channel) == ("declined", "customer_cancel")
    assert [args[1] for args, _kwargs in w.pushes] == [SALES_EVENT_AGENT_ORDER_DECLINED]


def _confirmed_on_web(w: _World, order_id: int, response: dict) -> None:
    assert _status(w, order_id) is OrderStatus.CONFIRMED
    row = OrderConfirmationRequest.query.filter_by(order_id=order_id).one()
    assert (row.status, row.response_channel) == ("confirmed", "web")


_S, _M, _P = OrderStatus, PaymentMethod, PaymentStatus

CASES = [
    Case(
        "click_unpaid",
        _row_case(ROW["click_pending"]),
        (True, "cancel", False),
        (
            _pay("pay_web", "web", 200, effect=_pay_effect),
            _pay("pay_bot", "bot", 200, via="bot", effect=_pay_effect),
            Exchange("cancel", "cancel", {"expected_via": "cancel"}, 200, effect=_cancel_effect),
        ),
    ),
    Case(
        "click_unpaid_stale",
        _row_case(ROW["click_pending"]),
        (True, "cancel", False),
        (_pay("pay_web", "web", 409), _pay("pay_bot", "bot", 409, via="bot")),
        after_read=_total_moves,
        reads_after=True,
    ),
    Case(
        "click_past_deadline",
        _row_case(ROW["click_past_deadline"]),
        (False, "cancel", False),
        (_pay("pay_web", "web", 409), _pay("pay_bot", "bot", 409, via="bot")),
    ),
    Case(
        "cash_pending",
        _row_case(ROW["cash_pending"]),
        (False, "cancel", False),
        (
            _pay("pay_bot", "bot", 409, via="bot"),
            Exchange("cancel", "cancel", {"expected_via": "cancel"}, 200, effect=_cancel_effect),
        ),
    ),
    Case("partly_paid", _row_case(ROW["click_partly_paid"]), (False, None, False)),
    Case(
        "delivered_unpaid_click",
        _row_case(Row("delivered_unpaid_click", _S.DELIVERED, _M.CLICK, _P.PENDING, has_deadline=False)),
        (True, None, False),
        (_pay("pay_web", "web", 200, effect=_pay_effect),),
    ),
    Case(
        "payme_unpaid",
        _row_case(Row("payme_unpaid", _S.PENDING, _M.PAYME, _P.PENDING, cancel_via="cancel")),
        (True, "cancel", False),
        (
            _pay("pay_bot", "bot", 200, via="bot", effect=_moved_to_click),
            Exchange(
                "payme_pool_short",
                "retry-payment",
                {"channel": "web", "expected_amount": "30000.00"},
                400,
                prepare=_pool_runs_short,
            ),
            Exchange(
                "payme_pool_short_bot",
                "retry-payment",
                {"channel": "bot", "expected_amount": "30000.00"},
                400,
                via="bot",
                prepare=_pool_runs_short,
            ),
        ),
    ),
    Case(
        "paid_delivered",
        _row_case(Row("paid_delivered", _S.DELIVERED, _M.CLICK, _P.COMPLETED, is_paid=True, pay_refusal="already_paid")),
        (False, None, False),
    ),
    Case("cancelled", _row_case(ROW["click_cancelled"]), (False, None, False)),
    Case(
        "subscription_later_order",
        _daily_later_order,
        (True, "skip", False),
        (
            Exchange(
                "skip_reason",
                "cancel",
                {"expected_via": "skip", "reason_code": "away"},
                200,
                effect=_skip_effect("away"),
            ),
            Exchange(
                "skip_other_text",
                "cancel",
                {"expected_via": "skip", "reason_code": "other", "reason_text": "Going to the dacha"},
                200,
                effect=_skip_effect("other", "Going to the dacha"),
            ),
            Exchange(
                "skip_still_have_water",
                "cancel",
                {"expected_via": "skip", "reason_code": "still_have_water"},
                200,
                effect=_skip_effect("still_have_water"),
            ),
            Exchange("skip_no_reason", "cancel", {"expected_via": "skip"}, 200, effect=_skip_effect(None)),
        ),
        sub_after="skip_still_have_water",
    ),
    Case(
        "subscription_leftover_paused",
        _leftover_order,
        (True, "cancel", False),
        (Exchange("cancel", "cancel", {"expected_via": "cancel"}, 200, effect=_cancel_effect),),
        sub_after="cancel",
    ),
    Case(
        "stale_via",
        _needs_payment_order,
        (True, "skip", False),
        (Exchange("skip", "cancel", {"expected_via": "skip"}, 409),),
        after_read=_subscription_pauses,
        reads_after=True,
        sub_after="skip",
    ),
    Case(
        "agent_pending",
        _agent_order,
        (False, "decline", True),
        (
            Exchange("decline", "cancel", {"expected_via": "decline"}, 200, effect=_declined),
            Exchange(
                "confirm_web",
                "agent-confirmation",
                {"action": "confirm", "channel": "web"},
                200,
                effect=_confirmed_on_web,
            ),
        ),
    ),
    Case("click_unpaid_ru", _row_case(ROW["click_pending"]), (True, "cancel", False), language="ru"),
]
CASE = {case.name: case for case in CASES}
assert len(CASE) == len(CASES), "case names must be unique"

# One test per (case, exchange); a case without a write has one test of its reads.
PARAMS = [(case, exchange) for case in CASES for exchange in (case.exchanges or (None,))]
IDS = [f"{case.name}-{exchange.name if exchange else 'reads'}" for case, exchange in PARAMS]


# --------------------------------------------------------------------------- reading and writing the world


def _order(w: _World, order_id: int) -> Order:
    w.db.session.expire_all()
    return w.db.session.get(Order, order_id)


def _status(w: _World, order_id: int) -> OrderStatus:
    return _order(w, order_id).status


def _skip_logs(w: _World):
    w.db.session.expire_all()
    return SubscriptionLog.query.filter_by(action="delivery_skipped").all()


def _snapshot(w: _World) -> dict:
    """Everything a refused write must leave as it was."""
    w.db.session.expire_all()
    return {
        "orders": [
            (o.id, str(o.status), str(o.total_amount), str(o.payment_method), o.is_paid)
            for o in Order.query.order_by(Order.id)
        ],
        "payments": [
            (p.id, str(p.status), str(p.amount), str(p.payment_method), str(p.amount_collected), p.callback_url)
            for p in Payment.query.order_by(Payment.id)
        ],
        "subscriptions": [(s.id, str(s.status), s.next_delivery_date) for s in Subscription.query.order_by(Subscription.id)],
        "logs": [(log.id, log.action) for log in SubscriptionLog.query.order_by(SubscriptionLog.id)],
        "questions": [(q.id, q.status) for q in OrderConfirmationRequest.query.order_by(OrderConfirmationRequest.id)],
    }


class _Clients:
    """The same customer through the two doors: the website (cookie JWT + CSRF, as base.js `apiRequest` sends it)
    and the bot (bearer token). Each has its own client, so the cookies never reach the bot's requests."""

    def __init__(self, w: _World, user):
        self.app, self.user = w.app, user
        self.web = _Web(w.app, w.app.test_client(), user)
        self.bot_client = w.app.test_client()
        with w.app.app_context():
            token = create_access_token(identity=str(user.id))
        self.bot_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def visit(self, language: str) -> None:
        """The page visit fixes the session's language, which the page's API calls then answer in."""
        resp = self.web._client.get(f"/my-orders?lang={language}", follow_redirects=True)
        assert resp.status_code == 200

    def web_get(self, path: str):
        return self.web._client.get(path, headers={"X-Platform": "web"}, follow_redirects=True)

    def bot_get(self, path: str):
        return self.bot_client.get(path, headers=self.bot_headers, follow_redirects=True)

    def write(self, via: str, path: str, body: dict):
        if via == "web":
            return self.web.post(path, body)
        return self.bot_client.post(path, headers=self.bot_headers, data=json.dumps(body))


def _tidy(w: _World, value):
    """A recorded answer without what only the wall clock stamps, with order numbers named by order id."""
    w.db.session.expire_all()
    names = {order.order_number: f"TG_{order.id:06d}_26" for order in Order.query.all()}
    tidy = _rename(_blank_timestamps(_blank_stamps(value)), names)
    # A delivery's tracking number carries the wall clock and a random suffix.
    return json.loads(re.sub(r"TRK\d{12}[0-9A-F]{6}", "TRK<stamp>", json.dumps(tidy)))


def _answer(w: _World, resp) -> dict:
    return _tidy(w, {"status": resp.status_code, "body": resp.get_json()})


def _read(w: _World, clients: _Clients, order_id: int, language: str) -> Dict[str, dict]:
    """The reads the order surfaces send, through both doors (which must answer the same)."""
    clients.visit(language)
    requests = {}
    for key in (detail_key(order_id), LIST_KEY, BOT_LIST_KEY):
        path = key[len("GET ") :]
        web, bot = clients.web_get(path), clients.bot_get(path)
        assert (web.status_code, bot.status_code) == (200, 200), (key, web.get_json(), bot.get_json())
        assert web.get_json() == bot.get_json(), f"{key}: the bot and the website read different answers"
        requests[key] = _answer(w, web)
    published = requests[detail_key(order_id)]["body"]["data"]["order"]
    assert "error" not in published, f"the serializer's partial fallback answered: {published}"
    tracking = clients.web_get(f"/order-tracking?order_id={order_id}")
    assert tracking.status_code == 200
    island = json.loads(BeautifulSoup(tracking.get_data(as_text=True), "html.parser").select_one("script#page-data").string)
    assert island["order"] == {"id": order_id, "customer_actions": published["customer_actions"]}, (
        "the tracking page's island must carry the API's answer"
    )
    requests["tracking_island"] = island["order"]
    assert requests[BOT_LIST_KEY] == requests[LIST_KEY], "the bot's spelling of the list reads another answer"
    listed = next(o for o in requests[LIST_KEY]["body"]["data"]["orders"] if o["id"] == order_id)
    assert listed["customer_actions"] == published["customer_actions"], "the list and the detail disagree"
    return requests


def _reads_after(w: _World, clients: _Clients, order_id: int) -> Dict[str, dict]:
    """The order and the list again, once the screen went stale: the fresh answers a reload is shown."""
    requests = {}
    for key in (detail_key(order_id), LIST_KEY):
        path = key[len("GET ") :]
        web, bot = clients.web_get(path), clients.bot_get(path)
        assert (web.status_code, bot.status_code) == (200, 200), (key, web.get_json(), bot.get_json())
        assert web.get_json() == bot.get_json(), f"{key}: the bot and the website read different answers"
        requests[key] = _answer(w, web)
    return requests


def _subscription_reads(w: _World, clients: _Clients) -> Dict[str, dict]:
    requests = {}
    for path in (f"{API}/subscriptions/1", f"{API}/subscriptions/1/items"):
        web, bot = clients.web_get(path), clients.bot_get(path)
        assert (web.status_code, bot.status_code) == (200, 200), (path, web.get_json())
        assert web.get_json() == bot.get_json(), path
        requests[f"GET {path}"] = _answer(w, web)
    return requests


def _save(case: Case, order_id: int, requests: dict, exchanges: dict, requests_after: Optional[dict] = None) -> None:
    """Write this test's slice of the case's file (when updating), then compare it with what the file holds."""
    path = FIXTURES / f"{case.name}.json"
    if UPDATE:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        saved = json.loads(path.read_text("utf-8")) if path.exists() else {"exchanges": {}, "requests": {}}
        saved.update({"case": case.name, "language": case.language, "order_id": order_id})
        saved["requests"].update(requests)
        if requests_after:
            saved["requests_after"] = requests_after
        saved["exchanges"].update(exchanges)
        path.write_text(json.dumps(saved, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert path.exists(), f"{path} is missing: run with UPDATE_CUSTOMER_ORDER_ACTIONS_FIXTURES=1"
    saved = json.loads(path.read_text("utf-8"))
    hint = f"{path.name} is stale: regenerate it with UPDATE_CUSTOMER_ORDER_ACTIONS_FIXTURES=1 and re-run the suites"
    assert (saved["case"], saved["language"], saved["order_id"]) == (case.name, case.language, order_id), hint
    for key, answer in requests.items():
        assert saved["requests"][key] == json.loads(json.dumps(answer)), f"{hint} ({key})"
    if requests_after:
        assert saved["requests_after"] == json.loads(json.dumps(requests_after)), f"{hint} (requests_after)"
    for name, exchange in exchanges.items():
        assert saved["exchanges"][name] == json.loads(json.dumps(exchange)), f"{hint} (exchange {name})"


@pytest.mark.parametrize("case, exchange", PARAMS, ids=IDS)
def test_the_case_fixture_is_what_the_endpoints_answer(world, request, monkeypatch, case, exchange):
    pushes = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    world.pushes = pushes
    built = case.build(world, request)
    _speaks(world.db, built.user, case.language)
    clients = _Clients(world, built.user)

    requests = _read(world, clients, built.order_id, case.language)
    published = requests[detail_key(built.order_id)]["body"]["data"]["order"]["customer_actions"]
    assert (published["pay"]["allowed"], published["cancel"]["via"], published["confirm"]["allowed"]) == case.expected

    exchanges = {}
    requests_after = None
    if exchange is not None:
        if case.after_read is not None:
            case.after_read(world, built.order_id)
            if case.reads_after:
                requests_after = _reads_after(world, clients, built.order_id)
        if exchange.prepare is not None:
            exchange.prepare(world, built.order_id)
        before = _snapshot(world)
        path = f"{API}/orders/{built.order_id}/{exchange.path}"
        resp = clients.write(exchange.via, path, exchange.body)
        assert resp.status_code == exchange.status, resp.get_json()
        if exchange.status == 200:
            assert exchange.effect is not None, f"{exchange.name} has no asserted effect"
            exchange.effect(world, built.order_id, resp.get_json())
        else:
            assert _snapshot(world) == before, f"{exchange.name}: a refused write changed the database"
        exchanges[exchange.name] = {
            "method": "POST",
            "path": path,
            "body": exchange.body,
            "status": resp.status_code,
            "response": _answer(world, resp)["body"],
        }
        if case.sub_after == exchange.name:
            requests.update(_subscription_reads(world, clients))
    _save(case, built.order_id, requests, exchanges, requests_after)


def test_every_case_file_holds_exactly_its_declared_exchanges():
    # The payment return pages' fixtures (Task 10b) are written, and kept fresh, by test_web_payment_return_pages.py.
    other_owners = ("payment_", "checkout_", "rescue_")
    mine = [path.stem for path in FIXTURES.glob("*.json") if path.stem != "shell" and not path.stem.startswith(other_owners)]
    assert sorted(mine) == sorted(CASE)
    for case in CASES:
        saved = json.loads((FIXTURES / f"{case.name}.json").read_text("utf-8"))
        assert sorted(saved["exchanges"]) == sorted(exchange.name for exchange in case.exchanges), case.name
        assert ("requests_after" in saved) == case.reads_after, case.name
        assert (LIST_KEY in saved["requests"]) and (BOT_LIST_KEY in saved["requests"]) and ("tracking_island" in saved["requests"]), case.name
        assert ("GET /api/v1/subscriptions/1" in saved["requests"]) == (case.sub_after is not None), case.name


# --------------------------------------------------------------------------- what the answers mean


def _saved(case: str) -> dict:
    return json.loads((FIXTURES / f"{case}.json").read_text("utf-8"))


def test_a_stale_pay_and_a_late_pay_are_refused_in_their_own_shapes():
    stale = _saved("click_unpaid_stale")["exchanges"]["pay_web"]
    assert (stale["status"], stale["response"]["data"]) == (409, {"reason_code": "STATE_CHANGED"})
    assert stale["response"]["message"] == "This changed. Refresh to see the latest."
    late = _saved("click_past_deadline")["exchanges"]["pay_bot"]
    assert (late["status"], late["response"]["data"]) == (
        409,
        {"reason_code": "PAY_REFUSED", "refusal": "deadline_passed"},
    )
    assert late["response"]["message"] == "The time to pay for this order online has ended."


def test_the_pool_short_refusal_is_a_400_with_its_sentence():
    short = _saved("payme_unpaid")["exchanges"]["payme_pool_short"]
    assert short["status"] == 400
    assert short["response"]["data"] == {"error_code": "MARKING_CODES_POOL_SHORT"}
    assert short["response"]["message"] and "api.payments" not in short["response"]["message"]


# --------------------------------------------------------------------------- the agent's answer, in words


def test_a_second_answer_is_refused_in_the_customers_language(world, request, monkeypatch):
    _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    built = _agent_order(world, request)
    _speaks(world.db, built.user, "ru")
    clients = _Clients(world, built.user)
    clients.visit("ru")
    path = f"{API}/orders/{built.order_id}/agent-confirmation"

    first = clients.write("web", path, {"action": "confirm", "channel": "web"})
    second = clients.write("web", path, {"action": "confirm", "channel": "web"})

    assert first.status_code == 200, first.get_json()
    assert second.status_code == 409
    assert second.get_json()["message"] == BACKEND_TRANSLATIONS["api.orders.agent_confirmation.not_pending"]["ru"]
    assert second.get_json()["error_code"] == "SALES_CONFIRMATION_NOT_PENDING"


def test_the_bot_body_still_records_telegram(world, request, monkeypatch):
    """The bot's body has no `channel`: the answer is recorded as Telegram, as before."""
    _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    built = _agent_order(world, request)
    clients = _Clients(world, built.user)

    resp = clients.write("bot", f"{API}/orders/{built.order_id}/agent-confirmation", {"action": "confirm"})

    assert resp.status_code == 200, resp.get_json()
    row = OrderConfirmationRequest.query.filter_by(order_id=built.order_id).one()
    assert (row.status, row.response_channel) == ("confirmed", "telegram")


# --------------------------------------------------------------------------- the shell


def _render(w: _World, user, path: str, language: str) -> BeautifulSoup:
    _speaks(w.db, user, language)
    client = w.app.test_client()
    _Web(w.app, client, user)
    w.app.jinja_env.cache.clear()
    resp = client.get(f"{path}{'&' if '?' in path else '?'}lang={language}", follow_redirects=True)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
    return BeautifulSoup(resp.get_data(as_text=True), "html.parser")


def _island(soup: BeautifulSoup, element_id: str = "page-data") -> dict:
    return json.loads(soup.select_one(f"script#{element_id}").string)


def _shell(w: _World) -> dict:
    order = seed(w.db, w.user, ROW["click_pending"], now=w.now.astimezone(UTC))
    shell = {}
    for language in LANGUAGES:
        orders = _render(w, w.user, "/my-orders", language)
        tracking = _render(w, w.user, f"/order-tracking?order_id={order.id}", language)
        shell[language] = {
            "orders": {
                "html": "".join(
                    str(orders.select_one(f"#{el}"))
                    for el in ("loadingOrders", "ordersList", "emptyState", "paginationSection")
                ),
                "island": _island(orders),
            },
            "modal": {
                "html": str(orders.select_one("#orderDetailModal")),
                "island": _island(orders, "order-detail-modal-data"),
            },
            "tracking": {"html": str(tracking.select_one("div.action-buttons")), "island": _island(tracking)},
        }
    return shell


def test_the_shell_fixture_is_what_the_pages_render(world):
    shell = _shell(world)
    path = FIXTURES / "shell.json"
    if UPDATE:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(shell, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert path.exists(), f"{path} is missing: run with UPDATE_CUSTOMER_ORDER_ACTIONS_FIXTURES=1"
    assert json.loads(path.read_text("utf-8")) == json.loads(json.dumps(shell)), (
        "shell.json is stale: regenerate it with UPDATE_CUSTOMER_ORDER_ACTIONS_FIXTURES=1"
    )
    for language in LANGUAGES:
        words = {key: BACKEND_TRANSLATIONS[f"web.order_actions.{key}"][language] for key in ORDER_ACTIONS_WORDS}
        assert shell[language]["orders"]["island"]["order_actions"] == words
        assert shell[language]["modal"]["island"]["order_actions"] == words
        assert shell[language]["tracking"]["island"]["order_actions"] == words
        # The containers the one script draws into.
        assert 'id="orderDetailActions"' in shell[language]["modal"]["html"]
        assert 'id="orderActions"' in shell[language]["tracking"]["html"]
        assert shell[language]["orders"]["island"]["subscriptions_url"] == "/my-subscriptions"


def test_every_order_actions_word_is_seeded_trilingual():
    keys = {key for key in BACKEND_TRANSLATIONS if key.startswith("web.order_actions.")}
    assert keys == {f"web.order_actions.{word}" for word in ORDER_ACTIONS_WORDS}
    for key in sorted(keys | {"api.orders.agent_confirmation.not_pending"}):
        row = BACKEND_TRANSLATIONS[key]
        assert all(row.get(language) for language in LANGUAGES), key
        placeholders = {language: set(re.findall(r"\{(\w+)\}", row[language])) for language in LANGUAGES}
        assert placeholders["uz"] == placeholders["en"] == placeholders["ru"], (key, placeholders)
    assert set(order_actions_words()) == set(ORDER_ACTIONS_WORDS)
