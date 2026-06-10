"""File-based email templates for order_update and delivery_reminder.

These notification types are dispatched with template_data from:
- business_app/api/orders.py (order_update: emergency order + cancellation)
- business_app/tasks/delivery_tasks.py (delivery_reminder: customer + driver payloads)
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

DELIVERY_REMINDER_CUSTOMER_DATA = {
    "user_name": "Test User",
    "user_email": "test@example.com",
    "order_number": "ORD-2026-0042",
    "tracking_number": "TRK-000777",
    "estimated_delivery_time": "15:45",
    "driver_name": "Aziz Karimov",
    "driver_phone": "+998901234567",
}

# One unmistakable per-language marker each, so a language falling back to
# another language's template fails the test.
ORDER_UPDATE_MARKERS = {
    "en": "Order Update",
    "ru": "Обновление заказа",
    "uz": "Buyurtma yangilanishi",
}

DELIVERY_REMINDER_MARKERS = {
    "en": "Delivery Reminder",
    "ru": "Напоминание о доставке",
    "uz": "Yetkazib berish eslatmasi",
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


@pytest.mark.parametrize("language", ["en", "ru", "uz"])
def test_delivery_reminder_renders_in_each_language(service, language):
    html = service.render_template("delivery_reminder", language, DELIVERY_REMINDER_CUSTOMER_DATA)

    assert html is not None
    assert DELIVERY_REMINDER_MARKERS[language] in html
    assert DELIVERY_REMINDER_CUSTOMER_DATA["order_number"] in html
    assert DELIVERY_REMINDER_CUSTOMER_DATA["driver_name"] in html
    assert DELIVERY_REMINDER_CUSTOMER_DATA["estimated_delivery_time"] in html


def test_order_update_minimal_payload_has_no_leaked_placeholders(service):
    # Cancellation path passes no estimated_delivery; emergency path passes
    # no cancellation_reason - every optional key must be guarded.
    html = service.render_template("order_update", "en", {"order_number": "ORD-1"})

    assert html is not None
    assert "ORD-1" in html
    assert "None" not in html


def test_delivery_reminder_driver_payload_renders(service):
    # Driver reminders reuse the same template with a different key set
    # (see send_delivery_reminders in business_app/tasks/delivery_tasks.py).
    driver_data = {
        "delivery_id": 7,
        "order_number": "ORD-2026-0042",
        "customer_name": "Olim Customer",
        "delivery_address": "Tashkent, Chilanzar 5",
        "estimated_time": "16:00",
    }

    html = service.render_template("delivery_reminder", "en", driver_data)

    assert html is not None
    assert driver_data["customer_name"] in html
    assert driver_data["delivery_address"] in html
    assert driver_data["estimated_time"] in html
    assert "None" not in html


def test_delivery_reminder_handles_explicit_none_driver_fields(service):
    # delivery_tasks.py passes driver_name/driver_phone as literal None when
    # no driver is assigned yet.
    data = {
        "order_number": "ORD-2026-0042",
        "tracking_number": "TRK-000777",
        "estimated_delivery_time": "15:45",
        "driver_name": None,
        "driver_phone": None,
    }

    html = service.render_template("delivery_reminder", "en", data)

    assert html is not None
    assert "None" not in html
