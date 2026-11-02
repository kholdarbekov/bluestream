"""The "Open on website" sign-in URL never reaches an nginx log (spec 2026-10-27 §9 "Never log the query string").

Telegram opens `/auth/telegram-login?next=…&id=…&first_name=…&username=…&hash=…`; the hash signs the customer in
for five minutes, and the names are personal data. gunicorn writes no access log (scripts/gunicorn.conf.py sets
no `accesslog`), so nginx is the one access log to redact, and its error log quotes whole request lines (an upstream
that is down, a refused request). So on every host that can see the path:
- the main site logs that location in the `redacted_query` format (the path without its query), keeps only `crit`
  error lines, and serves its own error page, logged redacted too: the server's `/50x.html` logs in `main`;
- the www host logs the path redacted before its redirect to the bare domain;
- the cabinet host, which never serves the route, answers 404 without logging.
The checks read the real config files; `nginx -t` and a no-network probe (a 502) were run by hand (task report).
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

NGINX = Path(__file__).resolve().parents[2] / "nginx"
SITES = NGINX / "conf.d" / "sites"
NGINX_CONF = (NGINX / "nginx.conf").read_text(encoding="utf-8")

LOG_FORMAT = re.compile(r"log_format\s+redacted_query\s+((?:'[^']*'\s*)+);")


def _blocks(text, head):
    """The bodies of every `<head> { … }` block in `text`, nested braces included (comments dropped)."""
    text = re.sub(r"#[^\n]*", "", text)
    bodies = []
    for match in re.finditer(re.escape(head) + r"\s*\{", text):
        depth, start = 1, match.end()
        for index in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[index], 0)
            if depth == 0:
                bodies.append(text[start:index])
                break
    return bodies


def _server(site, server_name):
    [server] = [
        body
        for body in _blocks((SITES / site).read_text(encoding="utf-8"), "server")
        if re.search(rf"^\s*server_name {re.escape(server_name)};", body, re.M)
    ]
    return server


def _directives(body):
    """The block's own directives (nested blocks left out), one per line."""
    flat = re.sub(r"\{[^{}]*\}", ";", body)
    return [line.strip() + ";" for line in flat.split(";") if line.strip()]


def _location(site, server_name, path):
    [location] = _blocks(_server(site, server_name), f"location = {path}")
    return _directives(location)


def _redacted_query_format():
    match = LOG_FORMAT.search(NGINX_CONF)
    assert match, "nginx.conf: no log_format redacted_query"
    return "".join(re.findall(r"'([^']*)'", match.group(1)))


def test_the_redacted_format_logs_the_path_without_its_query_and_keeps_the_client_addresses():
    fmt = _redacted_query_format()

    assert "$uri" in fmt
    assert not re.search(r"\$request(?![_a-z])", fmt)  # $request is the whole request line, query included
    for variable in ("$request_uri", "$args", "$query_string", "$http_referer"):
        assert variable not in fmt, variable
    assert "$http_x_forwarded_for" in fmt and "$http_cf_connecting_ip" in fmt  # forensics; not secret


def test_the_main_site_logs_the_sign_in_redacted_and_limits_it_in_its_own_zone():
    location = _location("aqua-element.conf", "aqua-element.uz", "/auth/telegram-login")

    assert "access_log /var/log/nginx/access.log redacted_query;" in location
    assert "limit_req zone=telegram_login_limit burst=5 nodelay;" in location
    [http] = _blocks(NGINX_CONF, "http")
    assert "limit_req_zone $binary_remote_addr zone=telegram_login_limit:10m rate=5r/m;" in _directives(http)
    assert "proxy_pass http://$upstream_business_app:80;" in location


def test_no_error_line_or_error_page_of_the_sign_in_writes_its_query():
    """nginx's [error] lines (an upstream down or timing out) quote `request: "GET …?<query>"`, so only `crit` is
    kept; the server's `error_page … /50x.html` would log in `main`, so the location has its own error page, logged
    redacted; and limit_req's default 503 would be one of those error pages, so a refusal answers 429."""
    location = _location("aqua-element.conf", "aqua-element.uz", "/auth/telegram-login")
    error_page = _location("aqua-element.conf", "aqua-element.uz", "/50x-login.html")

    assert "error_log /var/log/nginx/error.log crit;" in location
    assert "error_page 500 502 503 504 /50x-login.html;" in location
    assert "limit_req_status 429;" in location and "limit_req_log_level info;" in location
    assert "internal;" in error_page and "alias /usr/share/nginx/html/50x.html;" in error_page
    assert "access_log /var/log/nginx/access.log redacted_query;" in error_page
    assert "error_log /var/log/nginx/error.log crit;" in error_page


def test_the_www_host_logs_the_sign_in_redacted_before_its_redirect():
    location = _location("aqua-element.conf", "www.aqua-element.uz", "/auth/telegram-login")
    main_site = _directives(_server("aqua-element.conf", "aqua-element.uz"))

    assert "access_log /var/log/nginx/access.log redacted_query;" in location
    assert "return 301 https://aqua-element.uz$request_uri;" in location
    assert not any(line.startswith("if ") for line in main_site)  # the old server-level redirect logged in `main`


def test_every_www_redirect_goes_straight_to_https():
    """Behind the Cloudflare Tunnel nginx sees plain http, so `$scheme://` would bounce a hand-edited www URL (a
    sign-in URL with its live hash included) through one plaintext hop; the cabinet redirects already say https."""
    redirects = re.findall(r"^\s*(return [^;]*;)", _server("aqua-element.conf", "www.aqua-element.uz"), re.M)

    assert len(redirects) == 2  # the sign-in location and `location /`
    assert set(redirects) == {"return 301 https://aqua-element.uz$request_uri;"}


def test_the_cabinet_host_never_serves_the_sign_in_nor_logs_it():
    location = _location("cabinet.aqua-element.conf", "cabinet.aqua-element.uz", "/auth/telegram-login")

    assert "access_log off;" in location and "return 404;" in location
    assert not any(line.startswith("proxy_pass") for line in location)
