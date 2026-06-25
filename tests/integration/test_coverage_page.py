"""Public /coverage page: renders, structured data, and agent discoverability."""
import pytest


@pytest.fixture
def client(app):
    """Fresh, function-scoped test client for the coverage-page assertions.

    These tests assert *absolute* frontend behavior (``GET /coverage`` → 200,
    Markdown content-negotiation). The default ``client`` fixture is
    session-scoped, so a ``session['language']`` left by an earlier test in the
    same xdist worker leaks in; the app's language ``before_request`` then
    302-redirects ``/coverage`` to ``?lang=<non-default>`` and these asserts
    fail order-dependently. A fresh client carries no leaked session cookie, so
    the tests are deterministic regardless of suite ordering. (See the
    session-scoped-client cookie-leak gotcha in the project test-suite notes.)
    """
    return app.test_client()


@pytest.mark.integration
@pytest.mark.api
class TestCoveragePage:
    def test_renders_with_map_districts_and_jsonld(self, client, db):
        r = client.get("/coverage")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert 'id="coverage-map"' in html
        assert 'id="coverage-data"' in html        # inline JSON for map + checker + JSON-LD
        assert "Bektemir" in html                   # a covered district (same name in en/uz, so language-independent)
        assert '"@type": "GeoShape"' in html or '"@type":"GeoShape"' in html
        assert "FAQPage" in html

    def test_in_sitemap_static(self, client, db):
        r = client.get("/sitemap-static.xml")
        assert r.status_code == 200
        assert "/coverage" in r.get_data(as_text=True)

    def test_served_as_markdown_to_agents(self, client, db):
        r = client.get("/coverage", headers={"Accept": "text/markdown"})
        assert r.status_code == 200
        assert "markdown" in r.headers.get("Content-Type", "")
