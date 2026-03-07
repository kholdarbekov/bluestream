"""Regression tests for admin notification template serialization."""

from business_app.models.notification import NotificationTemplate
from business_app.services.notification_service import NotificationService


def test_admin_template_serialization_builds_combined_translations(db, monkeypatch):
    template = NotificationTemplate(
        name='Delivery reminder',
        notification_type='delivery_update',
        channel='telegram',
        subject='Reminder',
        content='Driver is nearby',
        is_active=True,
    )
    db.session.add(template)
    db.session.commit()

    translations_by_field = {
        'name': {'en': 'Delivery reminder', 'ru': 'Напоминание о доставке'},
        'subject': {'en': 'Reminder', 'ru': 'Напоминание'},
        'content': {'en': 'Driver is nearby', 'ru': 'Курьер рядом'},
    }
    monkeypatch.setattr(template, 'get_all_translations', lambda field_name: translations_by_field[field_name])

    service = NotificationService()
    payload = service._serialize_admin_notification_template(template, include_translations=True)

    assert payload['translations'] == {
        'en': {
            'name': 'Delivery reminder',
            'subject': 'Reminder',
            'content': 'Driver is nearby',
        },
        'ru': {
            'name': 'Напоминание о доставке',
            'subject': 'Напоминание',
            'content': 'Курьер рядом',
        },
    }
