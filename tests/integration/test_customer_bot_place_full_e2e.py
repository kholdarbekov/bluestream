"""Customer bot ``/bottles`` across EVERY place shape — full end-to-end.

WHAT MAKES THIS FILE DIFFERENT FROM THE EXISTING BOT TESTS
----------------------------------------------------------
``tests/unit/test_customer_bot_bottles_place.py`` and
``tests/telegram_bot/test_bottle_history.py`` feed a FABRICATED overview dict to
the formatters. That is fast and precise, but it is also exactly how the
(user, address) -> PLACE re-key shipped with every bot test green while the live
screen rendered 0 for every customer: a fabricated payload cannot notice a
backend rename.

Every screen in THIS file is rendered from the REAL payload:

    real service write paths  ->  real HTTP route (real JWT)
                              ->  the real ``BottleBalanceHandler``
                              ->  the rendered Telegram body + inline keyboard

Nothing here hand-builds a ``BottleBalance`` row, and nothing here hand-writes an
overview dict except where the SUBJECT of the test is a malformed/legacy payload
shape the handler must survive (clearly marked, and always derived by DELETING a
key from a real row — never by inventing one).

CONVENTIONS
-----------
* ``i18n`` is stubbed in one of two modes:
  - KEY-ECHO (``_key_echo``): returns ``"<key> <kwarg values>"`` so an assertion
    can see both the key that was requested AND the interpolated value. The real
    ``telegram_bot/i18n.py`` returns a humanised last segment on a miss and then
    silently DROPS every kwarg, which would hide balances from assertions in an
    unseeded test process.
  - REAL SEEDED TEMPLATES (``_SeededI18n``): the actual strings from the three
    seed scripts that own these keys, with ``telegram_bot/i18n.py``'s exact
    lookup + format-swallow semantics reproduced faithfully — including the
    miss path, so the "an unseeded key deletes the number" failure mode can be
    pinned rather than described.
* Fixture text is deliberately DIGIT-FREE wherever a test counts digits, so a
  stray "7" in the body can only have come from a balance.
* Where conservation is involved the assertion is a PAIR (before/after, or
  across both affected customers' screens) — never one side.
"""

import asyncio
import importlib.util
import pathlib
import re
import sys
from datetime import datetime, UTC
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import text as db_text

# telegram_bot modules use workdir-relative BARE imports
# (`from api_client import api_client`, `from i18n import i18n`, ...), so they
# are NOT importable as `telegram_bot.handlers.bottles`. Same bootstrap as
# tests/unit/test_customer_bot_bottles_place.py:30-33.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "telegram_bot") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

from telegram.error import BadRequest  # noqa: E402

from api_client import APIResponse  # noqa: E402
import handlers.bottles as bottles_mod  # noqa: E402

from business_app.models.bottle import BottleBalance, BottleLedger  # noqa: E402
from business_app.models.order import Order  # noqa: E402
from business_app.models.user import User, UserAddress  # noqa: E402
from business_app.services.bottle_tracking_service import BottleTrackingService  # noqa: E402
from business_app.services.customer_link_service import CustomerLinkService  # noqa: E402
from business_app.utils.exceptions import ValidationError  # noqa: E402
from business_app.utils.password_security import hash_password  # noqa: E402
from shared.enums import OrderStatus, UserRole, UserStatus, UserType  # noqa: E402
from tests.telegram_bot.helpers import (  # noqa: E402
    DummyCallbackQuery,
    DummyUpdate,
    make_context,
    overview_balance_row,
    overview_payload,
    overview_place_member,
)

LAT, LNG = 41.3111, 69.2797
BALANCES_URL = "/api/v1/orders/bottles/my-balances"
LEDGER_URL = "/api/v1/orders/bottles/my-ledger/{address_id}"


# --------------------------------------------------------------------------- #
# World builder — every row is created through a real write path
# --------------------------------------------------------------------------- #
class _World:
    """Builds users / addresses / orders and moves bottles ONLY through the
    real service write paths (`record_bottles_delivered`,
    `record_bottles_returned`, `admin_adjust_balance`, `set_initial_balance`,
    `create_place_group`, `remove_address_from_group`, ...).

    A hand-built `BottleBalance` row would make every balance assertion in this
    file vacuous: it is the materialised figure the whole re-key is about.
    """

    def __init__(self, db):
        self.db = db
        self.svc = BottleTrackingService()
        self.link = CustomerLinkService()
        self._n = 0
        self.admin = self.user("Ada", "Admin", role=UserRole.ADMIN, user_type=UserType.STAFF)

    # -- entities ---------------------------------------------------------
    def user(self, first_name, last_name="Member", *, role=UserRole.CUSTOMER,
             user_type=UserType.INDIVIDUAL):
        self._n += 1
        u = User(
            email=f"e2e{self._n}@example.com",
            phone=f"+9989000{self._n:05d}",
            password_hash=hash_password("TestPassword123!"),
            first_name=first_name,
            last_name=last_name,
            user_type=user_type,
            role=role,
            status=UserStatus.ACTIVE,
            is_verified=True,
            created_at=datetime.now(UTC),
        )
        self.db.session.add(u)
        self.db.session.commit()
        return u

    def address(self, user, title="Home", full_address="Baker Street", address_id=None):
        """`address_id` forces the PK, so a test can insert a HIGHER id before a
        lower one and make physical row order disagree with id order — the only
        way to see whether a reader has an explicit ORDER BY."""
        a = UserAddress(
            user_id=user.id, title=title, full_address=full_address,
            city="Tashkent", latitude=LAT, longitude=LNG,
        )
        if address_id is not None:
            a.id = address_id
        self.db.session.add(a)
        self.db.session.commit()
        return a

    def link_accounts(self, primary, *others):
        for other in others:
            self.link.link_accounts(
                primary_user_id=primary.id, secondary_user_id=other.id,
                actor_admin_id=self.admin.id, reason="e2e cluster",
            )
        self.db.session.commit()

    def group(self, *addresses, label="office", **review):
        g = self.link.create_place_group(
            [a.id for a in addresses], acting_admin_id=self.admin.id,
            reason="e2e place", label=label, **review,
        )
        self.db.session.commit()
        return g

    def order(self, user, address=None):
        o = Order(
            user_id=user.id, status=OrderStatus.DELIVERED,
            total_amount=Decimal("50000.00"),
            delivery_address_id=address.id if address is not None else None,
        )
        self.db.session.add(o)
        self.db.session.commit()
        return o

    # -- bottle movements -------------------------------------------------
    def deliver(self, user, address, qty, order=None):
        """A real order delivery: +qty at the PLACE this address belongs to."""
        order = order or self.order(user, address)
        entry = self.svc.record_bottles_delivered(
            order_id=order.id, user_id=user.id, address_id=address.id,
            quantity=Decimal(str(qty)), actor_user_id=self.admin.id,
        )
        self.db.session.commit()
        return entry

    def collect(self, user, address, qty, order=None):
        """A real return-on-delivery: -qty at the PLACE."""
        entry = self.svc.record_bottles_returned(
            user_id=user.id, address_id=address.id, quantity=Decimal(str(qty)),
            order_id=order.id if order is not None else None,
            actor_user_id=self.admin.id,
        )
        self.db.session.commit()
        return entry

    def adjust(self, user, address, delta, notes="e2e adjust"):
        entry = self.svc.admin_adjust_balance(
            user_id=user.id if user is not None else None, address_id=address.id,
            adjustment=Decimal(str(delta)), actor_user_id=self.admin.id, notes=notes,
        )
        self.db.session.commit()
        return entry

    def seed(self, user, address, qty):
        entry = self.svc.set_initial_balance(
            user_id=user.id, address_id=address.id, quantity=Decimal(str(qty)),
            actor_user_id=self.admin.id,
        )
        self.db.session.commit()
        return entry

    def drift_shape(self, user, address, stored):
        """Reproduce dev-DB address 24: a stored figure with ZERO ledger rows.

        Production got here by manual pre-grouping adjustments on data that
        predates the ledger. The `bottle_balances` ROW is still created by the
        real write path; only its explaining entries are removed, exactly as
        that history has none. No hand-built balance row.
        """
        self.adjust(user, address, stored, notes="pre-ledger figure")
        scope = BottleTrackingService.resolve_scope(address.id)
        # ORM-level deletes, not a bulk `.delete(synchronize_session=False)`:
        # the bulk form leaves the removed rows in the identity map as
        # persistent-clean, and a later unit-of-work pass over the same session
        # can resurrect the "pre-ledger" entry this helper exists to remove —
        # which silently turns a DRIFTED place back into an explained one and
        # makes every drift assertion downstream pass for the wrong reason.
        for entry in BottleLedger.query.filter(*scope.ledger_filter()).all():
            self.db.session.delete(entry)
        self.db.session.commit()
        self.db.session.expire_all()
        assert BottleTrackingService.get_place_balance(address.id) == Decimal(str(stored))
        assert ledger_sum(address.id) == Decimal("0.00")
        assert BottleLedger.query.filter(*scope.ledger_filter()).count() == 0


@pytest.fixture
def world(db):
    return _World(db)


def ledger_sum(address_id) -> Decimal:
    """SUM(bottle_ledger.quantity) over the address's PLACE scope.

    Deliberately NOT what the customer screen shows — the screen shows the
    STORED balance. The two legitimately disagree on production data; several
    tests below pin exactly that.
    """
    scope = BottleTrackingService.resolve_scope(address_id)
    return sum(
        (e.quantity for e in BottleLedger.query.filter(*scope.ledger_filter()).all()),
        Decimal("0.00"),
    )


def i18n_keys_in(text: str) -> set:
    """Every translation key the KEY-ECHO stub echoed into a rendered body.

    Lets a test assert the EXACT key set a screen requested. A forbidden-WORD
    check ("over-returned", "credit", ...) is vacuous under key echo — new copy
    arrives as a new KEY, not as English prose — so the strong form of "the bot
    added no editorial about this number" is "the key set did not change".
    """
    return set(re.findall(r"telegram\.[A-Za-z0-9_.]*[A-Za-z0-9_]", text))


def all_bottles() -> Decimal:
    """Every bottle the system materialises, across EVERY scope.

    Conservation is asserted against this, never against one place: a bug that
    landed one place on the right number while zeroing another satisfies a
    one-sided assertion.
    """
    return sum((b.balance for b in BottleBalance.query.all()), Decimal("0.00"))


def headers(app, user):
    with app.app_context():
        return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


# --------------------------------------------------------------------------- #
# i18n stubs
# --------------------------------------------------------------------------- #
def _key_echo(key, language=None, *args, **kwargs):
    """Echo the key plus every interpolated value."""
    return " ".join([key] + [str(v) for v in kwargs.values()])


def _load_seed(name):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED_TABLES_CACHE = {}


def seed_tables():
    """The REAL seeded templates for every key the /bottles screen requests.

    Union of the THREE seed sources that own them — that split is itself the
    hazard: no single script guarantees the handler's key set is covered, and a
    miss deletes the number rather than showing the key
    (telegram_bot/i18n.py:80-93).
    """
    if not _SEED_TABLES_CACHE:
        merged = {}
        merged.update(_load_seed("seed_backend_translations").BACKEND_TRANSLATIONS)
        merged.update(_load_seed("seed_bottle_ledger_translations").KEYS)
        merged.update(_load_seed("seed_place_group_telegram_translations").KEYS)
        _SEED_TABLES_CACHE["tables"] = merged
    return _SEED_TABLES_CACHE["tables"]


class _SeededI18n:
    """`telegram_bot/i18n.py`'s `get()` semantics, backed by the real seeds.

    Reproduced faithfully on purpose, including the two failure modes:
      * a MISS humanises the last key segment and then drops every kwarg;
      * a `KeyError`/`ValueError` from `.format()` is SWALLOWED and the RAW
        template is returned to the customer.
    """

    def __init__(self, *, fallback="en", drop_keys=()):
        self.tables = seed_tables()
        self.fallback = fallback
        self.drop = set(drop_keys)
        self.missing = []

    def get(self, key, language=None, *args, **kwargs):
        language = language or self.fallback
        entry = None if key in self.drop else self.tables.get(key)
        if entry and language in entry:
            translation = entry[language]
        elif entry and self.fallback in entry:
            translation = entry[self.fallback]
        else:
            self.missing.append(key)
            last = key.rsplit(".", 1)[-1] if "." in key else key
            translation = last.replace("_", " ").capitalize()
        if args or kwargs:
            try:
                translation = translation.format(*args, **kwargs)
            except (KeyError, ValueError):
                pass
        return translation


# --------------------------------------------------------------------------- #
# Handler driver
# --------------------------------------------------------------------------- #
class _FakeBottleClient:
    """`async with api_client as client` stand-in serving ONE prepared response.

    `get_my_bottle_balances` records whether it was called so the auth-guard
    test can prove the endpoint is never reached without a token.
    """

    def __init__(self, balances=None):
        self._balances = balances
        self.balance_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get_my_bottle_balances(self, _token):
        self.balance_calls += 1
        return self._balances


class Screen:
    """One rendered /bottles screen."""

    def __init__(self, update, fake_client, overview, i18n_stub=None):
        self.update = update
        self.query = update.callback_query
        self.client = fake_client
        self.overview = overview
        self.i18n = i18n_stub

    @property
    def edited(self):
        return self.query is not None and self.query.edit_message_text.call_args is not None

    @property
    def kwargs(self):
        if self.query is not None:
            assert self.query.edit_message_text.call_args is not None, "screen was never rendered"
            return self.query.edit_message_text.call_args.kwargs
        call = self.update.message.reply_text.call_args
        assert call is not None, "screen was never rendered"
        merged = dict(call.kwargs)
        if call.args:
            merged["text"] = call.args[0]
        return merged

    @property
    def text(self):
        return self.kwargs["text"]

    @property
    def lines(self):
        return self.text.splitlines()

    @property
    def rows(self):
        return [line for line in self.lines if line.startswith("• ")]

    @property
    def buttons(self):
        markup = self.kwargs["reply_markup"]
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]

    @property
    def callbacks(self):
        return [cb for _, cb in self.buttons]

    @property
    def answers(self):
        return [c.args[0] for c in self.query.answer.call_args_list if c.args]


