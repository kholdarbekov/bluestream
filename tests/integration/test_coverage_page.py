"""Public /coverage page: renders, structured data, and agent discoverability."""
import pytest


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
