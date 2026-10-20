"""The staff bot signs itself back in after the account's logins were ended (spec 2026-10-19 R9,
controller ruling T7-R2).

A staff member's password reset ends every login of the account, the staff bot's included: its
cached access token is refused on its very next request, although it is still valid by its own
clock. An admin changing the staff member's roles does the same (owner decision R9c, 2026-10-20).
That must not leave the driver reading "session expired" until the token runs out. The API
client drops the refused pair, signs the staff member in again through the signed
`/staff/auth/login` (the Telegram id is already linked, so no invite) and repeats the call once.

The journeys run through the REAL staff dispatcher and the real staff TokenManager (over an
in-memory Redis). The bot's own `_make_request` (signing included) talks to the REAL Flask app, and
the password is reset through the storefront's real routes, which end the logins through
`SessionRevocationService.revoke_all`. Only Telegram, the bot's own SQL, the reset e-mail task and the
bot's token Redis are faked.
"""

from types import SimpleNamespace

import pytest

from business_app.models.delivery import DeliveryPerson
from staff_bot import api_client as staff_api_module
from staff_bot.api_client import StaffAPIClient
from staff_bot.token_manager import TokenManager

from tests.bot_dispatcher_harness import FlaskBackend
from tests.integration.test_session_revocation import _reset_password, next_second
from tests.integration.test_session_revocation import sign_in as sign_in_on_the_website
from tests.integration.test_web_password_reset_and_change import emailed_reset_links  # noqa: F401  (a fixture)
from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID
from tests.staff_bot.test_staff_delivery_journey_dispatcher import (
    LOGIN,
    POOL,
    build_driver,
    capture_errors,
    copy_for,
    menu_label,
    sign_in,
    texts,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class _InMemoryRedis:
    """What the staff TokenManager asks of Redis: the token cache and its refresh lock."""

    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def setex(self, key, _ttl, value):
        self.data[key] = value
        return True

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)
        return 1


def _token_manager():
    tokens = TokenManager("redis://unused")
    tokens.redis, tokens._connected = _InMemoryRedis(), True
    return tokens


@pytest.fixture
def linked_driver(db, delivery_driver):
    """A driver whose Telegram is linked by an admin, with the delivery profile an admin switches off."""
    delivery_driver.telegram_id = str(DEFAULT_DRIVER_TELEGRAM_ID)
    delivery_driver.preferred_language = "en"
    profile = DeliveryPerson(
        user_id=delivery_driver.id,
        full_name="Delivery Driver",
        phone=delivery_driver.phone,
        is_active=True,
        is_available=True,
    )
    db.session.add(profile)
    db.session.commit()
    return delivery_driver, profile


async def _driver_bot_on_the_real_backend(app, monkeypatch):
    """The driver's staff bot with the production TokenManager, attached the way bot.py attaches it,
    and the bot's own request funnel answered by the real Flask app."""
    bot = await build_driver(monkeypatch)
    tokens = _token_manager()
    bot.application.bot_data["token_manager"] = tokens
    client = staff_api_module.api_client
    monkeypatch.setattr(client, "token_manager", tokens)
    monkeypatch.delattr(client, "_make_request")  # the bot's own funnel, not the harness double
    backend = FlaskBackend(app)
    monkeypatch.setattr(client, "_client", backend)
    return bot, tokens, backend


async def test_after_a_staff_password_reset_the_staff_bot_signs_in_again_by_itself(
    app, db, linked_driver, emailed_reset_links, monkeypatch
):
    driver_user, _profile = linked_driver
    bot, tokens, backend = await _driver_bot_on_the_real_backend(app, monkeypatch)
    driver, labels = await sign_in(bot)  # /start: the signed staff login, the pair is cached
    before = (await tokens.get_cached_tokens(DEFAULT_DRIVER_TELEGRAM_ID))["access_token"]
    assert [step[:3] for step in backend.seen] == [("POST", LOGIN, 200)]
    next_second()

    _reset_password(app, driver_user, emailed_reset_links)
    backend.seen.clear()
    errors = capture_errors(bot)
    await bot.send(driver.text(menu_label(labels, "staff.menu.new_orders")))

    after = (await tokens.get_cached_tokens(DEFAULT_DRIVER_TELEGRAM_ID))["access_token"]
    assert [step[:3] for step in backend.seen] == [
        ("GET", POOL, 401),  # the cached token is refused at once
        ("POST", LOGIN, 200),  # the bot signs in again, signed, with no invite
        ("GET", POOL, 200),  # and repeats the call with the new token
    ]
    assert backend.seen[-1][3] == f"Bearer {after}" and after != before
    assert not errors, errors
    assert texts(bot)[-1] == f"📦 {copy_for('staff.delivery.pool_empty')}"