def _patch_bot(monkeypatch, *, language="en", i18n_get=None):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(bottles_mod, "user_middleware", AsyncMock(return_value={"id": 1001}))
    monkeypatch.setattr(bottles_mod, "get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(bottles_mod.i18n, "get_user_language", AsyncMock(return_value=language))
    monkeypatch.setattr(bottles_mod.i18n, "get", i18n_get or _key_echo)


def render(monkeypatch, response, *, language="en", i18n_get=None, i18n_stub=None,
           as_message=False, edit_error=None, token="tok"):
    """Drive the REAL handler over a prepared API response."""
    from unittest.mock import AsyncMock

    _patch_bot(monkeypatch, language=language, i18n_get=i18n_get)
    if token is None:
        monkeypatch.setattr(bottles_mod, "get_auth_token", AsyncMock(return_value=None))

    fake = _FakeBottleClient(balances=response)
    monkeypatch.setattr(bottles_mod, "api_client", fake)

    update = DummyUpdate()
    if not as_message:
        update.callback_query = DummyCallbackQuery(data="my_bottles")
        if edit_error is not None:
            update.callback_query.edit_message_text.side_effect = edit_error

    handler = bottles_mod.BottleBalanceHandler()
    asyncio.run(handler.show_bottle_balance(update, make_context()))
    overview = None
    if isinstance(response, APIResponse) and isinstance(response.data, dict):
        overview = response.data.get("data")
    return Screen(update, fake, overview, i18n_stub=i18n_stub)


def fetch(client, app, user):
    """The REAL HTTP payload: `{'success': True, 'data': {...overview...}}`."""
    resp = client.get(BALANCES_URL, headers=headers(app, user))
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def create_address_via_api(client, app, user, **payload):
    """Create an address through the REAL customer route.

    `POST /api/v1/addresses/` accepts a pin-drop with NO `full_address` and NO
    `title` (api/addresses.py:88-90 requires "full_address OR lat/lng", and
    line 116 then stores `data.get("full_address", "")`). That is the production
    path to a title-less, address-less row — the one `_address_label` renders as
    the untranslated `Address #{id}`. Building that row by hand would leave the
    defect looking like a test artefact.
    """
    resp = client.post("/api/v1/addresses/", json=payload, headers=headers(app, user))
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()["data"]["address"]
    row = UserAddress.query.get(body["id"])
    assert row is not None
    return row


def screen_for(monkeypatch, client, app, user, **kw):
    """Real route -> real handler. The end-to-end path of this whole file."""
    return render(monkeypatch, APIResponse(success=True, data=fetch(client, app, user)), **kw)


# =========================================================================== #
# 1. Solo, ungrouped — the pre-link regression baseline
# =========================================================================== #
@pytest.mark.integration
def test_solo_ungrouped_customer_renders_one_plain_row_and_no_place_furniture(
    monkeypatch, client, app, db, world
):
    """100% of today's customers. Any re-key of the payload (the exact failure
    Plan A shipped: readers kept reading `balance`) renders 0 or drops the row,
    while every fabricated-payload test stays green."""
    solo = world.user("Solo")
    home = world.address(solo, "Home")
    world.deliver(solo, home, 7)

    screen = screen_for(monkeypatch, client, app, solo)

    # -- payload contract, straight off the wire
    assert screen.overview["is_linked"] is False
    assert len(screen.overview["balances"]) == 1
    row = screen.overview["balances"][0]
    assert row["is_grouped"] is False
    assert row["place_group_id"] is None
    assert row["place_members"] == []
    assert row["place_balance"] == 7.0
    assert row["is_own"] is True
    assert row["owner_user_id"] == solo.id
    assert row["address_id"] == home.id

    # -- rendered body: the number lives on the row line, nothing else exists
    assert screen.rows == ["• Home: <b>7</b>"]
    for key in ("place_total", "member_line", "cluster_total", "linked_account_line"):
        assert f"telegram.bottles.{key}" not in screen.text, key
    assert screen.callbacks == [f"bottle_history_{home.id}_1", "back_to_main"]
    assert screen.kwargs["parse_mode"] == "HTML"


# =========================================================================== #
# 2. Grouped place — one pool, one number (D6)
# =========================================================================== #
@pytest.mark.integration
def test_grouped_place_prints_the_pool_number_once_on_the_place_total_line(
    monkeypatch, client, app, db, world
):
    """D6. Reverting to `• {title}: {n}` for grouped rows prints the pool number
    twice (row + place_total), which reads as 14 bottles at a 7-bottle place."""
    a = world.user("Alice")
    b = world.user("Bob")
    a_addr = world.address(a, "Office")
    b_addr = world.address(b, "Office")
    world.group(a_addr, b_addr)
    world.deliver(a, a_addr, 5)
    world.deliver(b, b_addr, 2)

    screen = screen_for(monkeypatch, client, app, a)

    assert len(screen.overview["balances"]) == 1, "scope dedup: one row per PLACE"
    row = screen.overview["balances"][0]
    assert row["is_grouped"] is True
    assert row["place_group_id"] == a_addr.address_group_id
    assert row["place_balance"] == 7.0

    assert screen.rows == ["• Office"]
    assert ":" not in screen.rows[0]
    place_lines = [line.strip() for line in screen.lines if "telegram.bottles.place_total" in line]
    assert place_lines == ["telegram.bottles.place_total 7"]
    # Fixture text is digit-free elsewhere, so this pins "once in the WHOLE body".
    assert screen.text.count("7") == 1


@pytest.mark.integration
def test_both_coworkers_at_a_shared_place_read_the_same_pool(
    monkeypatch, client, app, db, world
):
    """Dev-DB group 9's shape: +6 at A's door, +5 at B's door, -4 returned at
    B's door -> the PLACE holds 7. A per-(user,address) read would give A "6"
    and B "1" — the exact pre-re-key bug — so both directions are pinned."""
    a = world.user("Alice")
    b = world.user("Bob")
    a_addr = world.address(a, "Office")
    b_addr = world.address(b, "Office")
    world.group(a_addr, b_addr)
    world.deliver(a, a_addr, 6)
    world.deliver(b, b_addr, 5)
    world.collect(b, b_addr, 4)

    a_screen = screen_for(monkeypatch, client, app, a)
    b_screen = screen_for(monkeypatch, client, app, b)

    for scr in (a_screen, b_screen):
        assert [ln.strip() for ln in scr.lines if "place_total" in ln] == [
            "telegram.bottles.place_total 7"
        ]
    assert "6" not in a_screen.text and "1" not in a_screen.text
    assert "1" not in b_screen.text and "5" not in b_screen.text

    a_row, b_row = a_screen.overview["balances"][0], b_screen.overview["balances"][0]
    assert a_row["place_group_id"] == b_row["place_group_id"]
    # Each viewer's OWN address is the representative of the shared place.
    assert a_row["address_id"] == a_addr.id and a_row["is_own"] is True
    assert b_row["address_id"] == b_addr.id and b_row["is_own"] is True


@pytest.mark.integration
def test_place_shared_with_an_unlinked_coworker_names_members_without_slicing_the_number(
    monkeypatch, client, app, db, world
):
    """`place_members` is derived from ADDRESS OWNERSHIP, not from the viewer's
    cluster: a "only show my cluster" filter would hide the coworker whose
    empties are inside the 8, making the number unexplainable. A re-introduced
    per-member slice would report a fiction — the pool is indivisible."""
    a = world.user("Alice")
    z = world.user("Zed", "Stranger")          # NOT in Alice's cluster
    a_addr = world.address(a, "Office")
    z_addr = world.address(z, "Office")
    world.group(a_addr, z_addr)
    world.deliver(a, a_addr, 5)
    world.deliver(z, z_addr, 3)

    screen = screen_for(monkeypatch, client, app, a)

    assert screen.overview["is_linked"] is False
    assert "telegram.bottles.cluster_total" not in screen.text
    assert [ln.strip() for ln in screen.lines if "place_total" in ln] == [
        "telegram.bottles.place_total 8"
    ]
    member_lines = [ln.strip() for ln in screen.lines if "member_line" in ln]
    assert sorted(member_lines) == [
        "telegram.bottles.member_line Alice Member",
        "telegram.bottles.member_line Zed Stranger",
    ]
    # No per-member number anywhere; the pool cannot be sliced.
    for member in screen.overview["balances"][0]["place_members"]:
        assert set(member) == {"member_name", "is_own"}
    assert "5" not in screen.text and "3" not in screen.text


@pytest.mark.integration
def test_coworker_who_never_took_a_delivery_still_sees_the_place(
    monkeypatch, client, app, db, world
):
    """Membership comes from `UserAddress`, not from owning a balance/ledger
    row. Iterating balance rows instead of addresses gives B the "no saved
    addresses" empty state while the driver at B's door is offered the pool."""
    a = world.user("Alice")
    b = world.user("Bob")
    a_addr = world.address(a, "Office")
    b_addr = world.address(b, "Office")
    world.group(a_addr, b_addr)
    world.deliver(a, a_addr, 9)          # nothing ever happens at B's door

    screen = screen_for(monkeypatch, client, app, b)

    assert "telegram.bottles.no_balance" not in screen.text
    assert screen.rows == ["• Office"]
    assert [ln.strip() for ln in screen.lines if "place_total" in ln] == [
        "telegram.bottles.place_total 9"
    ]
    names = {m["member_name"] for m in screen.overview["balances"][0]["place_members"]}
    assert names == {"Alice Member", "Bob Member"}


# =========================================================================== #
# 3. Linked clusters
# =========================================================================== #
@pytest.mark.integration
def test_linked_cluster_spanning_three_places_rows_labels_and_client_side_total(
    monkeypatch, client, app, db, world
):
    """The server carries NO total by design: a shared place's balance belongs
    to the place, so summing it per member reports the same bottles once per
    coworker. The bot sums the already-deduped `place_balance` itself."""
    a = world.user("Alice")
    a2 = world.user("AliceTwo")
    z = world.user("Zed", "Stranger")
    world.link_accounts(a, a2)

    home = world.address(a, "Home")
    office = world.address(a, "Office")
    z_office = world.address(z, "Office")
    dacha = world.address(a2, "Dacha")
    world.group(office, z_office)

    world.adjust(a, home, "2.5")
    world.deliver(a, office, 4)
    world.deliver(a2, dacha, 2)
    world.collect(a2, dacha, 3)          # -> -1

    screen = screen_for(monkeypatch, client, app, a)

    assert screen.overview["is_linked"] is True
    rows = screen.overview["balances"]
    assert [r["address_id"] for r in rows] == [office.id, home.id, dacha.id]
    assert [r["place_balance"] for r in rows] == [4.0, 2.5, -1.0]
    assert [r["is_own"] for r in rows] == [True, True, False]

    body = screen.text
    assert "• Office" in screen.rows[0] and ":" not in screen.rows[0]
    assert screen.rows[1] == "• Home: <b>2.5</b>"
    # The sibling's row is labelled with the owning account...
    assert screen.rows[2] == (
        "• telegram.bottles.linked_account_line Dacha AliceTwo Member: <b>-1</b>"
    )
    # ...and the viewer's own row is NOT.
    assert "linked_account_line" not in screen.rows[1]
    assert [ln.strip() for ln in screen.lines if "place_total" in ln] == [
        "telegram.bottles.place_total 4"
    ]
    assert [ln for ln in screen.lines if "cluster_total" in ln] == [
        "telegram.bottles.cluster_total 5.5"
    ]
    assert "Zed Stranger" in body      # the shared place still names its coworker
    assert screen.callbacks == [
        f"bottle_history_{office.id}_1",
        f"bottle_history_{home.id}_1",
        f"bottle_history_{dacha.id}_1",
        "back_to_main",
    ]


@pytest.mark.integration
def test_place_shared_between_two_linked_siblings_is_counted_exactly_once(
    monkeypatch, client, app, db, world
):
    """The multi-phone scenario the re-key exists for. The dedup key must be
    ('g', group_id): keying on address_id gives two rows and doubles the
    customer's reported bottles (28 instead of 14)."""
    a = world.user("Alice")
    a2 = world.user("AliceTwo")
    world.link_accounts(a, a2)
    a_addr = world.address(a, "Office")
    a2_addr = world.address(a2, "Office")
    world.group(a_addr, a2_addr)
    world.deliver(a, a_addr, 14)

    for viewer, own_addr in ((a, a_addr), (a2, a2_addr)):
        screen = screen_for(monkeypatch, client, app, viewer)
        rows = screen.overview["balances"]
        assert len(rows) == 1, rows
        assert rows[0]["place_balance"] == 14.0
        assert rows[0]["is_own"] is True
        assert rows[0]["owner_user_id"] == viewer.id
        assert rows[0]["address_id"] == own_addr.id
        assert [ln for ln in screen.lines if "cluster_total" in ln] == [
            "telegram.bottles.cluster_total 14"
        ]
        assert screen.callbacks == [f"bottle_history_{own_addr.id}_1", "back_to_main"]


@pytest.mark.integration
def test_cluster_of_three_accounts_every_viewer_computes_the_same_total(
    monkeypatch, client, app, db, world
):
    """Cross-member dedup + own-first sorting + the client-side sum interact.
    "every member computes the same cluster total" is the property a PARTIAL
    dedup breaks, and it is invisible in a two-member test."""
    a, a2, a3 = world.user("Alice"), world.user("AliceTwo"), world.user("AliceThree")
    world.link_accounts(a, a2)
    world.link_accounts(a, a3)

    a_solo = world.address(a, "Home")
    a_office = world.address(a, "Office")
    a2_office = world.address(a2, "Office")
    a2_solo = world.address(a2, "Dacha")      # never moves a bottle -> 0
    a3_solo = world.address(a3, "Cottage")
    world.group(a_office, a2_office)

    world.deliver(a, a_solo, 2)
    world.deliver(a, a_office, 6)
    world.adjust(a3, a3_solo, "-1.5")

    totals, place_group_id = [], a_office.address_group_id
    for viewer in (a, a2, a3):
        screen = screen_for(monkeypatch, client, app, viewer)
        rows = screen.overview["balances"]
        assert len(rows) == 4, [r["address_id"] for r in rows]
        grouped = [r for r in rows if r["place_group_id"] == place_group_id]
        assert len(grouped) == 1 and grouped[0]["place_balance"] == 6.0
        total_line = next(ln for ln in screen.lines if "cluster_total" in ln)
        totals.append(total_line)
    assert totals == ["telegram.bottles.cluster_total 6.5"] * 3


@pytest.mark.integration
def test_linked_customer_with_exactly_one_place_still_gets_a_footer_equal_to_the_row(
    monkeypatch, client, app, db, world
):
    """`is_linked` is cluster cardinality, not place cardinality: the number is
    printed twice by design. Pinned so a change to the rule is deliberate."""
    a, a2 = world.user("Alice"), world.user("AliceTwo")
    world.link_accounts(a, a2)          # a2 owns no address
    home = world.address(a, "Home")
    world.deliver(a, home, 4)

    screen = screen_for(monkeypatch, client, app, a)

    assert screen.overview["is_linked"] is True
    assert screen.rows == ["• Home: <b>4</b>"]
    assert [ln for ln in screen.lines if "cluster_total" in ln] == [
        "telegram.bottles.cluster_total 4"
    ]
    assert screen.text.count("4") == 2


@pytest.mark.integration
def test_unlink_removes_the_siblings_places_and_the_footer(
    monkeypatch, client, app, db, world
):
    """`is_linked` is derived from cluster size; a stale `canonical_customer_id`
    on one side keeps a sibling's addresses (and names) on the other's screen —
    a cross-account leak between two real phone numbers."""
    a, a2 = world.user("Alice"), world.user("AliceTwo")
    world.link_accounts(a, a2)
    home = world.address(a, "Home")
    dacha = world.address(a2, "Dacha")
    world.deliver(a, home, 3)
    world.deliver(a2, dacha, "2.5")

    before = screen_for(monkeypatch, client, app, a)
    assert [r["address_id"] for r in before.overview["balances"]] == [home.id, dacha.id]
    assert "telegram.bottles.cluster_total 5.5" in before.text

    world.link.unlink_account(a2.id, actor_admin_id=world.admin.id, reason="e2e unlink")
    db.session.commit()

    after = screen_for(monkeypatch, client, app, a)
    assert after.overview["is_linked"] is False
    assert [r["address_id"] for r in after.overview["balances"]] == [home.id]
    assert after.rows == ["• Home: <b>3</b>"]                # own number unchanged
    assert "cluster_total" not in after.text
    assert "Dacha" not in after.text and "AliceTwo" not in after.text
    assert after.callbacks == [f"bottle_history_{home.id}_1", "back_to_main"]


# =========================================================================== #
# 4. The number itself: zero, negative, fractional
# =========================================================================== #
@pytest.mark.integration
def test_zero_balance_place_with_no_balance_row_at_all_is_listed(
    monkeypatch, client, app, db, world
):
    """`get_place_balance_row` returns None for a never-touched place. A
    `if row:` filter would drop the address entirely and the customer would
    think an address they saved does not exist."""
    solo = world.user("Solo")
    home = world.address(solo, "Home")       # never moves a bottle
    work = world.address(solo, "Work")
    world.deliver(solo, work, 3)

    assert BottleTrackingService.get_place_balance_row(home.id) is None

    screen = screen_for(monkeypatch, client, app, solo)

    assert "telegram.bottles.no_balance" not in screen.text
    assert sorted(screen.rows) == ["• Home: <b>0</b>", "• Work: <b>3</b>"]
    assert sorted(screen.callbacks) == sorted(
        [f"bottle_history_{home.id}_1", f"bottle_history_{work.id}_1", "back_to_main"]
    )


@pytest.mark.integration
def test_over_returned_place_shows_the_raw_signed_number_decision_d1(
    monkeypatch, client, app, db, world
):
    """Decision D1: customers see the raw signed number. A defensive `max(0, x)`
    or `abs()` anywhere on the render path hides a real over-return from the
    customer AND from support."""
    solo = world.user("Solo")
    home = world.address(solo, "Home")
    other = world.address(solo, "Work")
    world.deliver(solo, home, 2)
    world.collect(solo, home, 5)
    world.deliver(solo, other, 1)

    assert BottleTrackingService.get_place_balance(home.id) == Decimal("-3.00")

    screen = screen_for(monkeypatch, client, app, solo)

    assert "• Home: <b>-3</b>" in screen.rows
    assert screen.rows[-1] == "• Home: <b>-3</b>", "negative sorts last among own rows"
    # The number is NOT re-signed, clamped or absolute-valued anywhere.
    assert screen.overview["balances"][-1]["place_balance"] == -3.0
    assert "3" not in screen.text.replace("-3", ""), screen.text
    # ...and no editorial copy was attached to it. Under KEY-ECHO i18n new copy
    # shows up as a NEW KEY, so the strong form of "the bot says nothing about
    # over-returns" is an EXACT key set: an unlinked, ungrouped screen requests
    # the title and nothing else.
    assert i18n_keys_in(screen.text) == {"telegram.bottles.title"}, screen.text
    lowered = screen.text.lower()
    for forbidden in ("over-returned", "over_returned", "over returned", "credit", "you have"):
        assert forbidden not in lowered, forbidden


@pytest.mark.integration
def test_negative_grouped_place_carries_the_sign_on_the_place_total_line(
    monkeypatch, client, app, db, world
):
    """The grouped branch formats through a template kwarg, the ungrouped one
    through an f-string: a sign/normalize bug can exist on one and not the
    other."""
    a, b = world.user("Alice"), world.user("Bob")
    a_addr, b_addr = world.address(a, "Office"), world.address(b, "Office")
    world.group(a_addr, b_addr)
    world.deliver(a, a_addr, 1)
    world.collect(b, b_addr, 4)

    screen = screen_for(monkeypatch, client, app, a)

    assert screen.rows == ["• Office"]
    assert [ln.strip() for ln in screen.lines if "place_total" in ln] == [
        "telegram.bottles.place_total -3"
    ]
    assert len([ln for ln in screen.lines if "member_line" in ln]) == 2
    assert "over-returned" not in screen.text.lower()


@pytest.mark.integration
def test_fractional_balances_survive_without_int_truncation(
    monkeypatch, client, app, db, world
):
    """`_normalize_qty` uses Decimal.normalize()+format(,'f'); any switch to
    int(), round() or str(Decimal) reintroduces truncation or exponent form."""
    a, a2 = world.user("Alice"), world.user("AliceTwo")
    world.link_accounts(a, a2)
    one, two, three, four = (
        world.address(a, "Alpha"), world.address(a, "Bravo"),
        world.address(a, "Charlie"), world.address(a2, "Delta"),
    )
    world.adjust(a, one, "1.5")
    world.adjust(a, two, "2.75")
    world.adjust(a, three, "0.25")
    world.adjust(a2, four, "10.00")

    screen = screen_for(monkeypatch, client, app, a)
    rendered = set(screen.rows)

    assert "• Bravo: <b>2.75</b>" in rendered
    assert "• Alpha: <b>1.5</b>" in rendered
    assert "• Charlie: <b>0.25</b>" in rendered
    assert any(row.endswith("Delta AliceTwo Member: <b>10</b>") for row in rendered), rendered
    assert "1E+1" not in screen.text and "10.00" not in screen.text
    assert [ln for ln in screen.lines if "cluster_total" in ln] == [
        "telegram.bottles.cluster_total 14.5"
    ]


@pytest.mark.integration
def test_exact_zero_renders_a_single_zero_character(monkeypatch, client, app, db, world):
    """Never '0.00'. A place driven back to exactly zero through the real write
    paths must read the same as one that never moved a bottle."""
    solo = world.user("Solo")
    moved = world.address(solo, "Moved")
    untouched = world.address(solo, "Untouched")
    world.deliver(solo, moved, 3)
    world.adjust(solo, moved, -3)
    assert BottleTrackingService.get_place_balance(moved.id) == Decimal("0.00")

    screen = screen_for(monkeypatch, client, app, solo)

    assert "• Moved: <b>0</b>" in screen.rows
    assert "• Untouched: <b>0</b>" in screen.rows
    assert "0.00" not in screen.text
    assert "-0" not in screen.text


@pytest.mark.unit
def test_normalize_qty_renders_negative_zero_as_minus_zero_current_behaviour():
    """PINS CURRENT BEHAVIOUR — see notes.

    `_normalize_qty(-0.0)` -> Decimal('-0.0').normalize() -> format(_, 'f') ->
    '-0'. A customer seeing '-0 bottles' files a support ticket.

    This is NOT reachable through any real write path today (Postgres numeric
    normalises -0 to 0, and no code path multiplies a zero balance by -1), so it
    is pinned as a pure-formatter property rather than xfailed. It SHOULD render
    '0'; the fix is a `+ Decimal(0)` / `if q == 0` guard in `_normalize_qty`.
    """
    assert bottles_mod._normalize_qty(-0.0) == "-0"
    assert bottles_mod._normalize_qty(Decimal("-0.00")) == "-0"
    # ...and the ordinary zeros are fine, so a guard would be a one-value change.
    assert bottles_mod._normalize_qty(0.0) == "0"
    assert bottles_mod._normalize_qty(Decimal("0.00")) == "0"


# =========================================================================== #
# 5. Empty state
# =========================================================================== #
@pytest.mark.integration
def test_empty_state_means_no_saved_addresses_and_only_that(
    monkeypatch, client, app, db, world
):
    """Post-re-key the empty state is unreachable for anyone with an address. If
    a filter (zero balance, no ledger) re-narrows the row set, this copy starts
    lying to customers who DO have addresses."""
    solo = world.user("Solo")           # owns nothing

    stub = _SeededI18n()
    screen = screen_for(monkeypatch, client, app, solo, i18n_get=stub.get, i18n_stub=stub)

    assert screen.overview["balances"] == []
    seeded = seed_tables()["telegram.bottles.no_balance"]["en"]
    assert "address" in seeded.lower(), seeded          # the copy is about ADDRESSES...
    assert "bottle activity" not in seeded.lower()      # ...not about bottle activity
    assert seeded in screen.text
    assert screen.callbacks == ["back_to_main"]
    assert "•" not in screen.text
    assert stub.missing == []


@pytest.mark.integration
def test_linked_cluster_owning_no_addresses_gets_the_empty_state_and_no_footer(
    monkeypatch, client, app, db, world
):
    """The `if not balances` early-return sits BEFORE `_build_balance_lines`.
    Moving the footer above it would print "Total across all your places: 0"
    next to "no addresses saved", and summing an empty list is the classic place
    a None sneaks in."""
    a, a2 = world.user("Alice"), world.user("AliceTwo")
    world.link_accounts(a, a2)          # neither owns an address

    screen = screen_for(monkeypatch, client, app, a)

    assert screen.overview["is_linked"] is True
    assert screen.overview["balances"] == []
    assert "telegram.bottles.no_balance" in screen.text
    assert "cluster_total" not in screen.text
    assert screen.callbacks == ["back_to_main"]


# =========================================================================== #
# 6. Escaping, templates, placeholders
# =========================================================================== #
@pytest.mark.integration
def test_html_escapes_address_title_owner_name_and_member_names(
    monkeypatch, client, app, db, world
):
    """One unescaped user-controlled fragment makes Telegram reject the ENTIRE
    message — the customer sees NOTHING, not a mis-rendered name. Three separate
    escape sites exist (title, owner kwarg, member kwarg); each can regress
    alone, so all three are driven in one screen."""
    a = world.user("Alice")
    a2 = world.user("Bob <b>", "")                      # sibling / row owner
    eve = world.user("Eve <i>", "")                     # unlinked coworker
    obrien = world.user("O'Brien & Sons", "")           # unlinked coworker
    world.link_accounts(a, a2)

    a_home = world.address(a, "Alpha")
    sib_addr = world.address(a2, "Home <3 & Co")
    eve_addr = world.address(eve, "Eve door")
    obrien_addr = world.address(obrien, "OB door")
    world.group(sib_addr, eve_addr, obrien_addr)
    world.deliver(a, a_home, 1)
    world.deliver(eve, eve_addr, 2)

    screen = screen_for(monkeypatch, client, app, a)
    text = screen.text

    assert "Home &lt;3 &amp; Co" in text and "Home <3 & Co" not in text
    assert "Bob &lt;b&gt;" in text and "Bob <b>" not in text
    assert "Eve &lt;i&gt;" in text and "Eve <i>" not in text
    # html.escape(quote=True) turns ' into &#x27;; the ampersand is what matters
    # for Telegram's parser, and the apostrophe must not break the copy.
    assert "O&#x27;Brien &amp; Sons" in text
    assert "O'Brien & Sons" not in text
    # The ONLY tags left in the body are the handler's own <b>…</b> balance
    # wrappers: strip those and no '<' or '>' may remain. A single unescaped
    # user fragment anywhere (title, owner kwarg, member kwarg) trips this even
    # if the three explicit pairs above were all still escaped.
    stripped = text.replace("<b>", "").replace("</b>", "")
    assert "<" not in stripped and ">" not in stripped, stripped


@pytest.mark.integration
def test_full_address_and_address_id_fallbacks_render_and_escape(
    monkeypatch, client, app, db, world
):
    """`_address_label` returns the value RAW by contract and relies on the
    caller to escape; the FALLBACK branches are the ones a refactor forgets.
    Also pins that a title-less address is still browsable."""
    solo = world.user("Solo")
    titled = world.address(solo, None, full_address="5 <b>Baker</b> St")
    # A pin-drop with neither title nor address, through the REAL route.
    bare = create_address_via_api(client, app, solo, latitude=LAT, longitude=LNG)
    assert bare.title is None and not bare.full_address
    world.deliver(solo, titled, 1)

    screen = screen_for(monkeypatch, client, app, solo)

    assert "• 5 &lt;b&gt;Baker&lt;/b&gt; St: <b>1</b>" in screen.rows
    assert "<b>Baker</b>" not in screen.text
    assert f"• Address #{bare.id}: <b>0</b>" in screen.rows
    assert f"bottle_history_{bare.id}_1" in screen.callbacks


@pytest.mark.integration
def test_inline_keyboard_button_text_stays_raw_and_is_not_html_escaped(
    monkeypatch, client, app, db, world
):
    """Button text is NOT HTML-parsed by Telegram. A blanket "escape everything"
    fix makes every customer with an apostrophe or ampersand read '&amp;' on
    their buttons. The asymmetry (escape body, not buttons) is exactly the kind
    of rule that gets "cleaned up"."""
    a = world.user("Alice")
    a2 = world.user("Ann & Co", "")
    world.link_accounts(a, a2)
    own = world.address(a, "Home <3 & Co")
    sib = world.address(a2, "Dacha")
    world.deliver(a, own, 1)

    screen = screen_for(monkeypatch, client, app, a)
    labels = [text for text, _ in screen.buttons]

    assert any("Home <3 & Co" in lbl for lbl in labels), labels
    assert not any("&lt;3" in lbl or "&amp;" in lbl for lbl in labels), labels
    assert any("Dacha (Ann & Co)" in lbl for lbl in labels), labels
    # ...while the BODY of the same screen is escaped.
    assert "Home &lt;3 &amp; Co" in screen.text


@pytest.mark.integration
def test_braces_in_user_data_cannot_become_a_format_placeholder(
    monkeypatch, client, app, db, world
):
    """If anyone switches to `template.format(**kwargs).format(...)`, or
    pre-interpolates the title into the template before .format(), a customer
    can inject a placeholder — at minimum crashing their own screen with a
    KeyError, which i18n swallows into the RAW template."""
    a = world.user("Alice")
    a2 = world.user("{name}", "")
    z = world.user("Zed", "Stranger")
    world.link_accounts(a, a2)
    sib_addr = world.address(a2, "{total}")
    z_addr = world.address(z, "Zed door")
    world.group(sib_addr, z_addr)
    world.deliver(z, z_addr, 6)

    stub = _SeededI18n()
    screen = screen_for(monkeypatch, client, app, a, i18n_get=stub.get, i18n_stub=stub)
    text = screen.text

    # The customer's own text survives verbatim as a LABEL...
    assert "{total}" in text
    assert "{name}" in text
    # ...the real balance is still printed by the place_total template...
    place_line = next(ln for ln in screen.lines if "Bottles at this place" in ln)
    assert place_line.strip().endswith(": 6"), place_line
    # ...and nothing was substituted into the injected braces.
    assert "{total}: 6" not in text
    assert stub.missing == []


@pytest.mark.integration
@pytest.mark.parametrize("language", ["en", "uz", "ru"])
def test_no_raw_placeholder_survives_in_any_seeded_language(
    monkeypatch, client, app, db, world, language
):
    """`telegram_bot/i18n.py:88-93` SWALLOWS the KeyError from `.format()` and
    ships the RAW template to the customer ('👤 {name}'). A template gaining a
    placeholder the handler stopped passing fails soft and silently, in ONE
    language only — so all three are rendered, from the REAL seed sources."""
    a, a2 = world.user("Alice"), world.user("AliceTwo")
    z = world.user("Zed", "Stranger")
    world.link_accounts(a, a2)
    home = world.address(a, "Home")
    office = world.address(a, "Office")
    z_office = world.address(z, "Office")
    dacha = world.address(a2, "Dacha")
    world.group(office, z_office)
    world.deliver(a, home, 2)
    world.deliver(a, office, 4)
    world.deliver(a2, dacha, 1)

    stub = _SeededI18n()
    screen = screen_for(monkeypatch, client, app, a, language=language,
                        i18n_get=stub.get, i18n_stub=stub)

    assert "{" not in screen.text and "}" not in screen.text, screen.text
    # KEY-COVERAGE: every key the handler requested exists in the union of the
    # three seed scripts, in this language. This is the guard that makes the
    # seed step provably deploy-critical.
    assert stub.missing == [], stub.missing
    for label, _cb in screen.buttons:
        assert "{" not in label and "}" not in label, label

    # ...and the empty state in the same language.
    empty = world.user("Empty")
    empty_stub = _SeededI18n()
    empty_screen = screen_for(monkeypatch, client, app, empty, language=language,
                              i18n_get=empty_stub.get, i18n_stub=empty_stub)
    assert "{" not in empty_screen.text and "}" not in empty_screen.text
    assert empty_stub.missing == []


@pytest.mark.integration
@pytest.mark.parametrize("language", ["uz", "ru"])
def test_uz_and_ru_screens_carry_the_numbers_names_and_no_english_leakage(
    monkeypatch, client, app, db, world, language
):
    """Only the `en` template is exercised by most tests; a uz/ru row missing a
    placeholder (or missing entirely) leaks the raw template or drops the number
    for the majority language of this customer base."""
    a, a2 = world.user("Alice"), world.user("AliceTwo")
    z = world.user("Zed", "Stranger")
    world.link_accounts(a, a2)
    office, z_office = world.address(a, "Office"), world.address(z, "Office")
    dacha = world.address(a2, "Dacha")
    world.group(office, z_office)
    world.deliver(a, office, 7)
    world.deliver(a2, dacha, 2)

    stub = _SeededI18n()
    screen = screen_for(monkeypatch, client, app, a, language=language,
                        i18n_get=stub.get, i18n_stub=stub)
    text = screen.text

    assert ": 7" in text                         # the place total
    assert "Zed Stranger" in text and "Alice Member" in text
    assert ": 9" in text                         # the cluster footer
    for english in (
        seed_tables()["telegram.bottles.place_total"]["en"].split("{")[0],
        seed_tables()["telegram.bottles.cluster_total"]["en"].split("{")[0],
    ):
        assert english not in text, english
    assert stub.missing == []


@pytest.mark.integration
def test_a_missing_place_total_translation_deletes_the_grouped_number(
    monkeypatch, client, app, db, world
):
    """DEPLOY-CRITICAL, PINNED AS A FAILURE MODE.

    `telegram_bot/i18n.py:80-93` humanises the last key segment on a miss and
    then silently DROPS every kwarg. An unseeded `telegram.bottles.place_total`
    therefore renders 'Place total' with NO balance at all: every grouped
    customer's bottle count vanishes with no error and no log at the customer's
    level. The key IS seeded today (asserted first) — this simulates the day a
    NEW grouped-place key is added to the handler and not to a seed script."""
    assert "telegram.bottles.place_total" in seed_tables(), "already unseeded?!"

    a, b = world.user("Alice"), world.user("Bob")
    a_addr, b_addr = world.address(a, "Office"), world.address(b, "Office")
    world.group(a_addr, b_addr)
    world.deliver(a, a_addr, 7)

    stub = _SeededI18n(drop_keys={"telegram.bottles.place_total"})
    screen = screen_for(monkeypatch, client, app, a, i18n_get=stub.get, i18n_stub=stub)

    assert "Place total" in screen.text
    assert "7" not in screen.text, "the grouped balance VANISHED — this is the bug shape"
    assert stub.missing == ["telegram.bottles.place_total"]


@pytest.mark.integration
def test_address_id_fallback_is_untranslated_english_on_a_customer_surface(
    monkeypatch, client, app, db, world
):
    """PINS CURRENT BEHAVIOUR — see notes.

    `_address_label` falls back to the literal English `f"Address #{id}"`
    (handlers/bottles.py:211), which reaches uz/ru customers verbatim AND
    exposes an internal database id. Pinned so a fix is deliberate.

    REACHABILITY IS PROVEN, NOT ASSUMED: the row is created through the REAL
    `POST /api/v1/addresses/` as a bare pin-drop (no title, no full_address),
    which that route explicitly accepts."""
    solo = world.user("Solo")
    bare = create_address_via_api(client, app, solo, latitude=LAT, longitude=LNG)
    assert bare.title is None and not bare.full_address

    stub = _SeededI18n()
    screen = screen_for(monkeypatch, client, app, solo, language="uz",
                        i18n_get=stub.get, i18n_stub=stub)

    assert f"• Address #{bare.id}: <b>0</b>" in screen.rows
    # The rest of the same screen IS localised, so the English leak is this one
    # fragment and not an unseeded-language artefact.
    assert stub.missing == [], stub.missing
    assert screen.text.startswith("📦 <b>" + seed_tables()["telegram.bottles.title"]["uz"])


# =========================================================================== #
# 7. History buttons — the keyboard is a promise the API must keep
# =========================================================================== #
@pytest.mark.integration
def test_every_history_button_on_the_screen_actually_opens(
    monkeypatch, client, app, db, world
):
    """The representative address chosen by the overview and the three-arm gate
    in `can_view_address_history` are two INDEPENDENT rules. Any drift produces
    a button that 404s — the customer taps History and gets "could not load"."""
    a, a2 = world.user("Alice"), world.user("AliceTwo")
    z, y = world.user("Zed", "Stranger"), world.user("Yan", "Stranger")
    world.link_accounts(a, a2)

    own_solo = world.address(a, "Home")
    own_office = world.address(a, "Office")
    z_office = world.address(z, "Office")
    sib_solo = world.address(a2, "Dacha")
    sib_office = world.address(a2, "Depot")           # viewer owns NO member here
    y_office = world.address(y, "Depot")
    world.group(own_office, z_office)
    world.group(sib_office, y_office, label="depot")

    world.deliver(a, own_solo, 1)
    world.deliver(a, own_office, 2)
    world.deliver(z, z_office, 3)
    world.deliver(a2, sib_solo, 4)
    world.deliver(y, y_office, 5)

    screen = screen_for(monkeypatch, client, app, a)
    history = [cb for cb in screen.callbacks if cb.startswith("bottle_history_")]
    assert len(history) == 4, screen.buttons

    hdrs = headers(app, a)
    seen = {}
    for cb in history:
        address_id = int(cb.split("_")[-2])
        resp = client.get(LEDGER_URL.format(address_id=address_id), headers=hdrs)
        assert resp.status_code == 200, (cb, resp.status_code, resp.get_json())
        seen[address_id] = resp.get_json()["data"]

    # The GROUPED buttons return the WHOLE place's entries, both members'.
    assert seen[own_office.id]["total"] == 2          # a's 2 + z's 3 => two entries
    assert {i["member_name"] for i in seen[own_office.id]["items"]} == {
        "Alice Member", "Zed Stranger"
    }
    assert seen[sib_office.id]["total"] == 1
    assert {i["member_name"] for i in seen[sib_office.id]["items"]} == {"Yan Stranger"}


@pytest.mark.integration
def test_one_history_button_per_place_not_per_address(
    monkeypatch, client, app, db, world
):
    """The dedup happens in the service; the keyboard blindly maps rows to
    buttons. A dedup regression duplicates buttons AND doubles the footer."""
    a, b = world.user("Alice"), world.user("Bob")
    a1 = world.address(a, "Office A")
    a2 = world.address(a, "Office B")       # SAME group, same owner
    b1 = world.address(b, "Office C")
    solo = world.address(a, "Home")
    world.group(a1, a2, b1)
    world.deliver(a, a1, 5)
    world.deliver(a, solo, 1)

    screen = screen_for(monkeypatch, client, app, a)

    assert len(screen.callbacks) == 3, screen.buttons
    assert screen.callbacks[-1] == "back_to_main"
    grouped_ids = {a1.id, a2.id}
    grouped_buttons = [
        cb for cb in screen.callbacks
        if cb.startswith("bottle_history_") and int(cb.split("_")[-2]) in grouped_ids
    ]
    assert len(grouped_buttons) == 1, grouped_buttons


@pytest.mark.integration
def test_sibling_owned_rows_get_the_owner_name_in_the_button_label(
    monkeypatch, client, app, db, world
):
    """Without the owner suffix the customer cannot tell two identical 'Home'
    buttons apart and opens the wrong history — and the button labels are built
    by a different function than the body labels, so they regress alone."""
    a, a2 = world.user("Alice"), world.user("AliceTwo")
    world.link_accounts(a, a2)
    own = world.address(a, "Home")
    sib = world.address(a2, "Home")
    world.deliver(a, own, 2)
    world.deliver(a2, sib, 1)

    screen = screen_for(monkeypatch, client, app, a)
    labels = {cb: text for text, cb in screen.buttons}

    assert labels[f"bottle_history_{own.id}_1"].endswith(": Home")
    assert labels[f"bottle_history_{sib.id}_1"].endswith(": Home (AliceTwo Member)")
    # The point of the suffix is that the two BUTTON TEXTS differ. Comparing
    # `set(labels)` (the dict's KEYS, i.e. the callbacks) against `len(labels)`
    # is tautologically true and proves nothing — compare the VALUES.
    history_labels = [text for text, cb in screen.buttons if cb.startswith("bottle_history_")]
    assert len(history_labels) == 2, history_labels
    assert len(set(history_labels)) == 2, history_labels


@pytest.mark.integration
def test_a_nameless_linked_sibling_loses_its_disambiguator(
    monkeypatch, client, app, db, world
):
    """PINS CURRENT BEHAVIOUR — see notes.

    `_name()` returns None when first_name and last_name are both blank, which
    is common for Telegram-registered accounts. The handler then skips the
    `linked_account_line` branch entirely (handlers/bottles.py:238), so the
    sibling's 'Home' row and button read exactly like the viewer's own 'Home' —
    two indistinguishable rows with DIFFERENT numbers and different History
    targets. There is no phone-tail / 'Linked account' fallback.

    REACHABILITY IS PROVEN, NOT ASSUMED: the blank name is written through the
    REAL customer route. `PUT /api/v1/auth/profile` assigns `first_name` /
    `last_name` straight from the request body with no validation at all
    (auth_service.py:1667-1670, api/auth.py:1517-1519), so any customer can
    blank their own name from the app in one call."""
    a = world.user("Alice")
    nameless = world.user("Temp", "Name")
    world.link_accounts(a, nameless)
    own = world.address(a, "Home")
    sib = world.address(nameless, "Home")
    world.deliver(a, own, 2)
    world.deliver(nameless, sib, 9)

    blanked = client.put(
        "/api/v1/auth/profile",
        json={"first_name": "", "last_name": ""},
        headers=headers(app, nameless),
    )
    assert blanked.status_code == 200, blanked.get_json()
    db.session.expire_all()
    assert (User.query.get(nameless.id).first_name or "") == ""
    assert (User.query.get(nameless.id).last_name or "") == ""

    screen = screen_for(monkeypatch, client, app, a)

    assert screen.overview["balances"][1]["owner_name"] is None
    # Own place first, sibling second — the compound sort key is deterministic,
    # so an either-order assertion would also accept a broken sort.
    assert screen.rows == ["• Home: <b>2</b>", "• Home: <b>9</b>"], screen.rows
    assert "linked_account_line" not in screen.text
    labels = [text for text, cb in screen.buttons if cb.startswith("bottle_history_")]
    assert len(labels) == 2 and len(set(labels)) == 1, labels   # INDISTINGUISHABLE


@pytest.mark.integration
def test_a_member_with_no_name_renders_the_em_dash_never_the_string_none(
    monkeypatch, client, app, db, world
):
    """`member_name` is None from `_name()`; `str(None)` -> 'None' if the `or`
    guard is dropped. '👤 None' next to a customer's bottles is a visible
    defect."""
    a = world.user("Alice")
    nameless = world.user("", "")
    a_addr, n_addr = world.address(a, "Office"), world.address(nameless, "Office")
    world.group(a_addr, n_addr)
    world.deliver(a, a_addr, 3)

    screen = screen_for(monkeypatch, client, app, a)
    member_lines = [ln.strip() for ln in screen.lines if "member_line" in ln]

    assert sorted(member_lines) == [
        "telegram.bottles.member_line Alice Member",
        "telegram.bottles.member_line —",
    ]
    assert "None" not in screen.text


@pytest.mark.integration
def test_the_viewers_own_name_is_listed_among_the_members_with_no_you_marker(
    monkeypatch, client, app, db, world
):
    """PINS CURRENT BEHAVIOUR. `is_own` is emitted on `place_members` and the
    handler IGNORES it (handlers/bottles.py:261-265). A future "mark yourself"
    or self-exclusion change alters the screen for EVERY grouped customer;
    pinning makes the change visible."""
    a, b = world.user("Alice"), world.user("Bob")
    a_addr, b_addr = world.address(a, "Office"), world.address(b, "Office")
    world.group(a_addr, b_addr)
    world.deliver(a, a_addr, 3)

    screen = screen_for(monkeypatch, client, app, a)
    members = screen.overview["balances"][0]["place_members"]

    assert sorted(m["member_name"] for m in members) == ["Alice Member", "Bob Member"]
    assert [m["is_own"] for m in members].count(True) == 1
    assert "Alice Member" in screen.text
    assert "(you)" not in screen.text.lower()


@pytest.mark.integration
def test_a_stale_history_callback_after_an_unlink_degrades_to_the_load_error(
    monkeypatch, client, app, db, world
):
    """The gate changed from silent-empty-200 to 404; a handler that treated 404
    as "no rows" would show an EMPTY history for an address the customer may no
    longer see, implying their bottles disappeared."""
    from unittest.mock import AsyncMock

    a, a2 = world.user("Alice"), world.user("AliceTwo")
    world.link_accounts(a, a2)
    own, sib = world.address(a, "Home"), world.address(a2, "Dacha")
    world.deliver(a, own, 1)
    world.deliver(a2, sib, 2)

    screen = screen_for(monkeypatch, client, app, a)
    stale_cb = f"bottle_history_{sib.id}_1"
    assert stale_cb in screen.callbacks

    world.link.unlink_account(a2.id, actor_admin_id=world.admin.id, reason="e2e")
    db.session.commit()

    # The endpoint now refuses it.
    resp = client.get(LEDGER_URL.format(address_id=sib.id), headers=headers(app, a))
    assert resp.status_code == 404

    # ...and the handler degrades to the load-error toast, rendering nothing.
    class _Ledger404:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get_my_bottle_ledger(self, *_a, **_kw):
            return APIResponse(success=False, error="Not found", status_code=404)

    _patch_bot(monkeypatch)
    monkeypatch.setattr(bottles_mod, "api_client", _Ledger404())
    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data=stale_cb)
    asyncio.run(bottles_mod.BottleBalanceHandler().show_bottle_history(update, make_context()))

    update.callback_query.edit_message_text.assert_not_called()
    answers = [c.args[0] for c in update.callback_query.answer.call_args_list if c.args]
    assert any("telegram.bottles.load_error" in ans for ans in answers), answers


