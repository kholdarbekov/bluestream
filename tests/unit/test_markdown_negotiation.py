"""Tests for Markdown-for-Agents content negotiation."""

import pytest
from flask import Response

from business_app.utils.markdown_negotiation import (
    estimate_tokens,
    html_to_markdown,
    wants_markdown,
)


@pytest.mark.unit
class TestWantsMarkdown:
    def test_explicit_text_markdown(self):
        assert wants_markdown("text/markdown") is True

    def test_text_markdown_with_positive_q(self):
        assert wants_markdown("text/markdown;q=0.9") is True

    def test_text_markdown_among_others(self):
        assert wants_markdown("text/html, text/markdown;q=0.8, */*;q=0.1") is True

    def test_text_markdown_q_zero(self):
        assert wants_markdown("text/markdown;q=0") is False

    def test_browser_default_is_html(self):
        assert wants_markdown("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8") is False

    def test_wildcard_does_not_match(self):
        assert wants_markdown("*/*") is False

    def test_text_wildcard_does_not_match(self):
        assert wants_markdown("text/*") is False

    def test_empty_and_none(self):
        assert wants_markdown("") is False
        assert wants_markdown(None) is False

    def test_malformed_entry_ignored(self):
        assert wants_markdown("text/markdown;q=notanumber") is False


@pytest.mark.unit
class TestEstimateTokens:
    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0

    def test_rounds_up_and_minimum_one(self):
        assert estimate_tokens("a") == 1
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("abcde") == 2  # ceil(5/4)


@pytest.mark.unit
class TestHtmlToMarkdown:
    def _page(self):
        return """
        <html>
          <head>
            <title>Fallback Title</title>
            <meta name="title" content="Aqua Element Water">
            <meta name="description" content="Pure water delivery">
            <meta property="og:title" content="OG Title">
            <meta property="og:image" content="https://aqua-element.uz/og.png">
            <script type="application/ld+json">{"@type": "Organization", "name": "Aqua"}</script>
          </head>
          <body>
            <header><nav>Home Shop Login</nav></header>
            <main>
              <h1>About Us</h1>
              <p>We deliver <a href="/shop">bottled water</a>.</p>
              <ul><li>Fast</li><li>Clean</li></ul>
              <script>console.log('tracking')</script>
            </main>
            <footer>Copyright</footer>
          </body>
        </html>
        """

    def test_frontmatter_meta_name_wins_over_og(self):
        md = html_to_markdown(self._page())
        assert md.startswith("---\n")
        assert 'title: "Aqua Element Water"' in md  # <meta name> beats og:title
        assert 'description: "Pure water delivery"' in md
        assert 'image: "https://aqua-element.uz/og.png"' in md

    def test_title_falls_back_to_title_tag(self):
        html = "<html><head><title>Just Title</title></head><body><main><p>Hi</p></main></body></html>"
        md = html_to_markdown(html)
        assert 'title: "Just Title"' in md

    def test_frontmatter_omits_absent_keys(self):
        html = "<html><head><title>T</title></head><body><main><p>Hi</p></main></body></html>"
        md = html_to_markdown(html)
        assert "description:" not in md
        assert "image:" not in md

    def test_body_converted_and_non_content_stripped(self):
        md = html_to_markdown(self._page())
        assert "# About Us" in md  # ATX heading
        assert "[bottled water](/shop)" in md
        assert "- Fast" in md
        # nav / footer / script must be gone
        assert "Home Shop Login" not in md
        assert "Copyright" not in md
        assert "console.log" not in md

    def test_json_ld_preserved_as_fenced_block(self):
        md = html_to_markdown(self._page())
        assert "```json" in md
        assert '"@type": "Organization"' in md

    def test_ordering_frontmatter_body_then_jsonld(self):
        md = html_to_markdown(self._page())
        assert md.index("---") < md.index("# About Us") < md.index("```json")

    def test_empty_html_does_not_raise(self):
        assert isinstance(html_to_markdown(""), str)

    def test_prefers_main_then_article_then_body(self):
        html = "<html><body><article><h2>Art</h2></article><div>noise</div></body></html>"
        md = html_to_markdown(html)
        assert "## Art" in md
        assert "noise" not in md

    def test_falls_back_to_body_when_no_main_or_article(self):
        html = "<html><head><title>T</title></head><body><h3>Body Heading</h3><p>Para</p></body></html>"
        md = html_to_markdown(html)
        assert "### Body Heading" in md
        assert "Para" in md

    def test_strips_preloader_chrome_on_body_fallback(self):
        html = (
            "<html><body>"
            '<div class="loader-wrap"><div class="preloader">LOADERNOISE</div></div>'
            "<h1>Real Content</h1>"
            "</body></html>"
        )
        md = html_to_markdown(html)
        assert "# Real Content" in md
        assert "LOADERNOISE" not in md


