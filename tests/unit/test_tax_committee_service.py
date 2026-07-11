"""Tests for TaxCommitteeService — token management and marking code utilisation."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, MagicMock

import pytest
import requests

from business_app import db
from business_app.models.payment import TaxCommitteeApiToken
from business_app.models.product import Product, ProductFiscalProfile
from business_app.services.tax_committee_service import TaxCommitteeService
from business_app.utils.exceptions import ValidationError


def _configure_tax_committee_context(app):
    app.config['TAX_COMMITTEE_API_URL'] = 'https://xtrace.test.uz'
    app.config['TAX_COMMITTEE_BUSINESS_PLACE_ID'] = '101380'
    app.config['TAX_COMMITTEE_PRODUCT_GROUP'] = 'water'
    app.config['TAX_COMMITTEE_RELEASE_TYPE'] = 'PRODUCTION'
    app.config['TAX_COMMITTEE_MANUFACTURER_COUNTRY'] = 'UZ'
    app.config['TAX_COMMITTEE_API_TIMEOUT_SECONDS'] = 5
    app.config['TAX_COMMITTEE_API_TOKEN'] = 'seed-token-abc'
    app.config['TAX_COMMITTEE_UTILISATION_ENABLED'] = True
    app.config['COMPANY_TIN'] = '306522134'


@pytest.mark.unit
class TestTaxCommitteeTokenManagement:
    def test_get_active_token_returns_none_when_no_rows(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        assert service.get_active_token() is None

    def test_seed_token_from_config_creates_row(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        token_row = service._seed_token_from_config()
        assert token_row.token == 'seed-token-abc'
        assert token_row.is_active is True

    def test_seed_token_raises_when_no_config(self, app, db):
        _configure_tax_committee_context(app)
        app.config['TAX_COMMITTEE_API_TOKEN'] = None
        service = TaxCommitteeService()
        with pytest.raises(ValidationError, match='No Tax Committee API token'):
            service._seed_token_from_config()

    def test_check_token_validity_valid(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'isTinCorrect': True,
            'expiresOn': '2026-07-05T17:29:05.388512290Z',
        }
        with patch('business_app.services.tax_committee_service.requests.get', return_value=mock_resp):
            result = service.check_token_validity('valid-token')
        assert result['isTinCorrect'] is True
        assert 'expiresOn' in result

    def test_check_token_validity_invalid(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{'code': 'invalid-token'}]
        mock_resp.text = '[{"code": "invalid-token"}]'
        with patch('business_app.services.tax_committee_service.requests.get', return_value=mock_resp):
            result = service.check_token_validity('bad-token')
        assert result.get('valid') is False

    def test_refresh_token_success(self, app, db):
        _configure_tax_committee_context(app)
        # Create old active token
        old_row = TaxCommitteeApiToken(token='old-token', is_active=True)
        db.session.add(old_row)
        db.session.flush()

        service = TaxCommitteeService()
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'apiKey': 'new-token-xyz'}
        with patch('business_app.services.tax_committee_service.requests.post', return_value=mock_resp):
            new_token = service.refresh_token('old-token')

        assert new_token == 'new-token-xyz'
        db.session.refresh(old_row)
        assert old_row.is_active is False
        new_row = TaxCommitteeApiToken.query.filter_by(is_active=True).first()
        assert new_row.token == 'new-token-xyz'

    def test_refresh_token_failure(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        mock_resp = Mock()
        mock_resp.status_code = 401
        mock_resp.text = 'Unauthorized'
        with patch('business_app.services.tax_committee_service.requests.post', return_value=mock_resp):
            with pytest.raises(ValidationError, match='Failed to refresh'):
                service.refresh_token('bad-old-token')

    def test_ensure_valid_token_seeds_and_checks(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        mock_check_resp = Mock()
        mock_check_resp.status_code = 200
        mock_check_resp.json.return_value = {
            'isTinCorrect': True,
            'expiresOn': '2026-07-05T17:29:05Z',
        }
        with patch('business_app.services.tax_committee_service.requests.get', return_value=mock_check_resp):
            token = service._ensure_valid_token()
        assert token == 'seed-token-abc'
        row = TaxCommitteeApiToken.query.filter_by(is_active=True).first()
        assert row is not None
        assert row.last_checked_at is not None

    def test_ensure_valid_token_refreshes_when_invalid(self, app, db):
        _configure_tax_committee_context(app)
        db.session.add(TaxCommitteeApiToken(token='expired-token', is_active=True))
        db.session.flush()

        service = TaxCommitteeService()
        mock_check_resp = Mock()
        mock_check_resp.status_code = 200
        mock_check_resp.json.return_value = [{'code': 'invalid-token'}]
        mock_check_resp.text = 'invalid'

        mock_refresh_resp = Mock()
        mock_refresh_resp.status_code = 200
        mock_refresh_resp.json.return_value = {'apiKey': 'refreshed-token'}

        with patch('business_app.services.tax_committee_service.requests.get', return_value=mock_check_resp), \
             patch('business_app.services.tax_committee_service.requests.post', return_value=mock_refresh_resp):
            token = service._ensure_valid_token()

        assert token == 'refreshed-token'

    def test_refresh_token_http_failure_dispatches_alert(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        mock_resp = Mock()
        mock_resp.status_code = 401
        mock_resp.text = 'Unauthorized'
        with patch('business_app.services.tax_committee_service.requests.post', return_value=mock_resp), \
             patch('business_app.tasks.notification_tasks.send_tax_committee_token_refresh_alert_task') as mock_task:
            with pytest.raises(ValidationError, match='Failed to refresh'):
                service.refresh_token('bad-old-token')
        mock_task.delay.assert_called_once_with('http_error', 401, 'Unauthorized')

    def test_refresh_token_empty_token_dispatches_alert(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        with patch('business_app.services.tax_committee_service.requests.post', return_value=mock_resp), \
             patch('business_app.tasks.notification_tasks.send_tax_committee_token_refresh_alert_task') as mock_task:
            with pytest.raises(ValidationError, match='empty token'):
                service.refresh_token('old-token')
        mock_task.delay.assert_called_once_with('empty_token', None, None)

    def test_refresh_token_alert_dispatch_failure_does_not_suppress_validation_error(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        mock_resp = Mock()
        mock_resp.status_code = 500
        mock_resp.text = 'Server Error'
        with patch('business_app.services.tax_committee_service.requests.post', return_value=mock_resp), \
             patch('business_app.tasks.notification_tasks.send_tax_committee_token_refresh_alert_task') as mock_task:
            mock_task.delay.side_effect = RuntimeError('broker down')
            # The refresh failure's ValidationError must still surface, not the dispatch error.
            with pytest.raises(ValidationError, match='Failed to refresh'):
                service.refresh_token('old-token')


@pytest.mark.unit
class TestTaxCommitteeUtilisation:
    def test_utilise_marking_codes_success(self, app, db, sample_product):
        _configure_tax_committee_context(app)
        sample_product.expire_days = 180
        db.session.flush()

        service = TaxCommitteeService()

        mock_check_resp = Mock()
        mock_check_resp.status_code = 200
        mock_check_resp.json.return_value = {'isTinCorrect': True, 'expiresOn': '2026-07-05T00:00:00Z'}

        mock_utilise_resp = Mock()
        mock_utilise_resp.status_code = 200
        mock_utilise_resp.json.return_value = {'reportId': 'RPT-123'}

        with patch('business_app.services.tax_committee_service.requests.get', return_value=mock_check_resp), \
             patch('business_app.services.tax_committee_service.requests.post', return_value=mock_utilise_resp) as mock_post:
            # Seed token first
            service._seed_token_from_config()
            result = service.utilise_marking_codes(['CODE-1\x1dVERIFY-1', 'CODE-2\x1dVERIFY-2'], sample_product)

        assert result['reportId'] == 'RPT-123'
        # Verify the POST body
        call_kwargs = mock_post.call_args
        body = call_kwargs.kwargs.get('json') or call_kwargs[1].get('json')
        assert body['sntins'] == ['CODE-1\x1dVERIFY-1', 'CODE-2\x1dVERIFY-2']
        assert body['businessPlaceId'] == 101380
        assert body['releaseType'] == 'PRODUCTION'
        assert body['manufacturerCountry'] == 'UZ'

    def test_utilise_marking_codes_empty_list_skips(self, app, db, sample_product):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        result = service.utilise_marking_codes([], sample_product)
        assert result.get('skipped') is True

    def test_utilise_marking_codes_api_error(self, app, db, sample_product):
        _configure_tax_committee_context(app)
        sample_product.expire_days = 90
        db.session.flush()

        service = TaxCommitteeService()
        db.session.add(TaxCommitteeApiToken(token='valid-token', is_active=True))
        db.session.flush()

        mock_check_resp = Mock()
        mock_check_resp.status_code = 200
        mock_check_resp.json.return_value = {'isTinCorrect': True, 'expiresOn': '2026-07-05T00:00:00Z'}

        mock_utilise_resp = Mock()
        mock_utilise_resp.status_code = 500
        mock_utilise_resp.text = 'Internal Server Error'

        with patch('business_app.services.tax_committee_service.requests.get', return_value=mock_check_resp), \
             patch('business_app.services.tax_committee_service.requests.post', return_value=mock_utilise_resp):
            with pytest.raises(ValidationError, match='Tax Committee utilisation failed'):
                service.utilise_marking_codes(['CODE-1'], sample_product)

    def test_utilise_uses_default_expire_days_when_null(self, app, db, sample_product):
        _configure_tax_committee_context(app)
        sample_product.expire_days = None
        db.session.flush()

        service = TaxCommitteeService()
        db.session.add(TaxCommitteeApiToken(token='valid-token', is_active=True))
        db.session.flush()

        mock_check_resp = Mock()
        mock_check_resp.status_code = 200
        mock_check_resp.json.return_value = {'isTinCorrect': True, 'expiresOn': '2026-07-05T00:00:00Z'}

        mock_utilise_resp = Mock()
        mock_utilise_resp.status_code = 200
        mock_utilise_resp.json.return_value = {'reportId': 'RPT-DEFAULT'}

        with patch('business_app.services.tax_committee_service.requests.get', return_value=mock_check_resp), \
             patch('business_app.services.tax_committee_service.requests.post', return_value=mock_utilise_resp) as mock_post:
            result = service.utilise_marking_codes(['CODE-1'], sample_product)

        assert result['reportId'] == 'RPT-DEFAULT'
        body = (mock_post.call_args.kwargs.get('json') or mock_post.call_args[1].get('json'))
        # With default 180 days, expirationDate should be ~180 days from productionDate
        assert 'expirationDate' in body


@pytest.mark.unit
class TestTaxCommitteeStatusCheck:
    def test_check_marking_code_statuses_success(self, app, db, sample_product):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        db.session.add(TaxCommitteeApiToken(token='valid-token', is_active=True))
        db.session.flush()

        mock_check_resp = Mock()
        mock_check_resp.status_code = 200
        mock_check_resp.json.return_value = {'isTinCorrect': True, 'expiresOn': '2026-07-05T00:00:00Z'}

        mock_status_resp = Mock()
        mock_status_resp.status_code = 200
        mock_status_resp.json.return_value = {
            'results': [
                {'codeData': {'code': 'CODE-001', 'status': 'RECEIVED'}},
                {'codeData': {'code': 'CODE-002', 'status': 'APPLIED'}},
                {'codeData': {'code': 'CODE-003', 'status': 'INTRODUCED'}},
            ]
        }

        with patch('business_app.services.tax_committee_service.requests.get', return_value=mock_check_resp), \
             patch('business_app.services.tax_committee_service.requests.post', return_value=mock_status_resp) as mock_post:
            result = service.check_marking_code_statuses(['CODE-001', 'CODE-002', 'CODE-003'])

        assert result == {'CODE-001': 'RECEIVED', 'CODE-002': 'APPLIED', 'CODE-003': 'INTRODUCED'}
        call_kwargs = mock_post.call_args
        body = call_kwargs.kwargs.get('json') or call_kwargs[1].get('json')
        assert body == {'codes': ['CODE-001', 'CODE-002', 'CODE-003']}

    def test_check_marking_code_statuses_empty_list(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        result = service.check_marking_code_statuses([])
        assert result == {}

    def test_check_marking_code_statuses_api_error(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        db.session.add(TaxCommitteeApiToken(token='valid-token', is_active=True))
        db.session.flush()

        mock_check_resp = Mock()
        mock_check_resp.status_code = 200
        mock_check_resp.json.return_value = {'isTinCorrect': True, 'expiresOn': '2026-07-05T00:00:00Z'}

        mock_status_resp = Mock()
        mock_status_resp.status_code = 500
        mock_status_resp.text = 'Internal Server Error'

        with patch('business_app.services.tax_committee_service.requests.get', return_value=mock_check_resp), \
             patch('business_app.services.tax_committee_service.requests.post', return_value=mock_status_resp):
            with pytest.raises(ValidationError, match='Tax Committee status check failed'):
                service.check_marking_code_statuses(['CODE-001'])

    def test_check_returns_withdrawn_and_written_off(self, app, db):
        _configure_tax_committee_context(app)
        service = TaxCommitteeService()
        db.session.add(TaxCommitteeApiToken(token='valid-token', is_active=True))
        db.session.flush()

        mock_check_resp = Mock()
        mock_check_resp.status_code = 200
        mock_check_resp.json.return_value = {'isTinCorrect': True, 'expiresOn': '2026-07-05T00:00:00Z'}

        mock_status_resp = Mock()
        mock_status_resp.status_code = 200
        mock_status_resp.json.return_value = {
            'results': [
                {'codeData': {'code': 'CODE-A', 'status': 'WITHDRAWN'}},
                {'codeData': {'code': 'CODE-B', 'status': 'WRITTEN_OFF'}},
            ]
        }

        with patch('business_app.services.tax_committee_service.requests.get', return_value=mock_check_resp), \
             patch('business_app.services.tax_committee_service.requests.post', return_value=mock_status_resp):
            result = service.check_marking_code_statuses(['CODE-A', 'CODE-B'])

        assert result == {'CODE-A': 'WITHDRAWN', 'CODE-B': 'WRITTEN_OFF'}

    def test_status_class_constants(self):
        """Verify the class-level status constants are correct."""
        assert TaxCommitteeService.ALREADY_UTILISED_STATUSES == {'APPLIED', 'INTRODUCED'}
        assert TaxCommitteeService.INVALID_STATUSES == {'WITHDRAWN', 'WRITTEN_OFF'}