# =========================================================================== #
# 8. Ordering and determinism
# =========================================================================== #
@pytest.mark.integration
def test_sort_puts_own_places_first_then_descending_balance_then_siblings(
    monkeypatch, client, app, db, world
):
    """The compound key `(not is_own, -place_balance)` is easy to invert or make
    single-key; a sibling's larger balance jumping above the customer's own row
    is a daily-visible regression."""
    a, a2 = world.user("Alice"), world.user("AliceTwo")
    world.link_accounts(a, a2)
    own_small, own_big = world.address(a, "Small"), world.address(a, "Big")
    sib_big, sib_neg = world.address(a2, "SibBig"), world.address(a2, "SibNeg")
    world.deliver(a, own_small, 1)
    world.deliver(a, own_big, 5)
    world.deliver(a2, sib_big, 9)               # largest overall
    world.deliver(a2, sib_neg, 1)
    world.collect(a2, sib_neg, 3)               # -> -2

    screen = screen_for(monkeypatch, client, app, a)

    assert [r["address_id"] for r in screen.overview["balances"]] == [
        own_big.id, own_small.id, sib_big.id, sib_neg.id
    ]
    assert [r["place_balance"] for r in screen.overview["balances"]] == [5.0, 1.0, 9.0, -2.0]
    assert screen.rows[0] == "• Big: <b>5</b>"
    assert screen.rows[-1].endswith("<b>-2</b>")


