"""The admin host's Content-Security-Policy must let the Support Inbox play media.

The inbox fetches a customer's video or voice note through the authenticated API and plays it from a
`blob:` URL. With no `media-src`, the outer policy fell back to `default-src 'self'` and the browser
refused every `<video src="blob:...">` and `<audio src="blob:...">` in production, while photos kept
working because `img-src` lists `blob:`. Dev never showed it: only the admin_ui container's own
nginx answers there, and its policy allows `blob:` through `default-src`.

The browser receives BOTH policies on the admin SPA (the outer site's and the admin_ui container's)
and enforces each one, so media plays only when both allow it. Both are read from the real config
files, never copied here.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
OUTER = REPO / "nginx" / "conf.d" / "sites" / "admin.aqua-element.conf"
INNER = REPO / "admin_ui" / "nginx.conf"

_CSP = re.compile(r'add_header\s+Content-Security-Policy\s+"([^"]*)"\s+always;')


def _policies(path):
    return _CSP.findall(path.read_text())


def _directives(policy):
    out = {}
    for part in policy.split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0]] = tokens[1:]
    return out


def _media_sources(policy):
    """What a <video>/<audio> src is checked against: media-src, else default-src."""
    directives = _directives(policy)
    return directives.get("media-src", directives.get("default-src"))


@pytest.mark.unit
def test_the_admin_host_sets_exactly_one_csp():
    # A second Content-Security-Policy header is intersected with the first, not merged.
    assert len(_policies(OUTER)) == 1


@pytest.mark.unit
def test_the_admin_host_csp_names_media_src_self_and_blob():
    (policy,) = _policies(OUTER)
    assert _directives(policy).get("media-src") == ["'self'", "blob:"]


@pytest.mark.unit
def test_the_spa_location_inherits_the_server_csp():
    # nginx drops every server-level add_header in a location that declares its own add_header.
    text = OUTER.read_text()
    spa = re.search(r"location / \{(.*?)\n    \}", text, re.S)
    assert spa, "the admin SPA's `location /` block was not found"
    assert "proxy_pass http://$upstream_admin_ui" in spa.group(1)
    assert "add_header" not in spa.group(1)


@pytest.mark.unit
@pytest.mark.parametrize("path", [OUTER, INNER], ids=["outer-site", "admin_ui-container"])
def test_every_policy_the_admin_spa_receives_allows_blob_media(path):
    policies = _policies(path)
    assert policies, f"{path.name}: no Content-Security-Policy found"
    for policy in policies:
        sources = _media_sources(policy)
        assert "'self'" in sources and "blob:" in sources, (path.name, sources)
