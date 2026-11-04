"""File-based email templates for order_update.

This notification type is dispatched with template_data from:
- business_app/api/orders.py (order_update: emergency order + cancellation)
"""

import pytest

from business_app.services.email_template_service import EmailTemplateService

ORDER_UPDATE_DATA = {
    "user_name": "Test User",
    "user_email": "test@example.com",
    "order_number": "ORD-2026-0042",
    "estimated_delivery": "14:30",
    "cancellation_reason": "Customer request",
}

# One unmistakable per-language marker each, so a language falling back to
# another language's template fails the test.
ORDER_UPDATE_MARKERS = {
    "en": "Order Update",
    "ru": "Обновление заказа",
    "uz": "Buyurtma yangilanishi",
}


@pytest.fixture
def service():
    # Fresh instance (not the singleton) so cached Jinja env/dirs do not leak.
    return EmailTemplateService()


@pytest.mark.parametrize("language", ["en", "ru", "uz"])
def test_order_update_renders_in_each_language(service, language):
    html = service.render_template("order_update", language, ORDER_UPDATE_DATA)

    assert html is not None
    assert ORDER_UPDATE_MARKERS[language] in html
    assert ORDER_UPDATE_DATA["order_number"] in html
    assert ORDER_UPDATE_DATA["cancellation_reason"] in html
    assert ORDER_UPDATE_DATA["estimated_delivery"] in html


def test_order_update_minimal_payload_has_no_leaked_placeholders(service):
    # Cancellation path passes no estimated_delivery; emergency path passes
    # no cancellation_reason - every optional key must be guarded.
    html = service.render_template("order_update", "en", {"order_number": "ORD-1"})

    assert html is not None
    assert "ORD-1" in html
    assert "None" not in html