@pytest.mark.integration
def test_repeated_taps_render_a_byte_identical_screen(
    monkeypatch, client, app, db, world
):
    """The underlying `UserAddress` query has NO `order_by`
    (bottle_tracking_service.py:1439) and the sort is stable over an unordered
    input, so ties and multi-address places are only as deterministic as the
    DB's row order. Pinned here on SQLite; see the notes for the Postgres risk
    and the suggested `.order_by(UserAddress.id.asc())` fix."""
    a = world.user("Alice")
    one, two = world.address(a, "Same"), world.address(a, "Same")
    world.deliver(a, one, 3)
    world.deliver(a, two, 3)

    renders = [screen_for(monkeypatch, client, app, a) for _ in range(3)]
    texts = {scr.text for scr in renders}
    callbacks = {tuple(scr.callbacks) for scr in renders}

    assert len(texts) == 1, texts
    assert len(callbacks) == 1, callbacks
    # "stable across taps" alone is satisfied by ANY fixed order, including one
    # that silently swapped the two tied rows. Pin the order the whole feature
    # publishes elsewhere — lowest address id first — so a change to the
    # (missing) ORDER BY is visible here and not only on Postgres.
    assert renders[0].rows == ["• Same: <b>3</b>", "• Same: <b>3</b>"]
    assert renders[0].callbacks == [
        f"bottle_history_{one.id}_1", f"bottle_history_{two.id}_1", "back_to_main"
    ], renders[0].callbacks


