"""Route regressions for admin cash reconciliation controls."""

from flask_jwt_extended import create_access_token


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims={'role': 'admin'})
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def test_verify_reconciliation_requires_reason_code(client, app, admin_user):
    response = client.post(
        '/api/v1/admin/staff/cash-reconciliation/sessions/1/verify',
        headers=_auth_headers(app, admin_user.id),
        json={'verified_cash': 1000},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert 'reason_code is required' in payload.get('errors', [])


def test_resolve_reconciliation_requires_reason_code(client, app, admin_user):
    response = client.post(
        '/api/v1/admin/staff/cash-reconciliation/sessions/1/resolve',
        headers=_auth_headers(app, admin_user.id),
        json={'resolution_notes': 'Fixed'},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert 'reason_code is required' in payload.get('errors', [])


def test_force_close_requires_reason(client, app, admin_user):
    response = client.post(
        '/api/v1/admin/staff/cash-reconciliation/sessions/1/force-close',
        headers=_auth_headers(app, admin_user.id),
        json={},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert 'reason is required' in payload.get('errors', [])


def test_force_close_forbidden_for_non_admin(client, app, sample_user):
    with app.app_context():
        token = create_access_token(
            identity=str(sample_user.id), additional_claims={'role': 'customer'}
        )
    response = client.post(
        '/api/v1/admin/staff/cash-reconciliation/sessions/1/force-close',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'reason': 'nope'},
    )

    assert response.status_code == 403
