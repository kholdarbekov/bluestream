"""The "Address changed" push to the driver on the stop (admin order address change, T4).

`staff.notify_order_address_changed` posts one payload to the staff bot under an event id built from
the Celery task id, so a retry collapses in the bot's dedup. It re-reads the delivery on every run,
retries included: an alert whose address was changed again, whose stop moved to another driver, or
whose delivery failed is stale (RF2) and sends nothing. Faked: `_send_staff_webhook` and the task's
`retry`, both bound against their real signatures.
"""

import pytest
from celery.exceptions import Retry

from business_app import db
from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.user import UserAddress
from business_app.tasks import staff_tasks
from shared.enums import DeliveryStatus as S
from tests.integration.test_admin_order_reschedule import _seed, rostered_driver  # noqa: F401  fixture
from tests.integration.test_delivery_failed_alert import _retries, _webhook

pytestmark = pytest.mark.integration

ENDPOINT = "/internal/order-address-changed"


def _alert(db, user_address, rostered_driver, local_noon):
    """An ASSIGNED stop, and the payload the admin PATCH queued for it."""
    seeded = _seed(user_address, rostered_driver, S.ASSIGNED, local_noon.date())
    payload = {
        "telegram_id": int(rostered_driver.telegram_id),
        "delivery_id": seeded.delivery_id,
        "delivery_address_id": user_address.id,
        "order_info": {"order_number": seeded.order_number},
        "old_address": "",
        "new_address": user_address.full_address,
        "lat": user_address.latitude,
        "lng": user_address.longitude,
        "door": {"address": user_address.full_address},
    }
    return seeded, payload


def test_the_alert_posts_the_payload_under_a_retry_stable_event_id(
    db, monkeypatch, user_address, rostered_driver, local_noon
):
    seeded, payload = _alert(db, user_address, rostered_driver, local_noon)
    sent = _webhook(monkeypatch, True)

    result = staff_tasks.notify_order_address_changed.apply(
        args=(seeded.delivery_id, rostered_driver.id, payload), task_id="addr-1"
    ).get()

    assert result == {"success": True, "delivery_id": seeded.delivery_id}
    assert sent == [(ENDPOINT, {**payload, "event_id": "order_address_changed:addr-1"})]


def test_a_refused_post_retries(db, monkeypatch, user_address, rostered_driver, local_noon):
    seeded, payload = _alert(db, user_address, rostered_driver, local_noon)
    sent = _webhook(monkeypatch, False)
    calls = _retries(monkeypatch, staff_tasks.notify_order_address_changed)

    with pytest.raises(Retry):
        staff_tasks.notify_order_address_changed.run(seeded.delivery_id, rostered_driver.id, payload)

    assert len(sent) == 1 and len(calls) == 1 and isinstance(calls[0]["exc"], RuntimeError)


def _supersede(db, change, seeded, user_address, rostered_driver, sample_user):
    delivery = db.session.get(Delivery, seeded.delivery_id)
    if change == "address_changed_again":
        other = UserAddress(user_id=sample_user.id, full_address="9 Other St", city="Tashkent")
        db.session.add(other)
        db.session.flush()
        db.session.get(Order, seeded.order_id).delivery_address_id = other.id
    elif change == "reassigned":
        delivery.delivery_person_id = None
        delivery.status = S.SCHEDULED
    else:
        delivery.status = S.FAILED
    db.session.commit()


@pytest.mark.parametrize("change", ["address_changed_again", "reassigned", "failed"])
def test_a_superseded_alert_is_not_sent(
    db, monkeypatch, sample_user, user_address, rostered_driver, local_noon, change
):  # RF2
    seeded, payload = _alert(db, user_address, rostered_driver, local_noon)
    _supersede(db, change, seeded, user_address, rostered_driver, sample_user)
    sent = _webhook(monkeypatch)
    db.session.expire_all()

    result = staff_tasks.notify_order_address_changed.run(seeded.delivery_id, rostered_driver.id, payload)

    assert result == {"success": False, "reason": "stale", "delivery_id": seeded.delivery_id}
    assert sent == []


def test_a_vanished_delivery_is_stale(db, monkeypatch, rostered_driver):
    sent = _webhook(monkeypatch)

    result = staff_tasks.notify_order_address_changed.run(987654, rostered_driver.id, {"delivery_address_id": 1})

    assert result["reason"] == "stale" and sent == []
