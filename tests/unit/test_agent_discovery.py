"""Tests for RFC 8288 Link headers + RFC 9727 api-catalog agent discovery."""

import json
from pathlib import Path

import pytest
from flask import Response

from business_app.utils.agent_discovery import (
    PUBLIC_DISCOVERY_LINKS,
    build_api_catalog_linkset,
    build_link_header,
)

# Resources that must NEVER be advertised to agents — they cover privileged
# admin endpoints (full OpenAPI spec / Swagger UI / admin API).
FORBIDDEN_TOKENS = ("/apispec_1.json", "/docs", "/api/admin", "swagger", "X-Admin-Token")

# Allowlist: the COMPLETE, intended public discovery surface. This is the
# security boundary the project owner set (advertise only public resources).
# Any change to PUBLIC_DISCOVERY_LINKS must consciously update this set, so a
# new admin/private path can never be advertised by accident.
EXPECTED_PUBLIC_SURFACE = {
    ("api-catalog", "/.well-known/api-catalog"),
    ("service-desc", "/api/public/products.json"),
    ("service-desc", "/api/public/check-delivery"),
    ("service-doc", "/llms.txt"),
}


@pytest.mark.unit
def test_public_surface_is_exactly_the_allowlist():
    assert {(rel, path) for rel, path, _type, _title in PUBLIC_DISCOVERY_LINKS} == EXPECTED_PUBLIC_SURFACE


@pytest.mark.unit
class TestBuildLinkHeader:
    def test_advertises_registered_public_relations(self):
        header = build_link_header()
        # All three IANA-registered relation types are present...
        assert 'rel="api-catalog"' in header
        assert 'rel="service-desc"' in header
        assert 'rel="service-doc"' in header
        # ...pointing at the public, root-relative discovery resources.
        assert "</.well-known/api-catalog>" in header
        assert "</api/public/products.json>" in header
        assert "</llms.txt>" in header
        # Comma-separated link-values with media-type hints (RFC 8288).
        assert header.count(",") == len(PUBLIC_DISCOVERY_LINKS) - 1
        assert 'type="application/linkset+json"' in header

    def test_does_not_leak_admin_surface(self):
        header = build_link_header()
        for token in FORBIDDEN_TOKENS:
            assert token not in header


@pytest.mark.unit
class TestBuildApiCatalogLinkset:
    def test_linkset_structure_and_absolute_hrefs(self):
        catalog = build_api_catalog_linkset(lambda p: f"https://aqua-element.uz{p}")
        assert list(catalog.keys()) == ["linkset"]
        context = catalog["linkset"][0]
        # Exactly the anchor + the two public relations; api-catalog self
        # relation is omitted from the body (this *is* it), nothing extra leaks.
        assert set(context.keys()) == {"anchor", "service-desc", "service-doc"}
        assert context["anchor"] == "https://aqua-element.uz/"
        assert len(context["service-desc"]) == 2
        desc_hrefs = {d["href"] for d in context["service-desc"]}
        assert "https://aqua-element.uz/api/public/products.json" in desc_hrefs
        assert "https://aqua-element.uz/api/public/check-delivery" in desc_hrefs
        assert len(context["service-doc"]) == 1
        assert context["service-doc"][0]["href"] == "https://aqua-element.uz/llms.txt"

    def test_does_not_leak_admin_surface(self):
        blob = json.dumps(build_api_catalog_linkset(lambda p: f"https://x{p}"))
        for token in FORBIDDEN_TOKENS:
            assert token not in blob


@pytest.mark.unit
class TestLinkHeaderAfterRequest:
    """The hook inspects both the response and the request host; exercise it in a request context."""

    def _hook(self):
        from business_app.frontend.routes import add_agent_discovery_link_header

        return add_agent_discovery_link_header

    def test_added_on_200_html(self, app):
        with app.test_request_context("/"):
            out = self._hook()(Response("<html></html>", status=200, mimetype="text/html"))
        assert 'rel="api-catalog"' in out.headers["Link"]

    def test_skipped_on_non_html(self, app):
        with app.test_request_context("/"):
            out = self._hook()(Response("{}", status=200, mimetype="application/json"))
        assert "Link" not in out.headers

    def test_skipped_on_redirect(self, app):
        with app.test_request_context("/"):
            out = self._hook()(Response("", status=302, mimetype="text/html"))
        assert "Link" not in out.headers

    def test_skipped_on_cabinet_host(self, app):
        with app.test_request_context("/", base_url="http://cabinet.aqua-element.uz"):
            out = self._hook()(Response("<html></html>", status=200, mimetype="text/html"))
        assert "Link" not in out.headers

    def test_appends_to_existing_link_header(self, app):
        resp = Response("<html></html>", status=200, mimetype="text/html")
        resp.headers["Link"] = '</style.css>; rel="preload"'
        with app.test_request_context("/"):
            out = self._hook()(resp)
        assert '</style.css>; rel="preload"' in out.headers["Link"]
        assert 'rel="api-catalog"' in out.headers["Link"]