@pytest.mark.integration
def test_the_place_representative_matches_the_admin_surfaces_lowest_id_rule(
    monkeypatch, client, app, db, world
):
    """CONTRACT PIN + KNOWN UNTESTABLE GAP (see notes: NON-DETERMINISTIC PLACE
    REPRESENTATIVE).

    When the viewer owns TWO addresses at ONE place, exactly one of them becomes
    the row the customer sees — it supplies the rendered `address_title` and the
    `bottle_history_<address_id>_1` callback target. `_place_member_address_ids`
    (bottle_tracking_service.py:141-159) documents LOWEST ID FIRST and the admin
    UI's `representative_address_id` / `resolve_place_attribution_user_id` both
    publish that rule; this test pins the customer surface to the SAME id, from
    both directions:

      * the address group's member list resolves to the lowest id, and
      * the rendered row and its History callback carry that same id.

    It also pins that the rule is LOWEST ID and not FIRST CREATED: the higher id
    is inserted first, so an ordering "fix" keyed on creation order fails here.

    HONEST LIMIT OF THIS TEST. `get_customer_bottle_overview` reaches that answer
    by ACCIDENT: bottle_tracking_service.py:1438-1442 has no `order_by`, and the
    only sort afterwards (line 1447) is own-before-sibling — a stable sort over an
    unordered input. So the guarantee is the database's, not the code's. This test
    cannot be made to fail on either harness. SQLite scans in rowid (= id) order.
    On real Postgres the PHYSICAL order CAN be forced apart from id order (see
    `test_the_place_representative_on_real_postgres` below, which asserts the
    reversed `ctid`s outright) — and the overview STILL returns the lowest id,
    because the planner, not the code, is choosing the order. The defect is
    therefore reported statically rather than xfailed: an xfail(strict) here
    would XPASS and go red.
    Fix: `.order_by(UserAddress.id.asc())` at bottle_tracking_service.py:1439.
    """
    a, b = world.user("Alice"), world.user("Bob")
    # HIGHER id created FIRST, so "lowest id" and "first created" disagree.
    a_high = world.address(a, "Office B", address_id=900_002)
    a_low = world.address(a, "Office A", address_id=900_001)
    b1 = world.address(b, "Office C")
    world.group(a_high, a_low, b1)
    world.deliver(a, a_low, 4)

    scope = BottleTrackingService.resolve_scope(a_low.id)
    assert BottleTrackingService._place_member_address_ids(scope)[0] == a_low.id

    screen = screen_for(monkeypatch, client, app, a)
    assert len(screen.overview["balances"]) == 1, screen.overview["balances"]
    chosen = screen.overview["balances"][0]["address_id"]

    assert chosen == a_low.id, (
        "the customer's representative address diverged from the admin surfaces' "
        "lowest-id rule — bottle_tracking_service.py:1439 needs an explicit order_by"
    )
    # The keyboard must target the SAME address, or the row and its History
    # button describe two different doors.
    assert screen.callbacks[0] == f"bottle_history_{a_low.id}_1", screen.callbacks
    assert screen.rows == ["• Office A"], screen.rows


@pytest.mark.integration
def test_the_place_representative_on_real_postgres(pg_app, pg_db):
    """The REAL-POSTGRES half of the pin above — and the evidence for the
    "known untestable gap" it reports.

    Same shape (one customer, TWO addresses at ONE place, HIGHER id inserted
    FIRST), but on a fully-migrated Postgres AND with the physical order forced
    apart from id order: `ctid` is asserted below to be high-id-first, so the
    two candidate answers genuinely differ here. SQLite could never show that —
    it scans in rowid (= id) order, so it agrees with the lowest-id rule by
    accident.

    The customer overview still returns the LOWEST id. That is the finding: with
    no ORDER BY at bottle_tracking_service.py:1438-1442 the guarantee belongs to
    the query planner, not to the code, and this test is what would go red the
    day the plan changes (more rows, a different index, a join reorder) and the
    customer's row + History button start pointing at a different door than
    `_place_member_address_ids` / `representative_address_id` /
    `resolve_place_attribution_user_id` publish. The defect is the MISSING
    `.order_by(UserAddress.id.asc())`, not a reproducible wrong answer.
    """
    world = _World(pg_db)
    a = world.user("Alice")
    b = world.user("Bob")
    # HIGHER id created FIRST, so heap order and id order disagree.
    a_high = world.address(a, "Office B", address_id=900_002)
    a_low = world.address(a, "Office A", address_id=900_001)
    # Explicit id: Postgres' sequence is independent of the forced ids above, so
    # an autoincremented third member would land on id 1 and become the place's
    # lowest-id representative, testing nothing.
    b1 = world.address(b, "Office C", address_id=900_003)
    world.group(a_high, a_low, b1)
    world.adjust(a, a_low, 4)

    # A row UPDATE writes a NEW tuple version at the end of the page, so the
    # customer simply RENAMING their older address (a real, everyday
    # `PUT /api/v1/addresses/<id>`) is enough to make heap order and id order
    # disagree. That is the whole point: nothing exotic is required.
    pg_db.session.execute(
        db_text("UPDATE addresses SET title = :t WHERE id = :i"),
        {"t": "Office A (renamed)", "i": a_low.id},
    )
    pg_db.session.commit()

    # The physical order the unordered query at bottle_tracking_service.py:1439
    # will actually see.
    # The PHYSICAL (ctid) order really is high-id-first — id order and heap order
    # genuinely disagree here. What the unordered query at
    # bottle_tracking_service.py:1439 then returns is the planner's business.
    physical = sorted(
        pg_db.session.execute(
            db_text("SELECT id, ctid FROM addresses WHERE user_id = :uid"), {"uid": a.id}
        ).fetchall(),
        key=lambda r: tuple(int(p) for p in r[1].strip("()").split(",")),
    )
    assert [r[0] for r in physical] == [a_high.id, a_low.id], physical

    scope = BottleTrackingService.resolve_scope(a_low.id)
    assert BottleTrackingService._place_member_address_ids(scope)[0] == a_low.id

    rows = BottleTrackingService().get_customer_bottle_overview(a.id)["balances"]
    assert len(rows) == 1, rows
    assert rows[0]["place_balance"] == 4.0
    assert rows[0]["address_id"] == a_low.id, (
        "the customer's representative address diverged from the admin surfaces' "
        "lowest-id rule — bottle_tracking_service.py:1439 needs an explicit "
        ".order_by(UserAddress.id.asc())"
    )


