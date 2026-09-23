"""The outlet wire contract: the key set the admin UI is pinned to, and the `class` alias."""
from datetime import time

from business_app.models.sales import Outlet, OutletContact
from business_app.serializers.sales_serializers import CreateOutletPayload, serialize_outlet

OUTLET_KEYS = {
    "id", "name", "outlet_type", "channel", "stage", "class", "cadence_days_override", "user_id", "address_id",
    "latitude", "longitude", "address_text", "district", "assigned_agent_user_id", "assigned_agent_name",
    "onboarded_by_user_id", "next_visit_due_at", "agent_next_visit_at", "last_visit_at", "last_order_at",
    "opening_hours", "preferred_visit_window", "delivery_window_start", "delivery_window_end", "payment_terms",
    "legal_form", "tax_id", "preferred_language", "competitor_note", "status_warning",
    "dedupe_candidates",
    "activation_requested_at", "approved_at", "approved_by_user_id", "rejected_reason", "lost_reason", "lost_note",
    "notes", "contacts", "created_at", "updated_at",
}


def test_serialize_outlet_publishes_the_full_key_set(db):
    outlet = Outlet(name="Bahor do'koni", outlet_type="grocery_store", outlet_class="B", latitude=41.31, longitude=69.28,
                    delivery_window_start=time(9, 0), delivery_window_end=time(12, 30))
    outlet.contacts.append(OutletContact(name="Olim aka", phone="+998901112233", role="owner", is_primary=True))
    db.session.add(outlet)
    db.session.commit()

    data = serialize_outlet(outlet)

    assert set(data) == OUTLET_KEYS
    assert data["class"] == "B"
    assert data["delivery_window_start"] == "09:00" and data["delivery_window_end"] == "12:30"
    assert data["contacts"] == [{
        "id": outlet.contacts[0].id, "name": "Olim aka", "phone": "+998901112233",
        "role": "owner", "is_primary": True, "presence_window": None,
    }]


def test_create_payload_accepts_class_alias():
    payload = CreateOutletPayload(**{"name": "X", "outlet_type": "workplace", "class": "A"}).model_dump(exclude_none=True)
    assert payload["outlet_class"] == "A"
