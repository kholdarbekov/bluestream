// Hand-copies of business_app/models/sales.py tuples, in the backend's order. Pinned by
// tests/unit/test_admin_ui_payload_fixture_contracts.py::test_outlets_page_mirrors_the_backend_enumerations
// — a value the backend accepts but no picker offers is invisible, and one a picker offers but
// the backend refuses 400s only when an admin picks it.
export const OUTLET_CLASSES = ['A', 'B', 'C'];
export const PAYMENT_TERMS = ['cash', 'business_account'];
export const CONTACT_ROLES = ['owner', 'decision_maker', 'receiver', 'payer'];