@pytest.mark.integration
def test_a_twelve_place_screen_renders_every_row_and_a_correct_footer(
    monkeypatch, client, app, db, world
):
    """Off-by-one truncation, an accidental slice or a keyboard-row grouping bug
    shows up only at scale. Also exercises the un-capped screen flagged in the
    notes (Telegram's 4096-char / 100-button limits have no guard)."""
    a, a2 = world.user("Alice"), world.user("AliceTwo")
    z = world.user("Zed", "Stranger")
    world.link_accounts(a, a2)

    expected = {}
    own_addrs, sib_addrs = [], []
    for i in range(6):
        addr = world.address(a, f"Own{i}")
        own_addrs.append(addr)
    for i in range(6):
        addr = world.address(a2, f"Sib{i}")
        sib_addrs.append(addr)
    # Two of the twelve are GROUPED places (one own, one sibling), each sharing
    # with an unlinked stranger.
    z1, z2 = world.address(z, "Zed1"), world.address(z, "Zed2")
    world.group(own_addrs[0], z1, label="g1")
    world.group(sib_addrs[0], z2, label="g2")

    quantities = ["4", "3", "2", "1", "0.5", "-1", "8", "7", "6", "-2", "0.25", "9"]
    for addr, qty, owner in zip(
        own_addrs + sib_addrs, quantities, [a] * 6 + [a2] * 6
    ):
        if Decimal(qty) != 0:
            world.adjust(owner, addr, qty)
        expected[addr.id] = Decimal(qty)

    screen = screen_for(monkeypatch, client, app, a)
    rows = screen.overview["balances"]

    assert len(rows) == 12
    assert len(screen.rows) == 12, screen.rows
    assert len([cb for cb in screen.callbacks if cb.startswith("bottle_history_")]) == 12
    assert screen.callbacks[-1] == "back_to_main"
    assert [r["is_own"] for r in rows] == [True] * 6 + [False] * 6
    own_balances = [r["place_balance"] for r in rows[:6]]
    sib_balances = [r["place_balance"] for r in rows[6:]]
    # EXACT values, not "is sorted": a sortedness check passes on any permutation
    # that happens to be monotonic, including one that lost or duplicated a place.
    assert own_balances == [4.0, 3.0, 2.0, 1.0, 0.5, -1.0], own_balances
    assert sib_balances == [9.0, 8.0, 7.0, 6.0, 0.25, -2.0], sib_balances
    # Every place the customer owns an address at appears exactly once, and the
    # two GROUPED places (one own, one sibling) each carry their own pool alone —
    # the unlinked stranger's addresses contributed nothing.
    assert {r["address_id"] for r in rows} == set(expected)
    grouped_ids = {r["address_id"] for r in rows if r["is_grouped"]}
    assert grouped_ids == {own_addrs[0].id, sib_addrs[0].id}, grouped_ids
    total = sum(Decimal(str(r["place_balance"])) for r in rows)
    assert total == sum(expected.values())
    assert [ln for ln in screen.lines if "cluster_total" in ln] == [
        f"telegram.bottles.cluster_total {bottles_mod._normalize_qty(total)}"
    ]


# =========================================================================== #
# 9. Handler branches: entry points and failure modes
# =========================================================================== #
@pytest.mark.integration
def test_repeated_tap_on_an_unchanged_screen_is_a_noop(monkeypatch, client, app, db, world):
    """`_edit_or_replace_callback_message` special-cases exactly the
    "message is not modified" substring; a change to the fallback path spams the
    chat with a duplicate screen on every re-tap."""
    solo = world.user("Solo")
    home = world.address(solo, "Home")
    world.deliver(solo, home, 2)

    payload = fetch(client, app, solo)
    screen = render(
        monkeypatch, APIResponse(success=True, data=payload),
        edit_error=BadRequest("Message is not modified"),
    )

    # EXACTLY one answer (the spinner dismissal at handlers/bottles.py:349) and
    # EXACTLY one edit attempt — a `>= 1` range would also pass on the fallback
    # path this test exists to rule out.
    assert screen.query.answer.await_count == 1
    assert screen.query.edit_message_text.await_count == 1
    assert screen.text == (
        "📦 <b>telegram.bottles.title</b>\n\n• Home: <b>2</b>"
    ), screen.text
    screen.query.message.reply_text.assert_not_called()
    screen.query.message.delete.assert_not_called()


@pytest.mark.integration
def test_message_entry_path_renders_the_same_screen_via_reply_text(
    monkeypatch, client, app, db, world
):
    """Today's registration is callback-only, so this branch has no production
    traffic and rots. A future menu/command entry point would ship broken."""
    solo = world.user("Solo")
    home = world.address(solo, "Home")
    world.deliver(solo, home, 6)
    payload = fetch(client, app, solo)

    via_callback = render(monkeypatch, APIResponse(success=True, data=payload))
    via_message = render(monkeypatch, APIResponse(success=True, data=payload), as_message=True)

    assert via_message.update.message.reply_text.await_count == 1
    assert via_message.text == via_callback.text
    assert via_message.callbacks == via_callback.callbacks
    assert via_message.kwargs["parse_mode"] == "HTML"


@pytest.mark.integration
def test_no_auth_token_shows_the_auth_error_and_never_calls_the_balances_endpoint(
    monkeypatch, client, app, db, world
):
    """The token guard sits INSIDE the `async with api_client` block; reordering
    it would call the endpoint with a None token and render an empty state
    instead of an auth prompt."""
    solo = world.user("Solo")
    world.address(solo, "Home")
    payload = fetch(client, app, solo)

    screen = render(monkeypatch, APIResponse(success=True, data=payload), token=None)

    assert screen.client.balance_calls == 0
    assert screen.query.edit_message_text.call_args is not None
    assert "telegram.error.auth_failed" in screen.query.edit_message_text.call_args.kwargs["text"]
    assert "telegram.bottles.no_balance" not in screen.query.edit_message_text.call_args.kwargs["text"]


@pytest.mark.integration
def test_api_failure_shows_the_load_error_and_never_the_empty_state(monkeypatch):
    """A backend outage must NOT be reported to the customer as "you have no
    addresses". The success check and the `overview or {}` fallback are one
    refactor away from merging."""
    screen = render(monkeypatch, APIResponse(success=False, error="boom", status_code=500))

    screen.query.edit_message_text.assert_not_called()
    assert any("telegram.bottles.load_error" in ans for ans in screen.answers), screen.answers
    assert not any("no_balance" in ans for ans in screen.answers)

    # PINS A DEFECT SHAPE — NOT A BLESSING. `show_bottle_balance` answers this
    # callback query with NO text at handlers/bottles.py:349, before the API call,
    # and `_handle_api_error` (handlers/base.py:168-175) then answers the SAME
    # query id a SECOND time to carry the load error. Telegram's
    # answerCallbackQuery is one-shot per query, so on a real outage the customer
    # is left staring at the previous screen with the spinner already dismissed
    # and no error at all — and `_reply_error` (base.py:100-108) swallows the
    # resulting BadRequest, so nothing surfaces above the log either.
    # `_handle_auth_error` gets this right by EDITING the message instead.
    calls = screen.query.answer.call_args_list
    assert len(calls) == 2, [c.args for c in calls]
    assert calls[0].args == () and calls[0].kwargs == {}, calls[0]
    assert "telegram.bottles.load_error" in calls[1].args[0]


