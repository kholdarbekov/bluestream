"""Helpers the prepayment payout test files share (spec 2026-11-10): routes, and the admin/driver calls that
book credit, record, undo and read back a return the way the screens do. One home, so no test module imports
another's privates. Test-file-specific helpers stay in their file."""

from decimal import Decimal
from itertools import count

from flask_jwt_extended import create_access_token

from business_app.models.payment import CashCollectionEvent, CustomerCreditPayout, DriverCashSession
from business_app.services.cash_collection_service import CashCollectionService

BASE = "/api/v1/admin/staff/cash-reconciliation"
PREVIEW = BASE + "/customers/{}/prepayment-payouts/preview"
APPLY = BASE + "/customers/{}/prepayment-payouts"
UNDO = BASE + "/prepayment-payouts/{}/undo"
HISTORY = BASE + "/customers/{}/prepayment-history"
COLLECTIONS = BASE + "/collections"
SESSION = BASE + "/sessions/{}"
HAND_IN = "/api/v1/staff/reconciliation/session/submit"
REASON = "Recorded by mistake"

_keys = count(1)


def unique_key(prefix="k"):
    """An idempotency key no other return in this process used (a reused key replays or is refused)."""
    return f"{prefix}-{next(_keys)}"


# --------------------------------------------------------------------------- credit and returns


def credit(client, headers, customer_id, amount):
    """The admin "Record collection" modal: `amount` paid ahead at the office. Returns the credit event's id."""
    response = client.post(
        COLLECTIONS,
        json={
            "customer_id": customer_id,
            "amount": amount,
            "source": "admin_adjustment",
            "notes": "Prepaid in cash at the office",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["cash_collection_event"]["id"]


def return_office(client, headers, customer_id, key=None, **body):
    """An office return through the apply route; returns the new payout's id."""
    payload = {"paid_by": "office", "reason": "Customer moved away", "idempotency_key": key or unique_key(), **body}
    response = client.post(APPLY.format(customer_id), json=payload, headers=headers)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["payout"]["id"]


def undo(client, headers, payout_id, reason=REASON):
    response = client.post(UNDO.format(payout_id), json={"reason": reason}, headers=headers)
    return response.status_code, response.get_json()


def unapplied(db, event_id):
    db.session.expire_all()
    return Decimal(str(db.session.get(CashCollectionEvent, event_id).unapplied_amount))


def balance(customer_id):
    return CashCollectionService().get_customer_prepaid_balance(customer_id)


def payout_row(db, payout_id):
    db.session.expire_all()
    return db.session.get(CustomerCreditPayout, payout_id)


# --------------------------------------------------------------------------- the driver's current trip


def collect_on_trip(client, headers, customer_id, driver_id, amount):
    """The admin "Record collection" modal with the driver as collector: the customer paid `amount` to the
    driver, owing nothing, so it is the customer's credit and it is in the driver's trip cash."""
    response = client.post(
        COLLECTIONS,
        json={
            "customer_id": customer_id,
            "amount": amount,
            "source": "standalone_meeting",
            "collector_user_id": driver_id,
            "notes": "Paid ahead to the driver",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["cash_collection_event"]["driver_cash_session_id"]


def driver_headers(app, driver):
    with app.app_context():
        token = create_access_token(identity=str(driver.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def hand_in(client, app, driver, amount):
    """The driver's "hand in" tap in the staff bot: it posts the figure the button showed."""
    response = client.post(HAND_IN, json={"declared_cash": amount}, headers=driver_headers(app, driver))
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def from_trip(driver, **body):
    return {"paid_by": "driver_current_session", "driver_user_id": driver.id, **body}


def preview_payout(client, headers, customer_id, **body):
    response = client.post(PREVIEW.format(customer_id), json=body, headers=headers)
    return response.status_code, response.get_json()


def apply_payout(client, headers, customer_id, key="trip-1", **body):
    """The apply route with the modal's body; returns (status, json) without asserting."""
    payload = {"reason": "Customer moved away", "idempotency_key": key, **body}
    response = client.post(APPLY.format(customer_id), json=payload, headers=headers)
    return response.status_code, response.get_json()


def session_detail(client, headers, session_id):
    response = client.get(SESSION.format(session_id), headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def stored(db, session_id):
    db.session.expire_all()
    return db.session.get(DriverCashSession, session_id)
