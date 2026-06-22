"""Agent-discovery helpers (RFC 8288 ``Link`` headers + RFC 9727 api-catalog).

Advertises the *public*, machine-readable surface of the storefront to AI
agents and crawlers so they can discover how to consume it:

* a ``Link`` response header on storefront HTML pages (RFC 8288), and
* a ``/.well-known/api-catalog`` linkset document (RFC 9727 §3, serialised as
  the RFC 9264 ``application/linkset+json`` format).

Only public resources are exposed here. The full OpenAPI spec
(``/apispec_1.json``) and Swagger UI (``/docs``) cover privileged admin
endpoints, so they are deliberately **excluded** — the catalog points instead
at the public product feed and the ``llms.txt`` index.
"""

# Single source of truth shared by the Link header and the api-catalog
# linkset. Each entry is (rel, root-relative path, media type, human title).
# IANA-registered relation types only (see RFC 8631 / RFC 9727):
#   - api-catalog : the catalog document itself
#   - service-desc: machine-readable description of the public service/data
#   - service-doc : human/agent-readable documentation of the public service
PUBLIC_DISCOVERY_LINKS = (
    ("api-catalog", "/.well-known/api-catalog", "application/linkset+json", "Public API catalog"),
    (
        "service-desc",
        "/api/public/products.json",
        "application/json",
        "Public product catalog feed (Schema.org ItemList)",
    ),
    (
        "service-desc",
        "/api/public/check-delivery",
        "application/json",
        "Delivery coverage check (address or lat/lng -> deliverable)",
    ),
    (
        "service-desc",
        "/api/public/loyalty.json",
        "application/json",
        "Loyalty program facts (Aqua Club tiers + earn rules + rewards) - Schema.org MemberProgram",
    ),
    ("service-doc", "/llms.txt", "text/markdown", "LLM-friendly index of public pages"),
)


# Common typographic (non-ASCII) characters mapped to safe ASCII equivalents.
# HTTP header values must be ISO-8859-1 (latin-1); a non-latin-1 byte makes the
# whole header illegal and gunicorn 23's strict parser rejects the response with
# a 502 on EVERY storefront page. A storefront SEO/discovery nicety must never be
# able to take the site down, so titles are coerced to ASCII before emission.
_TYPOGRAPHIC_TO_ASCII = {
    "—": "-",  # em dash
    "–": "-",  # en dash
    "‘": "'",  # left single quote
    "’": "'",  # right single quote / apostrophe
    "“": "'",  # left double quote  -> single, to not break title="..."
    "”": "'",  # right double quote -> single, to not break title="..."
    "…": "...",  # horizontal ellipsis
}


def _http_safe_title(title):
    """Coerce a Link-header ``title`` to an HTTP-safe ASCII string.

    Maps common smart-punctuation to ASCII, then drops any remaining non-ASCII
    byte (graceful degradation) so the rendered ``Link`` header is always
    latin-1 encodable. Also drops the ``"`` that would otherwise terminate the
    ``title="..."`` quoted-string early.
    """
    for uni, ascii_ in _TYPOGRAPHIC_TO_ASCII.items():
        title = title.replace(uni, ascii_)
    return title.encode("ascii", "ignore").decode("ascii").replace('"', "'")


def build_link_header(links=PUBLIC_DISCOVERY_LINKS):
    """Render an RFC 8288 ``Link`` header value from ``(rel, href, type, title)`` tuples.

    Hrefs are emitted as root-relative references; per RFC 8288 a user agent
    resolves them against the request URL, so the header stays correct across
    domains/schemes without baking in a host. Titles are ASCII-sanitised so the
    header can never become un-encodable HTTP (see ``_http_safe_title``).
    """
    fields = []
    for rel, href, media_type, title in links:
        field = f'<{href}>; rel="{rel}"'
        if media_type:
            field += f'; type="{media_type}"'
        if title:
            field += f'; title="{_http_safe_title(title)}"'
        fields.append(field)
    return ", ".join(fields)


def build_api_catalog_linkset(abs_url, anchor_path="/", links=PUBLIC_DISCOVERY_LINKS):
    """Build an RFC 9264 linkset describing the public API surface.

    ``abs_url`` is a callable mapping a root-relative path to an absolute URL
    (e.g. the frontend ``_absolute_public_url`` helper), keeping the document's
    hrefs absolute and proxy-scheme aware. The ``api-catalog`` self relation is
    omitted from the body — this document *is* that resource.
    """
    context = {"anchor": abs_url(anchor_path)}
    for rel, path, media_type, title in links:
        if rel == "api-catalog":
            continue
        context.setdefault(rel, []).append({"href": abs_url(path), "type": media_type, "title": title})
    return {"linkset": [context]}
