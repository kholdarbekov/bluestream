"""The per-provider payment-webhook limiter always gives its counter an expiry.

`_check_provider_webhook_rate_limit` (600 webhooks per provider per minute, refused with 503 + Retry-After so the
gateway retries) ran INCR, and EXPIRE only when the new count was 1, as two separate commands. A worker that died or
lost Redis between the two left the provider's counter with no expiry, and nothing ever repaired it: it kept growing
across minutes and, once past 600, refused every webhook from that gateway for good, so payment confirmations were
lost. The limiter now counts with the same rule as the other Redis limiters (`count_in_window`), which also gives a
counter found without an expiry its window.

Driven through the real webhook endpoint with the requests Click and Payme send. The limiter runs before the
signature check, so a refused webhook never reaches it; an accepted one is a genuine, correctly signed Click
PREPARE. Only the Redis outage in the last test is simulated.
"""

import base64
import logging
from types import SimpleNamespace

import pytest

import business_app.api.payments as payments_api
import business_app.services.payment_service as payment_service_module
from business_app.services.payment_service import PaymentService
from shared.redis_keyspace import RedisKeyspace

from tests.integration.fake_gateways import (
    TEST_CLICK_SHOP_SECRET_KEY,
    TEST_PAYME_SECRET_KEY,
    make_click_webhook_form,
    make_payme_webhook_body,
)
from tests.integration.payment_builders import seed_click_payment
from tests.integration.redis_fakes import DownRedis

pytestmark = pytest.mark.integration

WEBHOOK_URL = "/api/v1/payments/webhook/{provider}"
# The limiter's contract, which this fix must not change: 600 webhooks per provider per minute.
LIMIT, WINDOW = 600, 60
REFUSAL_BODY = {"error": "Provider webhook rate limit exceeded"}


@pytest.fixture
def limiter_redis(matrix_app):
    """The Redis the endpoint's limiter counts in: the client the payment service builds for webhook state."""
    return PaymentService().redis_client


@pytest.fixture
def click_order(db, order_with_address, no_fiscalization):
    """An order awaiting a Click payment, so a PREPARE for it is genuinely payable."""
    seed_click_payment(db, order_with_address)
    return order_with_address


@pytest.fixture
def webhook_limiter_warnings(matrix_app, caplog):
    """WARNING lines the webhook endpoint module writes. The app logger does not propagate, so caplog's handler is
    put on it."""
    matrix_app.logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger=matrix_app.logger.name)
    try:
        yield lambda: [
            record.getMessage()
            for record in caplog.records
            if record.module == "payments" and record.levelno == logging.WARNING
        ]
    finally:
        matrix_app.logger.removeHandler(caplog.handler)