@pytest.mark.unit
class TestApiCatalogEndpoint:
    def test_serves_linkset_document(self, client):
        resp = client.get("/.well-known/api-catalog")
        assert resp.status_code == 200
        assert resp.mimetype == "application/linkset+json"
        assert resp.headers["Access-Control-Allow-Origin"] == "*"

        data = json.loads(resp.get_data(as_text=True))
        context = data["linkset"][0]
        assert context["anchor"].endswith("/")
        assert context["service-desc"][0]["href"].endswith("/api/public/products.json")
        assert context["service-doc"][0]["href"].endswith("/llms.txt")

    def test_endpoint_does_not_leak_admin_surface(self, client):
        blob = client.get("/.well-known/api-catalog").get_data(as_text=True)
        for token in FORBIDDEN_TOKENS:
            assert token not in blob


@pytest.mark.unit
class TestStorefrontLinkHeader:
    def test_homepage_carries_link_header(self, client, db):
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.mimetype == "text/html"
        assert 'rel="api-catalog"' in resp.headers["Link"]
        assert "</.well-known/api-catalog>" in resp.headers["Link"]

    def test_secondary_storefront_page_carries_link_header(self, client, db):
        resp = client.get("/about")
        assert resp.status_code == 200
        assert resp.mimetype == "text/html"
        assert 'rel="api-catalog"' in resp.headers["Link"]

    def test_cabinet_subdomain_does_not_advertise(self, client, db):
        # Authenticated cabinet pages share this blueprint but are not public.
        resp = client.get("/", headers={"Host": "cabinet.aqua-element.uz"})
        assert resp.status_code == 200
        assert "Link" not in resp.headers

    def test_unknown_path_has_no_link_header(self, client):
        resp = client.get("/this-path-does-not-exist-xyz")
        assert resp.status_code == 404
        assert "Link" not in resp.headers


@pytest.mark.unit
class TestAdvertisedLinksResolve:
    """Every advertised href must actually resolve (catches a route rename/desync)."""

    def test_each_advertised_path_is_reachable(self, client, db):
        for _rel, path, _type, _title in PUBLIC_DISCOVERY_LINKS:
            resp = client.get(path)
            assert resp.status_code == 200, f"advertised {path} returned {resp.status_code}"


@pytest.mark.unit
class TestPublicApiFeedHeaders:
    """The advertised /api/public/ feed is carved out of the private /api/ no-store+noindex defaults."""

    def test_products_feed_is_cacheable_and_indexable(self, client, db):
        resp = client.get("/api/public/products.json")
        assert resp.status_code == 200
        cache_control = resp.headers.get("Cache-Control", "")
        assert "public" in cache_control and "max-age=900" in cache_control
        assert "no-store" not in cache_control
        assert "noindex" not in resp.headers.get("X-Robots-Tag", "")


@pytest.mark.unit
def test_robots_allows_public_api_feed():
    """robots.txt must Allow /api/public/ in every block that Disallows /api/."""
    import business_app

    robots = Path(business_app.__file__).parent / "static" / "robots.txt"
    text = robots.read_text(encoding="utf-8")
    assert "Allow: /api/public/" in text
    assert text.count("Allow: /api/public/") == text.count("Disallow: /api/")


@pytest.mark.unit
class TestCacheableResponseCookieGuard:
    """A publicly cacheable response must never carry a refreshed per-user auth cookie."""

    def _client_with_near_expiry_token(self, app, db, sample_user):
        from datetime import timedelta

        from flask_jwt_extended import create_access_token

        # A default-language user avoids the before_request language redirect,
        # while the JWT is still verified — so the auto-refresh path is genuinely
        # exercised (a no-cookie result then proves the guard, not a missing JWT).
        sample_user.preferred_language = app.config["DEFAULT_LANGUAGE"]
        db.session.commit()
        with app.app_context():
            # Valid now, but within the 30-min auto-refresh window so the
            # app-level after_request will try to re-issue the cookie.
            token = create_access_token(identity=str(sample_user.id), expires_delta=timedelta(minutes=10))
        fresh = app.test_client()
        fresh.set_cookie("access_token_cookie", token)
        return fresh

    @staticmethod
    def _sets_access_cookie(resp):
        return any("access_token_cookie=" in value for value in resp.headers.get_all("Set-Cookie"))

    def test_refresh_fires_on_non_cacheable_page(self, app, db, sample_user):
        # Control: the homepage is no-store, so the JWT auto-refresh SHOULD run.
        resp = self._client_with_near_expiry_token(app, db, sample_user).get("/")
        assert resp.status_code == 200
        assert self._sets_access_cookie(resp)

    def test_no_auth_cookie_on_public_cacheable_catalog(self, app, db, sample_user):
        # Guard: the catalog is Cache-Control: public, so no auth cookie may ride it.
        resp = self._client_with_near_expiry_token(app, db, sample_user).get("/.well-known/api-catalog")
        assert resp.status_code == 200
        assert "public" in resp.headers.get("Cache-Control", "")
        assert not self._sets_access_cookie(resp)
