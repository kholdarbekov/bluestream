"""Route regressions for admin cash reconciliation controls."""

import pytest
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


# ─── Report window: GET /admin/staff/cash-reconciliation ─────────────────────
# Driven the way the Delivery Reports and Staff Management pages call it: no
# dates on first load, then the picked `start_date`/`end_date` pair.

REPORT_URL = '/api/v1/admin/staff/cash-reconciliation'


def _closed_session(db, driver, *, started_utc, sid):
    from decimal import Decimal

    from business_app.models.payment import DriverCashSession
    from shared.enums import DriverCashSessionStatus

    session = DriverCashSession(
        session_id=sid,
        driver_user_id=driver.id,
        status=DriverCashSessionStatus.MISMATCH,
        session_started_at=started_utc,
        verified_at=started_utc,
        gross_cash_collected=Decimal('10000.00'),
        blocked_from_cod=True,
    )
    db.session.add(session)
    db.session.commit()
    return session.id


def test_report_without_dates_is_the_last_seven_local_days(client, app, admin_user, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from business_app.utils import local_windows

    # 01:20 Tashkent on 2026-09-16 is still 2026-09-15 in UTC: a UTC "today"
    # would answer one day early.
    monkeypatch.setattr(
        local_windows, 'local_now', lambda: datetime(2026, 9, 16, 1, 20, tzinfo=ZoneInfo('Asia/Tashkent'))
    )

    response = client.get(REPORT_URL, headers=_auth_headers(app, admin_user.id))

    assert response.status_code == 200
    data = response.get_json()['data']
    assert (data['start_date'], data['end_date']) == ('2026-09-10', '2026-09-16')


def test_report_for_a_picked_range_lists_only_sessions_inside_it(client, app, db, admin_user, delivery_driver):
    from datetime import UTC, datetime

    with app.app_context():
        inside = _closed_session(db, delivery_driver, started_utc=datetime(2026, 3, 2, 10, 0, tzinfo=UTC), sid='S-RNG-1')
        _closed_session(db, delivery_driver, started_utc=datetime(2026, 3, 10, 10, 0, tzinfo=UTC), sid='S-RNG-2')

    response = client.get(
        REPORT_URL,
        query_string={'start_date': '2026-03-01', 'end_date': '2026-03-05'},
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    data = response.get_json()['data']
    assert (data['start_date'], data['end_date']) == ('2026-03-01', '2026-03-05')
    assert [item['id'] for item in data['sessions']] == [inside]
    assert data['summary']['session_count'] == 1


def test_report_answers_a_range_longer_than_the_sales_cap(client, app, admin_user):
    response = client.get(
        REPORT_URL,
        query_string={'start_date': '2025-01-01', 'end_date': '2025-12-31'},
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    data = response.get_json()['data']
    assert (data['start_date'], data['end_date']) == ('2025-01-01', '2025-12-31')


@pytest.mark.parametrize(
    'query',
    [
        {'start_date': '06/03/2026', 'end_date': '2026-03-05'},
        {'start_date': '2026-03-05', 'end_date': '2026-03-01'},
    ],
    ids=['malformed-date', 'reversed-range'],
)
def test_report_refuses_a_range_it_cannot_honour(client, app, admin_user, query):
    response = client.get(REPORT_URL, query_string=query, headers=_auth_headers(app, admin_user.id))

    assert response.status_code == 400
