"""Route regressions for admin cash reconciliation controls."""

from types import SimpleNamespace

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


def test_confirm_transfer_route_delegates_to_services(client, app, admin_user, monkeypatch):
    transfer = SimpleNamespace(
        id=11,
        driver_cash_session_id=22,
        to_dict=lambda: {
            'id': 11,
            'driver_cash_session_id': 22,
            'declared_transfer_cash': 50000,
            'counted_transfer_cash': 50000,
            'transfer_status': 'confirmed',
        },
    )

    custody_calls = {}
    report_calls = {}

    def _confirm_transfer(self, *, transfer_id, actor_user_id, counted_transfer_cash, notes=None, reason_code=None):
        custody_calls['data'] = {
            'transfer_id': transfer_id,
            'actor_user_id': actor_user_id,
            'counted_transfer_cash': counted_transfer_cash,
            'notes': notes,
            'reason_code': reason_code,
        }
        return transfer

    def _get_session_detail(self, session_id):
        report_calls['session_id'] = session_id
        return {'id': session_id, 'status': 'submitted'}

    monkeypatch.setattr(
        'business_app.services.driver_cash_custody_service.DriverCashCustodyService.confirm_transfer',
        _confirm_transfer,
    )
    monkeypatch.setattr(
        'business_app.services.driver_reconciliation_service.DriverReconciliationService.get_session_detail',
        _get_session_detail,
    )

    response = client.post(
        '/api/v1/admin/staff/cash-reconciliation/transfers/11/confirm',
        headers=_auth_headers(app, admin_user.id),
        json={
            'counted_transfer_cash': 50000,
            'reason_code': 'cash_count_matched',
            'notes': 'Count matched at checkpoint',
        },
    )

    assert response.status_code == 200
    assert custody_calls['data']['transfer_id'] == 11
    assert custody_calls['data']['reason_code'] == 'cash_count_matched'
    assert report_calls['session_id'] == 22
