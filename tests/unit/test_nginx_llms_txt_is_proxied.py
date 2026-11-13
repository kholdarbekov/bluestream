"""`/llms.txt` on the main site is served by Flask, not from an nginx alias (Task 11).

The Flask route (Task 10) owns the body: it prints the live price and delivery facts, so a file copied to
`/var/www/static/llms.txt` would serve a stale price. The catch-all `location /` proxies it with the full header
set and writes the server's access log, so no `location = /llms.txt` block is needed.
"""

import re

import pytest

from tests.unit.test_nginx_telegram_login_logging import (
    SITES,
    _blocks,
    _directives,
    _server,
)

pytestmark = pytest.mark.unit

SERVER = _server("aqua-element.conf", "aqua-element.uz")


def test_llms_txt_is_not_served_from_disk():
    assert _blocks(SERVER, "location = /llms.txt") == []
    conf = re.sub(r"#[^\n]*", "", (SITES / "aqua-element.conf").read_text(encoding="utf-8"))
    assert "llms.txt" not in conf


def test_the_catch_all_proxies_to_flask_and_is_logged():
    [catch_all] = _blocks(SERVER, "location /")
    directives = _directives(catch_all)
    assert "proxy_pass http://$upstream_business_app:80;" in directives
    assert not [d for d in directives if d.startswith("access_log")]  # the server's log applies
