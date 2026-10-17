"""The two Telegram sign-up paths invent an email address. Both now build it from the shared
placeholder domains, so `is_deliverable_email` refuses exactly what they write (spec 2026-10-09
§7.1 item 1: a pure refactor of the generators). Driven through the endpoints the bot calls."""

import hashlib
import hmac
import json

import pytest

from business_app.models.user import User
from shared.validators import PLACEHOLDER_EMAIL_DOMAIN_BOT, PLACEHOLDER_EMAIL_DOMAIN_LOCAL, is_deliverable_email

pytestmark = pytest.mark.integration


def test_telegram_registration_writes_a_bluestream_local_placeholder(client, db):
    response = client.post(
        "/api/v1/auth/telegram-register",
        json={"telegram_id": 778899101, "first_name": "Kamola", "language_code": "uz"},
    )

    assert response.status_code == 201, response.get_data(as_text=True)
    user = User.query.filter_by(telegram_id="778899101").one()
    assert user.email == f"telegram_778899101@{PLACEHOLDER_EMAIL_DOMAIN_LOCAL}" == "telegram_778899101@bluestream.local"
    assert is_deliverable_email(user.email) is False


def test_a_first_telegram_login_writes_a_bot_internal_placeholder(app, client, db):
    body = json.dumps({"telegram_id": 778899102, "first_name": "Aziz"}).encode()
    signature = hmac.new(app.config["BOT_WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()

    response = client.post(
        "/api/v1/auth/telegram-login",
        data=body,
        headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": signature},
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    user = User.query.filter_by(telegram_id="778899102").one()
    assert user.email == f"telegram_778899102@{PLACEHOLDER_EMAIL_DOMAIN_BOT}" == "telegram_778899102@bot.internal"
    assert is_deliverable_email(user.email) is False
