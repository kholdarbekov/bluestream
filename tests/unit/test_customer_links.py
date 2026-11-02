"""The website links a customer notice carries (spec 2026-10-27 §10 D22): absolute, built from the app config,
and with no request context (Celery builds them)."""

import pytest

from business_app.services.customer_links import (
    order_page_path,
    order_page_url,
    order_page_url_for,
    subscription_page_url,
    tracking_url,
    website_url,
)

pytestmark = pytest.mark.unit


def test_links_are_absolute_on_the_website_without_a_request(app):
    with app.app_context():  # the Celery case: no request context
        site = app.config["COMPANY_WEBSITE"].rstrip("/")
        assert tracking_url(7) == f"{site}/order-tracking?order_id=7"
        assert subscription_page_url(3) == f"{site}/my-subscriptions?sub=3"
        assert order_page_url_for(7, 3) == f"{site}/my-subscriptions?sub=3"
        assert order_page_url_for(7, None) == f"{site}/order-tracking?order_id=7"


def test_a_trailing_slash_on_the_site_never_doubles(app, monkeypatch):
    monkeypatch.setitem(app.config, "COMPANY_WEBSITE", "https://aqua-element.uz/")
    with app.app_context():
        assert website_url("/x") == "https://aqua-element.uz/x"
        assert tracking_url(7) == "https://aqua-element.uz/order-tracking?order_id=7"


class _Order:
    def __init__(self, order_id, subscription_id):
        self.id, self.subscription_id = order_id, subscription_id


@pytest.mark.parametrize("subscription_id", [None, 3])
def test_an_order_and_its_ids_name_the_same_page(app, subscription_id):
    order = _Order(7, subscription_id)
    with app.app_context():
        assert order_page_url(order) == order_page_url_for(7, subscription_id)
        assert order_page_url(order) == website_url(order_page_path(order))