async def test_after_an_admin_changes_the_drivers_roles_the_staff_bot_signs_in_again_by_itself(
    app, db, linked_driver, admin_user, monkeypatch
):
    """R9c (owner, 2026-10-20): a role change ends the account's logins too. The admin gives the
    driver the operator role on the Staff Management page; the staff bot's next call recovers."""
    driver_user, _profile = linked_driver
    bot, tokens, backend = await _driver_bot_on_the_real_backend(app, monkeypatch)
    driver, labels = await sign_in(bot)
    before = (await tokens.get_cached_tokens(DEFAULT_DRIVER_TELEGRAM_ID))["access_token"]
    admin = sign_in_on_the_website(app, admin_user.email, "AdminPassword123!")

    changed = app.test_client().put(
        f"/api/v1/admin/staff/users/{driver_user.id}/roles",
        headers={"Authorization": f"Bearer {admin['access_token']}"},
        json={"staff_roles": ["delivery_driver", "operator"]},
    )
    assert changed.status_code == 200, changed.get_json()
    backend.seen.clear()
    errors = capture_errors(bot)
    await bot.send(driver.text(menu_label(labels, "staff.menu.new_orders")))

    after = (await tokens.get_cached_tokens(DEFAULT_DRIVER_TELEGRAM_ID))["access_token"]
    assert [step[:3] for step in backend.seen] == [
        ("GET", POOL, 401),  # the cached token is refused at once
        ("POST", LOGIN, 200),  # the bot signs in again, signed, with no invite
        ("GET", POOL, 200),  # and repeats the call with the new token
    ]
    assert backend.seen[-1][3] == f"Bearer {after}" and after != before
    assert not errors, errors
    assert texts(bot)[-1] == f"📦 {copy_for('staff.delivery.pool_empty')}"


async def test_a_driver_switched_off_since_the_reset_is_told_why_and_the_call_is_not_repeated(
    app, db, linked_driver, emailed_reset_links, monkeypatch
):
    driver_user, profile = linked_driver
    bot, _tokens, backend = await _driver_bot_on_the_real_backend(app, monkeypatch)
    driver, labels = await sign_in(bot)
    next_second()
    _reset_password(app, driver_user, emailed_reset_links)
    profile.is_active = False  # an admin switches the driver off on the Delivery Persons page
    db.session.commit()
    backend.seen.clear()

    await bot.send(driver.text(menu_label(labels, "staff.menu.new_orders")))

    assert [step[:3] for step in backend.seen] == [
        ("GET", POOL, 401),
        ("POST", LOGIN, 403),  # STAFF_ACCOUNT_DEACTIVATED: nothing to repeat the call with
    ]
    assert texts(bot)[-1] == copy_for("staff.error.api.account_deactivated")


class _ScriptedBackend:
    """Answers by (method, path); records (method, path, Authorization header)."""

    def __init__(self, answers):
        self.answers = answers
        self.seen = []

    async def request(self, method, url, headers=None, params=None, json=None, content=None):
        self.seen.append((method, url, (headers or {}).get("Authorization")))
        status, body = self.answers[(method, url)]
        return SimpleNamespace(
            status_code=status, content=b"{}", json=lambda: body, request=SimpleNamespace(method=method)
        )


async def test_a_token_refused_again_after_signing_in_is_not_retried_a_second_time():
    """One repeat, never a loop: the new token's own 401 goes back to the handler."""
    tokens = _token_manager()
    await tokens.store_tokens(DEFAULT_DRIVER_TELEGRAM_ID, "old-access", "old-refresh")
    fresh = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
    client = StaffAPIClient()
    client.token_manager = tokens
    client._client = _ScriptedBackend({("GET", POOL): (401, {}), ("POST", LOGIN): (200, {"data": fresh})})

    response = await client.get_order_pool("old-access")

    assert client._client.seen == [
        ("GET", POOL, "Bearer old-access"),
        ("POST", LOGIN, None),
        ("GET", POOL, "Bearer new-access"),
    ]
    assert (response.success, response.status_code, response.error_code) == (False, 401, "STAFF_AUTH_REQUIRED")
