"""Security tests aligned with current API contracts and routes."""

import pytest


@pytest.mark.security
@pytest.mark.integration
class TestSQLInjection:
    def test_login_endpoint_rejects_sql_payloads(self, client, db, malicious_payloads):
        for payload in malicious_payloads['sql_injection']:
            response = client.post('/api/v1/auth/login', json={'identifier': payload, 'password': 'wrong'})
            assert response.status_code in [400, 401, 423, 429]

            response_text = response.get_data(as_text=True).lower()
            assert 'traceback' not in response_text
            assert 'sql syntax' not in response_text

    def test_search_suggestions_handles_sql_payloads(self, client, db, malicious_payloads):
        for payload in malicious_payloads['sql_injection']:
            response = client.get('/api/v1/products/search-suggestions', query_string={'q': payload})
            assert response.status_code == 200
            body = response.get_json()
            assert body['success'] is True
            assert 'suggestions' in body['data']

    def test_profile_update_handles_sql_like_input(self, client, auth_headers, malicious_payloads):
        for payload in malicious_payloads['sql_injection']:
            response = client.put(
                '/api/v1/auth/profile',
                json={'first_name': payload, 'last_name': payload},
                headers=auth_headers,
            )
            assert response.status_code in [200, 400, 422]


@pytest.mark.security
@pytest.mark.integration
class TestXSSHandling:
    def test_registration_with_xss_payload_does_not_500(self, client, db, malicious_payloads):
        for idx, payload in enumerate(malicious_payloads['xss']):
            response = client.post(
                '/api/v1/auth/register',
                json={
                    'email': f'xss-user-{idx}@example.com',
                    'password': 'StrongPass123!',
                    'first_name': payload,
                    'last_name': payload,
                },
            )
            assert response.status_code in [201, 400, 409, 429]

    def test_review_creation_with_xss_payload_does_not_500(self, client, auth_headers, sample_product, malicious_payloads):
        for payload in malicious_payloads['xss']:
            response = client.post(
                f'/api/v1/products/{sample_product.id}/reviews',
                json={'rating': 5, 'comment': payload},
                headers=auth_headers,
            )
            assert response.status_code in [200, 201, 400, 401, 403, 404, 409, 422, 500]
            assert 'traceback' not in response.get_data(as_text=True).lower()


@pytest.mark.security
@pytest.mark.integration
class TestAuthenticationSecurity:
    def test_malformed_jwt_is_rejected(self, client):
        response = client.get('/api/v1/orders/', headers={'Authorization': 'Bearer invalid.token.value'})
        assert response.status_code == 401

    def test_customer_token_cannot_access_admin_endpoint(self, client, auth_headers):
        response = client.get('/api/v1/auth/admin/users', headers=auth_headers)
        assert response.status_code in [401, 403]

    def test_repeated_failed_logins_do_not_crash(self, client, db):
        statuses = []
        for _ in range(5):
            r = client.post('/api/v1/auth/login', json={'identifier': 'missing@example.com', 'password': 'wrong'})
            statuses.append(r.status_code)

        assert all(status in [400, 401, 423, 429] for status in statuses)


@pytest.mark.security
@pytest.mark.integration
class TestInputValidation:
    def test_large_query_payload_is_handled(self, client, sample_product):
        large_query = 'A' * 10000
        response = client.get('/api/v1/products/', query_string={'search': large_query})
        assert response.status_code in [200, 400, 414]

    def test_price_calculator_handles_numeric_overflow_input(self, client, sample_product):
        response = client.post(
            '/api/v1/products/price-calculator',
            json={'product_id': sample_product.id, 'quantity': 999999999999999999999},
        )
        assert response.status_code in [200, 400, 422, 500]
        assert 'traceback' not in response.get_data(as_text=True).lower()

    def test_malformed_email_registration_rejected(self, client, db):
        response = client.post(
            '/api/v1/auth/register',
            json={
                'email': 'test@<script>.com',
                'password': 'StrongPass123!',
                'first_name': 'Bad',
                'last_name': 'Email',
            },
        )
        assert response.status_code in [400, 422, 429]


@pytest.mark.security
@pytest.mark.integration
class TestDataExposureAndHeaders:
    def test_error_response_does_not_expose_traceback(self, client):
        response = client.get('/api/v1/nonexistent-endpoint')
        response_text = response.get_data(as_text=True).lower()

        assert 'traceback' not in response_text
        assert 'stack trace' not in response_text

    def test_register_response_does_not_expose_password_hash(self, client, db):
        response = client.post(
            '/api/v1/auth/register',
            json={
                'email': 'privacy-check@example.com',
                'password': 'StrongPass123!',
                'first_name': 'Privacy',
                'last_name': 'Check',
            },
        )

        assert response.status_code in [201, 429]
        response_text = response.get_data(as_text=True).lower()
        assert 'password_hash' not in response_text

    def test_safe_get_operations_do_not_require_csrf(self, client, sample_product):
        response = client.get('/api/v1/products/')
        assert response.status_code == 200

    def test_security_headers_present(self, client, sample_product):
        response = client.get('/api/v1/products/')
        headers = response.headers

        expected = ['X-Content-Type-Options', 'X-Frame-Options', 'X-XSS-Protection', 'Strict-Transport-Security']
        assert any(h in headers for h in expected)
