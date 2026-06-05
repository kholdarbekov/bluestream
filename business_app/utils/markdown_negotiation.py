"""Markdown content negotiation for AI agents.

When an agent sends ``Accept: text/markdown``, public storefront *content* pages
return a Markdown rendering instead of HTML; browsers — which never send
``text/markdown`` — keep receiving HTML, so HTML stays the default. Mirrors
Cloudflare's "Markdown for Agents" output shape (YAML frontmatter from ``<meta>``
tags, body Markdown with non-content stripped, JSON-LD preserved as fenced code
blocks) but at the application layer, consistent with the in-app agent-discovery
helpers in ``business_app/utils/agent_discovery.py``.
"""

from __future__ import annotations

import json
import math

from bs4 import BeautifulSoup
from markdownify import ATX
from markdownify import markdownify as _md

# Public *content* endpoints (``frontend.<name>``) eligible for Markdown
# negotiation: the sitemap-static page set plus product/blog detail pages. This
# is an explicit allowlist so private/interactive pages (cart, checkout, login,
# account, order tracking, subscription management, ...) are never exposed as
# Markdown by accident.
MARKDOWN_NEGOTIATION_ENDPOINTS = frozenset(
    {
        "frontend.index",
        "frontend.shop",
        "frontend.product_detail",
        "frontend.product_detail_slug",
        "frontend.subscriptions",
        "frontend.services",
        "frontend.coverage",
        "frontend.about",
        "frontend.about_sources",
        "frontend.process_filtration",
        "frontend.contact",
        "frontend.gallery",
        "frontend.blog_list",
        "frontend.blog_detail",
        "frontend.terms",
        "frontend.privacy",
        "frontend.delivery_policy",
        "frontend.pricing_policy",
        "frontend.refund_policy",
        "frontend.quality_standards",
        "frontend.water_delivery_faq",
    }
)

# Leave very large pages as HTML (Cloudflare's edge converter caps at 2 MB; we
# allow a little more before skipping).
MARKDOWN_MAX_HTML_BYTES = 3 * 1024 * 1024

# Elements that never carry primary page content.
_NON_CONTENT_TAGS = ("script", "style", "nav", "header", "footer", "noscript")

# Class/id fragments that mark loader/preloader chrome (never page content).
# Belt-and-suspenders: base.html wraps real content in <main>, so this only
# matters on the <body>-fallback path, but it keeps loaders out of the Markdown.
_NON_CONTENT_SELECTORS = ("loader-wrap", "preloader")


def wants_markdown(accept_header):
    """Return True only when ``Accept`` explicitly prefers ``text/markdown``.

    Triggers on an explicit ``text/markdown`` media range with q>0. Wildcards
    (``*/*``, ``text/*``) and ``text/html`` do NOT match, so browsers and default
    HTTP clients keep receiving HTML. Malformed entries are ignored.
    """
    if not accept_header:
        return False
    for part in accept_header.split(","):
        token = part.strip()
        if not token:
            continue
        media, _, params = token.partition(";")
        if media.strip().lower() != "text/markdown":
            continue
        q = 1.0
        for param in params.split(";"):
            param = param.strip().lower()
            if param.startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 0.0
        if q > 0:
            return True
    return False


def estimate_tokens(text):
    """Rough token estimate (~4 chars/token), documented as an estimate.

    Avoids a heavy, model-specific, native ``tiktoken`` dependency. Cloudflare's
    ``x-markdown-tokens`` header is likewise an estimate.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _first_meta(soup, name=None, prop=None):
    """Return the trimmed ``content`` of a ``<meta name=...>`` or ``<meta property=...>``."""
    if name is not None:
        tag = soup.find("meta", attrs={"name": name})
    else:
        tag = soup.find("meta", attrs={"property": prop})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def _yaml_quote(value):
    """Double-quote a scalar for YAML frontmatter.

    Escapes backslash, double-quote, and the common control chars (newline,
    carriage return, tab). Rarer C0 control characters are not enumerated —
    meta-tag values effectively never contain them.
    """
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _build_frontmatter(soup):
    """Build ``---``-delimited YAML frontmatter; emit only keys with a value.

    ``<meta name>`` wins over the Open Graph equivalent (matches Cloudflare);
    ``title`` finally falls back to the ``<title>`` element.
    """
    title = _first_meta(soup, name="title") or _first_meta(soup, prop="og:title")
    if not title and soup.title:
        title = soup.title.get_text(strip=True) or None
    description = _first_meta(soup, name="description") or _first_meta(soup, prop="og:description")
    image = _first_meta(soup, prop="og:image")

    lines = [
        f"{key}: {_yaml_quote(val)}"
        for key, val in (("title", title), ("description", description), ("image", image))
        if val
    ]
    if not lines:
        return ""
    return "---\n" + "\n".join(lines) + "\n---"


def _extract_json_ld(soup):
    """Return each ``application/ld+json`` script as a fenced ```json block (captured before stripping)."""
    blocks = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw or not raw.strip():
            continue
        try:
            pretty = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except (ValueError, TypeError):
            pretty = raw.strip()
        blocks.append(f"```json\n{pretty}\n```")
    return blocks


def html_to_markdown(html):
    """Render page HTML as Markdown: YAML frontmatter + body + JSON-LD blocks."""
    soup = BeautifulSoup(html or "", "html.parser")

    frontmatter = _build_frontmatter(soup)
    json_ld_blocks = _extract_json_ld(soup)  # capture before scripts are stripped

    root = soup.find("main") or soup.find("article") or soup.body or soup
    for tag in root.find_all(_NON_CONTENT_TAGS):
        tag.decompose()
    for tag in root.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()
    for selector in _NON_CONTENT_SELECTORS:
        for tag in root.select(f'[class*="{selector}"], [id*="{selector}"]'):
            tag.decompose()

    body_md = _md(str(root), heading_style=ATX, bullets="-").strip()

    parts = [section for section in (frontmatter, body_md, "\n\n".join(json_ld_blocks)) if section]
    return ("\n\n".join(parts)).strip() + "\n"