@pytest.mark.unit
class TestConvertHookDirect:
    """Exercise the after_request hook directly in a request context."""

    def _hook(self):
        from business_app.frontend.routes import convert_to_markdown_if_requested

        return convert_to_markdown_if_requested

    def _html_response(self):
        return Response(
            "<html><head><title>About</title></head><body><main><h1>About</h1></main></body></html>",
            status=200,
            mimetype="text/html",
        )

    def test_converts_when_requested_on_allowlisted_endpoint(self, app):
        with app.test_request_context("/about", headers={"Accept": "text/markdown"}):
            out = self._hook()(self._html_response())
        assert out.headers["Content-Type"] == "text/markdown; charset=utf-8"
        assert int(out.headers["x-markdown-tokens"]) >= 1
        assert "Accept" in out.headers["Vary"]
        assert "# About" in out.get_data(as_text=True)

    def test_untouched_without_accept_header(self, app):
        with app.test_request_context("/about"):
            out = self._hook()(self._html_response())
        assert out.mimetype == "text/html"
        assert "x-markdown-tokens" not in out.headers

    def test_untouched_on_non_allowlisted_endpoint(self, app):
        with app.test_request_context("/cart", headers={"Accept": "text/markdown"}):
            out = self._hook()(self._html_response())
        assert out.mimetype == "text/html"

    def test_untouched_on_cabinet_host(self, app):
        with app.test_request_context(
            "/about", base_url="http://cabinet.aqua-element.uz", headers={"Accept": "text/markdown"}
        ):
            out = self._hook()(self._html_response())
        assert out.mimetype == "text/html"

    def test_untouched_on_redirect(self, app):
        with app.test_request_context("/about", headers={"Accept": "text/markdown"}):
            out = self._hook()(Response("", status=302, mimetype="text/html"))
        assert out.mimetype == "text/html"


@pytest.mark.unit
class TestMarkdownNegotiationIntegration:
    def test_about_returns_markdown_for_agent(self, client, db):
        resp = client.get("/about", headers={"Accept": "text/markdown"})
        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "text/markdown; charset=utf-8"
        assert "x-markdown-tokens" in resp.headers
        assert int(resp.headers["x-markdown-tokens"]) >= 1
        vary = resp.headers.get("Vary", "")
        assert "Accept" in vary
        assert "Cookie" in vary and "Accept-Language" in vary

    def test_about_returns_html_for_browser(self, client, db):
        resp = client.get(
            "/about",
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        )
        assert resp.status_code == 200
        assert resp.mimetype == "text/html"
        assert "x-markdown-tokens" not in resp.headers

    def test_cart_stays_html_even_for_agent(self, client, db):
        # /cart is interactive and not in the allowlist.
        resp = client.get("/cart", headers={"Accept": "text/markdown"})
        assert resp.status_code == 200
        assert resp.mimetype == "text/html"

    def test_link_discovery_header_present_on_markdown(self, client, db):
        resp = client.get("/about", headers={"Accept": "text/markdown"})
        assert 'rel="api-catalog"' in resp.headers.get("Link", "")


@pytest.mark.unit
class TestMarkdownAuthGuard:
    """Personalized (authenticated) responses must never be converted to Markdown."""

    def test_authenticated_request_gets_html_not_markdown(self, app, db, sample_user):
        from flask_jwt_extended import create_access_token

        # Default-language user avoids the before_request language redirect so the
        # homepage returns 200 directly.
        sample_user.preferred_language = app.config["DEFAULT_LANGUAGE"]
        db.session.commit()
        with app.app_context():
            token = create_access_token(identity=str(sample_user.id))
        c = app.test_client()
        c.set_cookie("access_token_cookie", token)

        resp = c.get("/", headers={"Accept": "text/markdown"})
        assert resp.status_code == 200
        assert resp.mimetype == "text/html"  # personalized -> NOT converted
        assert "x-markdown-tokens" not in resp.headers

    def test_anonymous_request_to_homepage_gets_markdown(self, client, db):
        resp = client.get("/", headers={"Accept": "text/markdown"})
        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "text/markdown; charset=utf-8"
        assert "x-markdown-tokens" in resp.headers
