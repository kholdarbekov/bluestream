"""Unit tests for security headers middleware and CSP helpers."""

import pytest
from flask import Flask, jsonify

from business_app.utils.security_headers import (
    CSPBuilder,
    SecurityHeadersConfig,
    SecurityHeadersMiddleware,
    configure_csp_sources,
    get_current_csp_policy,
    setup_csp_reporting,
    setup_security_headers,
)


@pytest.mark.unit
class TestSecurityHeadersConfigAndBuilder:
    def test_security_headers_config_by_environment(self):
        prod = SecurityHeadersConfig("production")
        assert prod.x_frame_options == "DENY"
        assert "max-age=63072000" in prod.strict_transport_security

        staging = SecurityHeadersConfig("staging")
        assert staging.x_frame_options == "SAMEORIGIN"
        assert "max-age=31536000" in staging.strict_transport_security

        dev = SecurityHeadersConfig("development")
        assert dev.strict_transport_security is None
        assert "geolocation=(self)" in dev.permissions_policy

    def test_csp_builder_operations(self):
        builder = CSPBuilder("development")
        initial = builder.build()
        assert "default-src" in initial

        builder.add_source("script-src", "https://cdn.example.com")
        builder.remove_source("script-src", "localhost:*")
        builder.set_directive("img-src", ["'self'", "data:"])

        policy = builder.build()
        assert "https://cdn.example.com" in policy
        assert "img-src 'self' data:" in policy

        report_policy = builder.build_report_only("/csp-report")
        assert "report-uri /csp-report" in report_policy


@pytest.mark.unit
class TestSecurityHeadersMiddleware:
    @pytest.fixture
    def app(self):
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["FLASK_ENV"] = "development"

        @app.get("/api/demo")
        def api_demo():
            return jsonify({"ok": True})

        @app.get("/admin/panel")
        def admin_panel():
            return jsonify({"ok": True})

        @app.get("/web")
        def web_page():
            return jsonify({"ok": True})

        return app

    def test_should_skip_headers(self, app):
        middleware = SecurityHeadersMiddleware(app)

        class _Resp:
            headers = {"Content-Type": "application/json"}

        class _ImgResp:
            headers = {"Content-Type": "image/png"}

        assert middleware._should_skip_headers("/static/logo.png", _Resp()) is True
        assert middleware._should_skip_headers("/api/demo", _ImgResp()) is True
        assert middleware._should_skip_headers("/api/demo", _Resp()) is False

    def test_get_csp_for_request_variants(self, app):
        from flask import request

        middleware = SecurityHeadersMiddleware(app)

        with app.test_request_context("/api/demo"):
            assert "default-src 'none'" in middleware._get_csp_for_request(request)

        with app.test_request_context("/health"):
            assert "default-src 'none'" in middleware._get_csp_for_request(request)

        with app.test_request_context("/admin/panel"):
            admin_policy = middleware._get_csp_for_request(request)
            assert "frame-ancestors 'none'" in admin_policy

        with app.test_request_context("/web"):
            web_policy = middleware._get_csp_for_request(request)
            assert "default-src" in web_policy

    def test_middleware_applies_headers_to_responses(self, app):
        SecurityHeadersMiddleware(app)
        client = app.test_client()

        api_response = client.get("/api/demo")
        assert api_response.status_code == 200
        assert api_response.headers["X-Content-Type-Options"] == "nosniff"
        assert api_response.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert "Content-Security-Policy" in api_response.headers

        web_response = client.get("/web")
        assert web_response.status_code == 200
        assert "Permissions-Policy" in web_response.headers

    def test_setup_helpers_and_reporting_endpoint(self, app):
        middleware = setup_security_headers(app)
        assert hasattr(app, "security_headers")
        assert middleware is app.security_headers

        configure_csp_sources(app, {"script-src": ["https://cdn.example.com"]})
        assert "https://cdn.example.com" in get_current_csp_policy(app)

        app2 = Flask(__name__)
        with pytest.raises(RuntimeError):
            configure_csp_sources(app2, {"img-src": ["data:"]})

        setup_csp_reporting(app)
        client = app.test_client()

        ok = client.post("/csp-report", json={"csp-report": {"violated-directive": "script-src"}})
        assert ok.status_code == 204

        bad = client.post("/csp-report", data="not-json", content_type="application/json")
        assert bad.status_code == 400