@pytest.mark.integration
def test_an_expired_jwt_401s_at_the_route_and_surfaces_as_the_load_error(
    monkeypatch, app, db, world
):
    """A change that returned 200 with an empty payload would show the
    "no addresses" copy to a logged-out customer."""
    fresh = app.test_client()          # session `client` leaks cookies into 401 tests
    resp = fresh.get(BALANCES_URL, headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401, resp.get_json()

    screen = render(monkeypatch, APIResponse(success=False, error="unauthorized", status_code=401))

    screen.query.edit_message_text.assert_not_called()
    assert any("telegram.bottles.load_error" in ans for ans in screen.answers)


def _legacy_payload_shapes():
    """Every malformed/legacy envelope the handler must survive.

    The "row missing place_balance" case is derived by DELETING a key from a
    real factory row — never by inventing one, which the shared factory forbids
    (tests/telegram_bot/helpers.py:48-57) precisely so the contract guard can
    see every fixture field."""
    row = overview_balance_row(5, "Home", 3.0)
    stripped = {k: v for k, v in row.items() if k != "place_balance"}
    return [
        ("bare-list-legacy-backend", {"data": [{"address_id": 5, "balance": 2}]}, True),
        ("data-None", {"data": None}, True),
        ("data-empty-dict", {"data": {}}, True),
        ("balances-None", {"data": {"is_linked": False, "balances": None}}, True),
        ("balances-empty", {"data": {"is_linked": False, "balances": []}}, True),
        ("top-level-string", "totally not json", True),
        ("row-missing-place_balance", {"data": overview_payload([stripped])}, False),
    ]


@pytest.mark.integration
@pytest.mark.parametrize(
    "name,payload,expect_empty",
    [pytest.param(n, p, e, id=n) for n, p, e in _legacy_payload_shapes()],
)
def test_malformed_or_legacy_payloads_never_crash_and_never_fabricate_numbers(
    monkeypatch, name, payload, expect_empty
):
    """Two chained `or {}` fallbacks and .get defaults carry all of this; a
    single one removed turns a backend contract change into an unhandled
    exception in the customer's chat.

    NOTE (see notes): every malformed shape is indistinguishable from
    "no saved addresses" on the customer's screen."""
    screen = render(monkeypatch, APIResponse(success=True, data=payload))

    if expect_empty:
        assert "telegram.bottles.no_balance" in screen.text
        assert screen.callbacks == ["back_to_main"]
    else:
        assert screen.rows == ["• Home: <b>0</b>"], screen.rows
        assert screen.callbacks == ["bottle_history_5_1", "back_to_main"]


# =========================================================================== #
# 10. Privacy
# =========================================================================== #
@pytest.mark.integration
def test_a_stranger_sees_only_their_own_place_through_the_balances_endpoint(
    monkeypatch, client, app, db, world
):
    """`get_cluster_user_ids` degrades to a singleton for unlinked users — if it
    ever returned [], the `in_([])` filter plus a wrong default could widen the
    query. A stranger's name on a customer's screen is a privacy incident."""
    a, z, e = world.user("Alice"), world.user("Zed", "Stranger"), world.user("Eve", "Outsider")
    a_addr, z_addr = world.address(a, "Office"), world.address(z, "Office")
    world.group(a_addr, z_addr)
    world.deliver(a, a_addr, 5)
    e_addr = world.address(e, "EveHome")
    world.deliver(e, e_addr, 1)

    screen = screen_for(monkeypatch, client, app, e)

    assert [r["address_id"] for r in screen.overview["balances"]] == [e_addr.id]
    assert screen.rows == ["• EveHome: <b>1</b>"]
    for leaked in ("Alice", "Zed", "Office", "5"):
        assert leaked not in screen.text, leaked
    # ...and the endpoint refuses that place's history outright.
    assert client.get(
        LEDGER_URL.format(address_id=a_addr.id), headers=headers(app, e)
    ).status_code == 404


# =========================================================================== #
# 11. Place lifecycle (Plan C) as the customer sees it
# =========================================================================== #
@pytest.mark.integration
def test_split_with_bottles_leaving_conserves_across_both_customers_screens(
    monkeypatch, client, app, db, world
):
    """The split is a PAIRED admin adjustment; a sign error or a missed leg
    mints/destroys bottles visibly on the customer screen. The departing address
    must also flip `is_grouped` to False so its number moves from `place_total`
    back onto the row line."""
    a, b, c = world.user("Alice"), world.user("Bob"), world.user("Cara")
    a_addr, b_addr, c_addr = (
        world.address(a, "Office"), world.address(b, "Office"), world.address(c, "Office")
    )
    world.group(a_addr, b_addr, c_addr)
    world.deliver(a, a_addr, 10)

    before_a = screen_for(monkeypatch, client, app, a)
    before_b = screen_for(monkeypatch, client, app, b)
    total_before = all_bottles()
    assert [ln.strip() for ln in before_a.lines if "place_total" in ln] == [
        "telegram.bottles.place_total 10"
    ]
    assert len([ln for ln in before_a.lines if "member_line" in ln]) == 3

    # The pre-fill the admin is offered is derived from the DEPARTING address's
    # OWN attributed entries, then clamped to [0, place]. Bob never took a
    # delivery, so his is exactly 0 — a `0 <= x <= 10` range would also accept
    # the bug where every member is pre-filled with the whole place.
    assert BottleTrackingService.suggested_bottles_leaving(
        b_addr.address_group_id, b_addr.id
    ) == Decimal("0.00")
    # ...and Alice, who took all ten, is pre-filled with all ten.
    assert BottleTrackingService.suggested_bottles_leaving(
        a_addr.address_group_id, a_addr.id
    ) == Decimal("10.00")

    result = world.link.remove_address_from_group(
        b_addr.id, acting_admin_id=world.admin.id, reason="Bob moved out", bottles_leaving=4
    )
    db.session.commit()
    assert result["dissolved"] is False
    assert result["bottles_leaving"] == Decimal("4.00")

    after_a = screen_for(monkeypatch, client, app, a)
    after_b = screen_for(monkeypatch, client, app, b)

    assert [ln.strip() for ln in after_a.lines if "place_total" in ln] == [
        "telegram.bottles.place_total 6"
    ]
    assert len([ln for ln in after_a.lines if "member_line" in ln]) == 2
    assert after_b.rows == ["• Office: <b>4</b>"], after_b.rows
    assert "place_total" not in after_b.text and "member_line" not in after_b.text
    assert after_b.overview["balances"][0]["is_grouped"] is False

    # CONSERVATION, as a pair: nothing minted, nothing destroyed.
    assert all_bottles() == total_before
    assert (
        Decimal(str(after_a.overview["balances"][0]["place_balance"]))
        + Decimal(str(after_b.overview["balances"][0]["place_balance"]))
        == Decimal(str(before_a.overview["balances"][0]["place_balance"]))
    )


@pytest.mark.integration
def test_split_default_leaves_the_departing_customer_at_zero_and_still_listed(
    monkeypatch, client, app, db, world
):
    """§8 netting was DELETED. A resurrected netting path (or a get_or_create
    that copies the place balance) would give the departing member a phantom 9
    and double the reported total across the two screens."""
    a, b, c = world.user("Alice"), world.user("Bob"), world.user("Cara")
    a_addr, b_addr, c_addr = (
        world.address(a, "Office"), world.address(b, "Office"), world.address(c, "Office")
    )
    world.group(a_addr, b_addr, c_addr)
    world.deliver(a, a_addr, 9)
    total_before = all_bottles()

    result = world.link.remove_address_from_group(
        b_addr.id, acting_admin_id=world.admin.id, reason="Bob moved out"
    )
    db.session.commit()
    assert result["bottles_leaving"] == Decimal("0.00")
    assert result["dissolved"] is False

    after_a = screen_for(monkeypatch, client, app, a)
    after_b = screen_for(monkeypatch, client, app, b)

    assert [ln.strip() for ln in after_a.lines if "place_total" in ln] == [
        "telegram.bottles.place_total 9"
    ]
    assert after_b.rows == ["• Office: <b>0</b>"]
    assert after_b.callbacks == [f"bottle_history_{b_addr.id}_1", "back_to_main"]
    assert all_bottles() == total_before
    # The departing customer's History button still opens (arm 1 of the gate).
    assert client.get(
        LEDGER_URL.format(address_id=b_addr.id), headers=headers(app, b)
    ).status_code == 200


@pytest.mark.integration
@pytest.mark.parametrize(
    "bottles_leaving",
    [-1, 6, float("nan"), float("inf"), float("-inf"), "abc"],
    ids=["negative", "above-place", "nan", "inf", "-inf", "not-a-number"],
)
def test_a_rejected_split_leaves_both_screens_byte_identical(
    monkeypatch, client, app, db, world, bottles_leaving
):
    """Validation happens before any write, but a flushed audit event or a
    partially-applied adjustment would show up as a changed number on the
    customer's screen. Silent clamping (the RETIRED behaviour) would show a
    wrong-but-plausible number instead of an error."""
    a, b = world.user("Alice"), world.user("Bob")
    a_addr, b_addr = world.address(a, "Office"), world.address(b, "Office")
    world.group(a_addr, b_addr)
    world.deliver(a, a_addr, 5)

    before_a = screen_for(monkeypatch, client, app, a).text
    before_b = screen_for(monkeypatch, client, app, b).text
    total_before = all_bottles()
    ledger_before = ledger_sum(a_addr.id)

    with pytest.raises(ValidationError) as exc:
        world.link.remove_address_from_group(
            b_addr.id, acting_admin_id=world.admin.id, reason="attempt",
            bottles_leaving=bottles_leaving,
        )
    db.session.rollback()
    assert exc.value.error_code == "PLACE_SPLIT_INVALID"

    db.session.expire_all()
    assert UserAddress.query.get(b_addr.id).address_group_id is not None
    assert screen_for(monkeypatch, client, app, a).text == before_a
    assert screen_for(monkeypatch, client, app, b).text == before_b
    assert all_bottles() == total_before
    assert ledger_sum(a_addr.id) == ledger_before


@pytest.mark.integration
def test_a_non_zero_split_out_of_a_non_positive_place_is_rejected(
    monkeypatch, client, app, db, world
):
    """The third arm of `_validated_bottles_leaving` is NOT redundant: an
    over-returned place sits BELOW the default of 0, so "cap at the place
    balance" would reject the default and "clamp to the cap" would quietly
    produce a negative transfer."""
    a, b = world.user("Alice"), world.user("Bob")
    a_addr, b_addr = world.address(a, "Office"), world.address(b, "Office")
    world.group(a_addr, b_addr)
    world.deliver(a, a_addr, 1)
    world.collect(b, b_addr, 4)                  # place at -3
    before = screen_for(monkeypatch, client, app, a).text
    total_before = all_bottles()
    assert total_before == Decimal("-3.00")

    with pytest.raises(ValidationError) as exc:
        world.link.remove_address_from_group(
            b_addr.id, acting_admin_id=world.admin.id, reason="x", bottles_leaving=1
        )
    db.session.rollback()
    assert exc.value.error_code == "PLACE_SPLIT_INVALID"
    assert screen_for(monkeypatch, client, app, a).text == before

    # ...while 0 (the default) is always accepted, even below zero.
    world.link.remove_address_from_group(
        b_addr.id, acting_admin_id=world.admin.id, reason="x", bottles_leaving=0
    )
    db.session.commit()
    survivor = screen_for(monkeypatch, client, app, a)
    departed = screen_for(monkeypatch, client, app, b)
    assert survivor.rows == ["• Office: <b>-3</b>"]     # dissolved onto the survivor
    # BOTH SIDES, and the global sum. A dissolve that copied -3 onto the leaver
    # as well would leave the survivor's screen looking exactly right while the
    # system minted a second debt — one-sided assertions cannot see that.
    assert departed.rows == ["• Office: <b>0</b>"], departed.rows
    assert all_bottles() == total_before


@pytest.mark.integration
def test_dissolve_keeps_the_number_on_the_last_members_screen(
    monkeypatch, client, app, db, world
):
    """§7.3. The dissolve CARRIES the balance across as a paired adjustment
    rather than reconciling; any reliance on `reconcile_balance` here would
    rebuild the survivor at their ledger sum and destroy a seeded/drifted
    figure — visible to the customer as their bottles vanishing."""
    a, b = world.user("Alice"), world.user("Bob")
    a_addr, b_addr = world.address(a, "Office"), world.address(b, "Office")
    group = world.group(a_addr, b_addr)
    world.deliver(a, a_addr, 7)

    before_a = screen_for(monkeypatch, client, app, a)
    assert before_a.rows == ["• Office"]
    assert len([ln for ln in before_a.lines if "member_line" in ln]) == 2
    total_before = all_bottles()

    result = world.link.remove_address_from_group(
        b_addr.id, acting_admin_id=world.admin.id, reason="Bob left"
    )
    db.session.commit()
    assert result["dissolved"] is True

    after_a = screen_for(monkeypatch, client, app, a)
    after_b = screen_for(monkeypatch, client, app, b)

    assert after_a.rows == ["• Office: <b>7</b>"]
    assert "place_total" not in after_a.text and "member_line" not in after_a.text
    assert after_a.overview["balances"][0]["is_grouped"] is False
    assert after_b.rows == ["• Office: <b>0</b>"]
    assert all_bottles() == total_before
    # The memberless AddressGroup row is KEPT (bottle_ledger.address_group_id FK).
    from business_app.models.customer_link import AddressGroup
    assert AddressGroup.query.get(group.id) is not None
    assert client.get(
        LEDGER_URL.format(address_id=a_addr.id), headers=headers(app, a)
    ).status_code == 200


@pytest.mark.integration
def test_joining_a_place_absorbs_the_joiners_own_balance_and_leaves_the_total_alone(
    monkeypatch, client, app, db, world
):
    """§7.2, the bug this closed: if the joiner's own-scope row is deleted
    without crediting, the customer's total silently drops from 8 to 5; if it is
    credited without deleting, the screen shows 11."""
    a, a2 = world.user("Alice"), world.user("AliceTwo")
    z = world.user("Zed", "Stranger")
    world.link_accounts(a, a2)
    office, z_office = world.address(a, "Office"), world.address(z, "Office")
    joiner = world.address(a2, "Dacha")
    group = world.group(office, z_office)
    world.deliver(a, office, 5)
    world.deliver(a2, joiner, 3)

    before = screen_for(monkeypatch, client, app, a)
    total_before = all_bottles()
    assert len(before.overview["balances"]) == 2
    assert "telegram.bottles.cluster_total 8" in before.text

    world.link.add_addresses_to_group(
        group.id, [joiner.id], acting_admin_id=world.admin.id, reason="same office"
    )
    db.session.commit()

    after = screen_for(monkeypatch, client, app, a)

    assert len(after.overview["balances"]) == 1
    assert [ln.strip() for ln in after.lines if "place_total" in ln] == [
        "telegram.bottles.place_total 8"
    ]
    names = {m["member_name"] for m in after.overview["balances"][0]["place_members"]}
    assert names == {"Alice Member", "AliceTwo Member", "Zed Stranger"}
    assert "telegram.bottles.cluster_total 8" in after.text
    assert len([cb for cb in after.callbacks if cb.startswith("bottle_history_")]) == 1
    assert all_bottles() == total_before


@pytest.mark.integration
def test_split_then_readd_returns_the_customers_total_to_its_original_value(
    monkeypatch, client, app, db, world
):
    """`absorb`'s selector is `address_id = a AND address_group_id IS NULL`. The
    IS NULL arm is what stops the FORMER group's history being dragged back in;
    a regression there mints the whole place's history into the new group."""
    a, b, c = world.user("Alice"), world.user("Bob"), world.user("Cara")
    a_addr, b_addr, c_addr = (
        world.address(a, "Office"), world.address(b, "Office"), world.address(c, "Office")
    )
    group = world.group(a_addr, b_addr, c_addr)
    world.deliver(a, a_addr, 12)
    total_before = all_bottles()

    def distinct_total(*screens):
        seen, total = set(), Decimal("0.00")
        for scr in screens:
            for row in scr.overview["balances"]:
                key = ("g", row["place_group_id"]) if row["is_grouped"] else ("a", row["address_id"])
                if key in seen:
                    continue
                seen.add(key)
                total += Decimal(str(row["place_balance"]))
        return total

    step1 = distinct_total(screen_for(monkeypatch, client, app, a),
                           screen_for(monkeypatch, client, app, b))
    assert step1 == Decimal("12")

    world.link.remove_address_from_group(
        b_addr.id, acting_admin_id=world.admin.id, reason="split", bottles_leaving=4
    )
    db.session.commit()
    mid_a = screen_for(monkeypatch, client, app, a)
    mid_b = screen_for(monkeypatch, client, app, b)
    assert "telegram.bottles.place_total 8" in mid_a.text
    assert mid_b.rows == ["• Office: <b>4</b>"]
    assert distinct_total(mid_a, mid_b) == Decimal("12")
    assert all_bottles() == total_before

    world.link.add_addresses_to_group(
        group.id, [b_addr.id], acting_admin_id=world.admin.id, reason="back again"
    )
    db.session.commit()

    end_a = screen_for(monkeypatch, client, app, a)
    end_b = screen_for(monkeypatch, client, app, b)
    assert [ln.strip() for ln in end_a.lines if "place_total" in ln] == [
        "telegram.bottles.place_total 12"
    ]
    assert {m["member_name"] for m in end_a.overview["balances"][0]["place_members"]} == {
        "Alice Member", "Bob Member", "Cara Member"
    }
    assert len(end_b.overview["balances"]) == 1
    assert end_b.overview["balances"][0]["is_grouped"] is True
    assert distinct_total(end_a, end_b) == Decimal("12")
    assert all_bottles() == total_before


@pytest.mark.integration
def test_merge_review_override_makes_the_screen_agree_with_the_ledger(
    monkeypatch, client, app, db, world
):
    """The customer screen reads the STORED balance. The reviewed merge converges
    stored and ledger — after it, `get_place_balance == SUM(quantity)`. If the
    backfill/correction pair is mis-ordered, or one leg is decoupled when it
    should be coupled, the screen shows a number the ledger cannot explain,
    which is exactly the drift class this feature exists to end."""
    a, b = world.user("Alice"), world.user("Bob")
    a_addr, b_addr = world.address(a, "Office"), world.address(b, "Office")

    # A: dev address-24's shape — stored 20, ZERO ledger rows.
    world.drift_shape(a, a_addr, 20)
    # B: two ordinary entries, one of which the admin will exclude.
    world.adjust(b, b_addr, 3)
    world.adjust(b, b_addr, 1, notes="counted twice")

    preview = BottleTrackingService.build_merge_preview([a_addr.id, b_addr.id])
    drop = next(e.id for e in preview["entries"] if e.quantity == Decimal("1.00"))

    # PRE-CONDITION, pinned explicitly. Without it a fixture that quietly lost
    # the drift (an un-deleted "pre-ledger" entry, a repaired balance) still
    # produces 15/15 at the end — the merge converges either way — and the
    # backfill assertion below becomes the only thing that notices, from the
    # wrong end of the test. These four numbers ARE the scenario.
    assert BottleTrackingService.get_place_balance(a_addr.id) == Decimal("20.00")
    assert ledger_sum(a_addr.id) == Decimal("0.00")
    assert BottleTrackingService.get_place_balance(b_addr.id) == Decimal("4.00")
    assert all_bottles() == Decimal("24.00")
    # The preview's TWO figures — what the places hold vs what their merged
    # ledger sums to — and the drift between them.
    assert preview["stored_balance"] == Decimal("24.00")
    assert preview["computed_balance"] == Decimal("4.00")
    assert preview["drift"] == Decimal("20.00")

    world.link.create_place_group(
        [a_addr.id, b_addr.id], acting_admin_id=world.admin.id,
        reason="counted 15 crates on site", label="office",
        excluded_ledger_entry_ids=[drop], resulting_balance=Decimal("15"),
    )
    db.session.commit()

    screen = screen_for(monkeypatch, client, app, a)

    assert [ln.strip() for ln in screen.lines if "place_total" in ln] == [
        "telegram.bottles.place_total 15"
    ]
    # THE convergence guarantee — the strongest guard on the feature.
    assert BottleTrackingService.get_place_balance(a_addr.id) == Decimal("15.00")
    assert ledger_sum(a_addr.id) == Decimal("15.00")
    # ...and the two PRE-merge scopes were absorbed, not merely shadowed. Both
    # readers above are scope-scoped, so an orphaned `bottle_balances` row still
    # holding A's 20 would satisfy every assertion so far while the system as a
    # whole believed in 35 bottles.
    assert all_bottles() == Decimal("15.00"), [
        (b.address_group_id, b.address_id, b.balance) for b in BottleBalance.query.all()
    ]
    assert BottleBalance.query.count() == 1
    # ...and the change arrived ONLY through audited ledger entries.
    scope = BottleTrackingService.resolve_scope(a_addr.id)
    keys = [
        e.idempotency_key
        for e in BottleLedger.query.filter(*scope.ledger_filter()).all()
        if (e.idempotency_key or "").startswith("merge_")
    ]
    assert any(k.startswith("merge_backfill:") for k in keys), keys
    assert any(k.startswith("merge_exclude:") for k in keys), keys
    assert any(k.startswith("merge_correction:") for k in keys), keys


@pytest.mark.integration
def test_a_drifted_place_renders_its_stored_balance_over_an_empty_history(
    monkeypatch, client, app, db, world
):
    """THE most valuable pin on this axis.

    Dev address 24: stored 20, ZERO ledger rows. The customer legitimately sees
    20 bottles with a completely EMPTY history. Any reader that "cleans this up"
    by switching to the ledger sum instantly zeroes real customers' screens."""
    solo = world.user("Solo")
    home = world.address(solo, "Home")
    world.drift_shape(solo, home, 20)

    screen = screen_for(monkeypatch, client, app, solo)

    assert screen.rows == ["• Home: <b>20</b>"]
    assert screen.overview["balances"][0]["place_balance"] == 20.0
    assert ledger_sum(home.id) == Decimal("0.00")

    history = client.get(LEDGER_URL.format(address_id=home.id), headers=headers(app, solo))
    assert history.status_code == 200
    assert history.get_json()["data"]["total"] == 0
    assert history.get_json()["data"]["items"] == []


@pytest.mark.integration
def test_admin_reconcile_destroys_the_customers_number_with_no_ledger_entry(
    monkeypatch, client, app, db, world, admin_auth_headers
):
    """PINS A DESTRUCTIVE, STILL-EXPOSED OPERATION — see notes.

    `reconcile_balance` assigns `balance = ledger_sum` unconditionally, writes NO
    ledger entry, and only logs a warning. Plan C never calls it, but nothing
    stops an admin: one click drops this customer's screen from 20 to 0 with no
    audit row explaining it. Documented here rather than blessed."""
    solo = world.user("Solo")
    home = world.address(solo, "Home")
    world.drift_shape(solo, home, 20)

    before = screen_for(monkeypatch, client, app, solo)
    assert before.rows == ["• Home: <b>20</b>"]
    ledger_rows_before = BottleLedger.query.count()

    resp = client.post(f"/api/v1/admin/bottles/reconcile/{home.id}", headers=admin_auth_headers)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["corrected"] is True
    db.session.expire_all()

    after = screen_for(monkeypatch, client, app, solo)
    assert after.rows == ["• Home: <b>0</b>"], "20 bottles wiped off the customer's account"
    assert BottleLedger.query.count() == ledger_rows_before, (
        "no ledger entry explains the change — the destruction is unaudited"
    )
    # WHAT THE CUSTOMER ACTUALLY SEES. Not just a smaller number: the History
    # screen behind that row is still completely EMPTY, so nothing anywhere on
    # the customer's own surface accounts for the twenty bottles that left. The
    # balance moved and the ledger did not, which is the one thing every other
    # write path in this feature guarantees cannot happen.
    history = client.get(LEDGER_URL.format(address_id=home.id), headers=headers(app, solo))
    assert history.status_code == 200
    assert history.get_json()["data"]["items"] == []
    assert history.get_json()["data"]["total"] == 0
    assert ledger_sum(home.id) == Decimal("0.00")
    assert all_bottles() == Decimal("0.00")


@pytest.mark.integration
def test_a_grouped_address_cannot_be_deleted_and_its_row_stays_on_the_screen(
    monkeypatch, client, app, db, world
):
    """§7.3's fence, from the CUSTOMER entry point. If it were missed, deleting
    the address would orphan the group membership and the customer's screen
    would lose a place whose bottles still exist.

    NOTE: SQLite runs with FOREIGN KEYS OFF, so this proves the SERVICE fence,
    not the FK. The FK behaviour needs the pg_app/pg_db fixtures."""
    a, b = world.user("Alice"), world.user("Bob")
    grouped, solo = world.address(a, "Office"), world.address(a, "Home")
    # The control address deliberately has NO bottle history: an UNGROUPED
    # address that has ever moved a bottle owns a `bottle_balances` row, and
    # deleting it nulls that row's `address_id` into the
    # `(address_group_id IS NULL) <> (address_id IS NULL)` CHECK -> 400
    # "referenced by existing records". That is pre-existing behaviour, not the
    # place fence, and mixing the two would make this test prove neither.
    spare = world.address(a, "Spare")
    b_addr = world.address(b, "Office")
    world.group(grouped, b_addr)
    world.deliver(a, grouped, 5)
    world.deliver(a, solo, 1)

    before = screen_for(monkeypatch, client, app, a)
    hdrs = headers(app, a)

    resp = client.delete(f"/api/v1/addresses/{grouped.id}", headers=hdrs)
    assert resp.status_code == 400, resp.get_json()
    body = resp.get_json()
    assert body["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE", body
    # ...and the customer is told what to do about it, not just refused.
    assert any("place" in str(err).lower() for err in body["errors"]), body

    after = screen_for(monkeypatch, client, app, a)
    assert after.text == before.text
    assert after.callbacks == before.callbacks
    assert f"bottle_history_{grouped.id}_1" in after.callbacks
    # ...and an UNGROUPED address is still deletable, so the 400 above is the
    # place fence and not a blanket refusal.
    assert client.delete(f"/api/v1/addresses/{spare.id}", headers=hdrs).status_code == 200


# =========================================================================== #
# 12. Operational writes moving the customer-visible number
# =========================================================================== #
@pytest.mark.integration
def test_a_delivery_at_the_coworkers_door_moves_the_viewers_number(
    monkeypatch, client, app, db, world
):
    """The whole point of the re-key, crossing the order/delivery path into the
    customer screen. If the delivery is booked to the (user, address) pair
    instead of the place, the viewer's number never moves."""
    a, b = world.user("Alice"), world.user("Bob")
    a_addr, b_addr = world.address(a, "Office"), world.address(b, "Office")
    world.group(a_addr, b_addr)
    world.deliver(a, a_addr, 7)

    before = screen_for(monkeypatch, client, app, a)
    assert "telegram.bottles.place_total 7" in before.text
    assert all_bottles() == Decimal("7.00")

    world.deliver(b, b_addr, 3)               # a DIFFERENT customer's door

    after = screen_for(monkeypatch, client, app, a)
    after_b = screen_for(monkeypatch, client, app, b)
    assert [ln.strip() for ln in after.lines if "place_total" in ln] == [
        "telegram.bottles.place_total 10"
    ]
    # BOTH members read the SAME pool, and only ONE pool exists. Booking the
    # delivery to a second (user, address) scope as well would still show Alice
    # 10 while doubling the bottles the system believes exist.
    assert [ln.strip() for ln in after_b.lines if "place_total" in ln] == [
        "telegram.bottles.place_total 10"
    ]
    assert all_bottles() == Decimal("10.00")
    assert ledger_sum(a_addr.id) == Decimal("10.00")
    assert BottleBalance.query.count() == 1


@pytest.mark.integration
def test_returns_and_admin_adjustments_move_the_same_customer_visible_number(
    monkeypatch, client, app, db, world
):
    """Three different write paths feed one displayed number; a sign-convention
    divergence (returns stored positive somewhere) shows up here and nowhere
    else on the customer surface."""
    solo = world.user("Solo")
    home = world.address(solo, "Home")
    world.deliver(solo, home, 5)

    steps = []

    def snapshot():
        scr = screen_for(monkeypatch, client, app, solo)
        stored = BottleTrackingService.get_place_balance(home.id)
        assert Decimal(str(scr.overview["balances"][0]["place_balance"])) == stored
        # Every one of these three write paths goes through `_create_ledger_entry`,
        # which moves the stored balance AND appends the matching quantity. Drift
        # is therefore invariant here: if a path ever moves one without the other
        # the customer's number stops being explainable by their own history.
        assert ledger_sum(home.id) == stored
        assert all_bottles() == stored
        steps.append(scr.rows[0])

    snapshot()
    world.collect(solo, home, 2)
    snapshot()
    world.adjust(solo, home, 1)
    snapshot()
    world.adjust(solo, home, -4)
    snapshot()

    assert steps == [
        "• Home: <b>5</b>", "• Home: <b>3</b>", "• Home: <b>4</b>", "• Home: <b>0</b>"
    ]


@pytest.mark.integration
def test_an_idempotent_delivery_replay_does_not_double_the_customers_number(
    monkeypatch, client, app, db, world
):
    """Webhook/celery retries are routine. A broken `delivery:{order_id}` key
    doubles the bottles the customer is told they hold — and, downstream, the
    fines they can be charged."""
    solo = world.user("Solo")
    home = world.address(solo, "Home")
    order = world.order(solo, home)

    world.deliver(solo, home, 4, order=order)
    once = screen_for(monkeypatch, client, app, solo)
    entries_once = BottleLedger.query.count()

    world.deliver(solo, home, 4, order=order)          # the SAME order, replayed
    twice = screen_for(monkeypatch, client, app, solo)

    assert once.rows == ["• Home: <b>4</b>"]
    assert twice.rows == once.rows
    assert twice.text == once.text
    assert BottleLedger.query.count() == entries_once
    # The screen and the ledger agreeing is not enough: a replay that credited a
    # SECOND scope would leave both of those untouched.
    assert all_bottles() == Decimal("4.00")
    assert ledger_sum(home.id) == Decimal("4.00")
    assert BottleBalance.query.count() == 1


# =========================================================================== #
# 13. Anti-blind-spot: the fabricated payload must remain a SUBSET of the real
# =========================================================================== #
@pytest.mark.integration
def test_the_shared_fabrication_factory_is_a_subset_of_the_real_payload(
    monkeypatch, client, app, db, world
):
    """This guard's absence is why the re-key shipped with every bot test green
    and the live screen showing 0. It is duplicated (not moved) from
    tests/unit/test_customer_bot_bottles_place.py because THIS file's
    legacy-payload parametrization also builds through that factory: if the
    factory drifts, the malformed-shape tests above start proving nothing."""
    a, z = world.user("Alice"), world.user("Zed", "Stranger")
    a_addr, z_addr = world.address(a, "Office"), world.address(z, "Office")
    world.group(a_addr, z_addr)
    world.deliver(a, a_addr, 5)
    world.deliver(z, z_addr, 2)

    real = fetch(client, app, a)["data"]

    assert set(overview_payload([])) <= set(real)
    assert set(overview_balance_row(1, "x", 0.0)) <= set(real["balances"][0])
    assert set(overview_place_member("x")) <= set(real["balances"][0]["place_members"][0])

    row = real["balances"][0]
    assert row["place_balance"] == 7.0
    assert "cluster_total_balance" not in real
    for stale in ("balance", "place_union_balance", "group_union_balance",
                  "bottle_balance_id", "cluster_total_balance"):
        assert stale not in row, stale

    # The formatter must survive the REAL payload, not just the fabricated one.
    screen = render(monkeypatch, APIResponse(success=True, data={"data": real}))
    assert "telegram.bottles.place_total 7" in screen.text