def _post_click_prepare(client, order, click_trans_id):
    """A Click PREPARE for `order`, signed with the shop's secret key as Click signs it."""
    form = make_click_webhook_form(
        action="0",
        click_trans_id=click_trans_id,
        merchant_trans_id=order.order_number,
        amount=str(int(order.total_amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        click_paydoc_id="5231141285",
    )
    return client.post(
        WEBHOOK_URL.format(provider="click"), data=form, content_type="application/x-www-form-urlencoded"
    )


def _post_payme_check(client, order):
    """Payme's CheckPerformTransaction for `order`, with the merchant's Basic credentials."""
    credentials = base64.b64encode(f"Paycom:{TEST_PAYME_SECRET_KEY}".encode()).decode("ascii")
    body = make_payme_webhook_body(
        method="CheckPerformTransaction",
        params={"amount": int(order.total_amount) * 100, "account": {"order_id": order.order_number}},
    )
    return client.post(
        WEBHOOK_URL.format(provider="payme"), json=body, headers={"Authorization": f"Basic {credentials}"}
    )


def _post_webhook(provider, client, order):
    return _post_click_prepare(client, order, "940001") if provider == "click" else _post_payme_check(client, order)


def _count_and_ttl(limiter_redis, provider):
    # Plain ints, so a failing assertion prints numbers and not the client (whose repr holds the password).
    key = RedisKeyspace.webhook_provider_rate(provider)
    return int(limiter_redis.get(key)), int(limiter_redis.ttl(key))


def _assert_refused(resp, provider, ttl):
    assert resp.status_code == 503, resp.get_json()
    assert resp.get_json() == {**REFUSAL_BODY, "provider": provider}
    assert resp.headers["Retry-After"] == str(ttl)


# --------------------------------------------------------------------------- the counter always expires


@pytest.mark.parametrize("provider", ["click", "payme"])
def test_a_counter_stuck_past_the_limit_without_expiry_ends_again(
    matrix_client, click_order, limiter_redis, provider
):
    """The production failure: the provider's counter lost its expiry and is past 600, so every webhook from that
    gateway was refused forever. The next webhook is still refused this minute, but the counter now ends."""
    key = RedisKeyspace.webhook_provider_rate(provider)
    limiter_redis.set(key, LIMIT)
    assert limiter_redis.ttl(key) == -1

    resp = _post_webhook(provider, matrix_client, click_order)

    count, ttl = _count_and_ttl(limiter_redis, provider)
    assert count == LIMIT + 1
    assert 0 < ttl <= WINDOW, "the provider's counter still never resets, so its webhooks stay refused for good"
    _assert_refused(resp, provider, ttl)


@pytest.mark.parametrize(
    "seed, expected_count",
    [(None, 1), (5, 6)],
    ids=["first_webhook_of_the_minute", "counter_left_without_expiry"],
)
def test_an_accepted_webhook_leaves_the_counter_with_its_window(
    matrix_client, click_order, limiter_redis, seed, expected_count
):
    """A genuine PREPARE under the limit is answered, counted, and its counter ends within the minute, whether it
    opened the window or found a counter left without an expiry."""
    key = RedisKeyspace.webhook_provider_rate("click")
    if seed is not None:
        limiter_redis.set(key, seed)
        assert limiter_redis.ttl(key) == -1

    resp = _post_click_prepare(matrix_client, click_order, "940002")

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["error"] == 0, resp.get_json()
    count, ttl = _count_and_ttl(limiter_redis, "click")
    assert count == expected_count
    assert 0 < ttl <= WINDOW


# --------------------------------------------------------------------------- the limit itself is unchanged


def test_the_six_hundredth_webhook_is_answered_and_the_next_one_refused(
    matrix_client, click_order, limiter_redis, webhook_limiter_warnings
):
    """599 Click webhooks already arrived this minute (from Click's many addresses). The 600th, a genuine PREPARE, is
    answered; the 601st is refused with 503 and the counter's remaining seconds, logged, and the refusal does not
    stretch the window."""
    key = RedisKeyspace.webhook_provider_rate("click")
    limiter_redis.set(key, LIMIT - 1, ex=WINDOW)

    accepted = _post_click_prepare(matrix_client, click_order, "940003")
    refused = _post_click_prepare(matrix_client, click_order, "940004")

    assert accepted.status_code == 200, accepted.get_json()
    assert accepted.get_json()["error"] == 0, accepted.get_json()
    count, ttl = _count_and_ttl(limiter_redis, "click")
    assert count == LIMIT + 1
    assert 0 < ttl <= WINDOW
    _assert_refused(refused, "click", ttl)
    assert webhook_limiter_warnings() == [f"Provider webhook rate limit hit (provider=click, count={LIMIT + 1})"]


# --------------------------------------------------------------------------- a Redis outage never refuses a webhook


def test_the_limiter_lets_webhooks_through_while_redis_is_down(
    matrix_client, click_order, webhook_limiter_warnings, monkeypatch
):
    """Fails open, as before: when the counter cannot reach Redis, the limiter does not answer the webhook; the
    request goes on to the signature check, which decides the reply."""
    with monkeypatch.context() as building:
        building.setattr(payment_service_module, "redis", SimpleNamespace(from_url=lambda *a, **k: DownRedis()))
        service_without_redis = PaymentService()
    monkeypatch.setattr(payments_api, "get_payment_service", lambda: service_without_redis)

    resp = _post_click_prepare(matrix_client, click_order, "940005")

    assert resp.status_code == 200, resp.get_json()
    assert "Retry-After" not in resp.headers
    assert resp.get_json() != {**REFUSAL_BODY, "provider": "click"}
    assert "Provider webhook rate-limit check failed (provider=click): Connection refused" in webhook_limiter_warnings()
