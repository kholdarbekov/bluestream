"""D2: a URL's password never reaches a log line, a log filter or Sentry."""

import ast
import json
import logging
import re
import string
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.url_redaction import redact_url_credentials

PASSWORD = "s3cr3t-Z9"
REPO = Path(__file__).resolve().parents[2]
_COMPACT_JSON = '{"origin":"https://bluestream.uz","email":"x@y.com"}'


@pytest.mark.parametrize(
    "text, expected",
    [
        ("redis://:p@h:6379/0", "redis://:***@h:6379/0"),
        ("rediss://u:p@h", "rediss://u:***@h"),
        ("postgresql+psycopg2://u:p@h:5432/db", "postgresql+psycopg2://u:***@h:5432/db"),
        ("amqp://u:p@h//", "amqp://u:***@h//"),
        ("redis://:p%40ss@h:6379/0", "redis://:***@h:6379/0"),
        ("redis://:a:b!$x@h/0", "redis://:***@h/0"),
        ("a redis://:p1@h1/0 and redis://u:p2@h2/1 end", "a redis://:***@h1/0 and redis://u:***@h2/1 end"),
        ("scheme://user@host/x", "scheme://user@host/x"),
        ("no credentials http://example.com:80/x?y=1", "no credentials http://example.com:80/x?y=1"),
        # whatever precedes `://` (a digit, dots, a long scheme-character run) never stops the match
        ("1.redis://:p@h/0", "1.redis://:***@h/0"),
        ("connecting ...redis://:p@h/0", "connecting ...redis://:***@h/0"),
        ("x" + "1" * 32 + "://:secret@h", "x" + "1" * 32 + "://:***@h"),
        # a raw `@` in the password is masked whole: the password ends at the LAST `@`, as urlparse and redis-py read it
        ("redis://:p@ss@h:6379/0", "redis://:***@h:6379/0"),
        ("redis://:pw@h:6379 admin@x.com", "redis://:***@h:6379 admin@x.com"),
        ("redis://:pw@[::1]:6379/0", "redis://:***@[::1]:6379/0"),
        # a `?`, `#` or quote ends the user part: with no `:` before it there is no password, and a later `@` stays
        ("http://example.com?t=12:30@home", "http://example.com?t=12:30@home"),
        (_COMPACT_JSON, _COMPACT_JSON),
        # Accepted over-mask (fix round 3, G1: fail closed). The password run stops only at `/` and whitespace, so a
        # host and port followed directly by text holding an `@` reads as `host:<password>@`. It hides more than it
        # should, never less: the password is gone either way.
        ("https://host:443?email=a@b.com", "https://host:***@b.com"),
        ('{"a":"redis://:FAKEpw@h:6379","e":"x@y.com"}', '{"a":"redis://:***@y.com"}'),
        ("", ""),
        (None, None),
    ],
)
def test_redact_url_credentials(text, expected):
    assert redact_url_credentials(text) == expected


# ---- G1 (fix round 3): fail closed on the password's characters. Compose and shared/secrets_manager.py interpolate
# the Postgres and Redis passwords raw, so a rotated password holding a quote, `<`, `>`, `?` or `#` is a working URL;
# round 2's password run stopped at those and printed the whole password. Only `/` still ends it (accepted: it keeps
# the pattern linear, and rotated passwords must be [A-Za-z0-9] only); whitespace is not a password character.
_FAKE_HEAD, _FAKE_TAIL = "FAKEhead", "FAKEtail"
_CREDENTIAL_URLS = {
    "redis": "redis://:{password}@redis:6379/0",
    "postgresql": "postgresql+psycopg2://postgres:{password}@postgres:5432/bluestream_db",
}
_CONTEXTS = {
    "bare": lambda url: url,
    "space-after": lambda url: f"{url} connected",
    "single-quoted": lambda url: f"'{url}'",
    "double-quoted": lambda url: f'"{url}"',
    "json": lambda url: json.dumps({"url": url, "attempt": 1}),
    "comma-after": lambda url: f"{url}, retrying",
}


@pytest.mark.parametrize("context", list(_CONTEXTS))
@pytest.mark.parametrize("url", list(_CREDENTIAL_URLS))
@pytest.mark.parametrize("character", [c for c in string.punctuation if c != "/"], ids=lambda c: f"0x{ord(c):02x}")
def test_a_password_holding_any_punctuation_but_a_slash_is_masked_whole(character, url, context):
    password = f"{_FAKE_HEAD}{character}{_FAKE_TAIL}"
    out = redact_url_credentials(_CONTEXTS[context](_CREDENTIAL_URLS[url].format(password=password)))

    assert _FAKE_HEAD not in out and _FAKE_TAIL not in out
    assert _CREDENTIAL_URLS[url].format(password="***") in out  # the host, port and path survive


# ---- Fix round 3b: credentials a client reads from outside `user:password@`. redis-py (`?password=`, its documented
# form for a unix socket) and libpq (`?password=`, directly or through SQLAlchemy) take the password from the query.
# python-telegram-bot (httpx) and the backend (requests) put the bot token in every Bot API request's path, and
# Sentry's HTTP integrations record that URL as a breadcrumb's `url`.
_QUERY_PASSWORD = "QX7KPZR4MN"  # alphanumeric, as rotated passwords must be
_QUERY_PASSWORD_URLS = {
    "redis-tcp": "redis://redis:6379/0?password={password}",
    "redis-unix-socket": "unix:///run/redis/redis.sock?db=0&password={password}",
    "libpq": "postgresql://postgres@postgres:5432/bluestream_db?password={password}&sslmode=disable",
    "libpq-host-in-query": "postgresql:///bluestream_db?host=postgres&password={password}",
}


def _password_the_client_reads(url):
    if url.startswith("postgresql"):
        from psycopg2.extensions import parse_dsn  # libpq's own parser

        return parse_dsn(url)["password"]
    from redis.connection import parse_url

    return parse_url(url)["password"]


@pytest.mark.parametrize("context", list(_CONTEXTS))
@pytest.mark.parametrize("url", list(_QUERY_PASSWORD_URLS))
def test_a_password_in_the_query_is_masked(url, context):
    working_url = _QUERY_PASSWORD_URLS[url].format(password=_QUERY_PASSWORD)
    assert _password_the_client_reads(working_url) == _QUERY_PASSWORD  # the client authenticates with it

    out = redact_url_credentials(_CONTEXTS[context](working_url))

    assert _QUERY_PASSWORD not in out
    assert _QUERY_PASSWORD_URLS[url].format(password="***") in out  # the rest of the URL survives


# Fail closed, as G1: the value ends only at `&` (where every client ends it too, so a raw `&` cuts the password the
# client reads, and that head is masked), `/` (the accepted, documented gap) and whitespace.
@pytest.mark.parametrize("context", list(_CONTEXTS))
@pytest.mark.parametrize("url", ["redis-tcp", "libpq-host-in-query"])
@pytest.mark.parametrize("character", [c for c in string.punctuation if c not in "/&"], ids=lambda c: f"0x{ord(c):02x}")
def test_a_query_password_holding_any_punctuation_but_a_slash_or_an_ampersand_is_masked_whole(character, url, context):
    password = f"{_FAKE_HEAD}{character}{_FAKE_TAIL}"
    out = redact_url_credentials(_CONTEXTS[context](_QUERY_PASSWORD_URLS[url].format(password=password)))

    assert _FAKE_HEAD not in out and _FAKE_TAIL not in out
    assert _QUERY_PASSWORD_URLS[url].format(password="***") in out


@pytest.mark.parametrize(
    "text, expected",
    [
        ("redis://redis:6379/0?password=QX7KPZR4MN", "redis://redis:6379/0?password=***"),
        (
            "postgresql:///bluestream_db?host=postgres&password=QX7KPZR4MN&sslmode=disable",
            "postgresql:///bluestream_db?host=postgres&password=***&sslmode=disable",
        ),
        # only the `password` key: a key that merely starts with it is not a credential
        ("https://bluestream.uz/reset?password_reset=1", "https://bluestream.uz/reset?password_reset=1"),
        (
            "https://api.telegram.org/bot7012345678:AAQzX9kV7wPq2Lm4RtYbN8cJ3hG6dF5sUeW/getUpdates",
            "https://api.telegram.org/bot7012345678:***/getUpdates",
        ),
        (
            "https://api.telegram.org/file/bot7012345678:AAQzX9kV7wPq2Lm4RtYbN8cJ3hG6dF5sUeW/photos/file_7.jpg",
            "https://api.telegram.org/file/bot7012345678:***/photos/file_7.jpg",
        ),
    ],
)
def test_query_passwords_and_bot_api_tokens_have_exact_redacted_forms(text, expected):
    assert redact_url_credentials(text) == expected


_BOT_TOKEN_SECRET = "AAQzX9kV7wPq2Lm4RtYbN8cJ3hG6dF5sUeW"  # fake, shaped like a token's secret half
_BOT_API_URLS = {
    "method": "https://api.telegram.org/bot7012345678:{secret}/getUpdates",
    "file": "https://api.telegram.org/file/bot7012345678:{secret}/photos/file_7.jpg",
}


def _windows_of(secret, size=4):
    """Every 4-character window of `secret`: a text holding any part of it (a head, a tail) contains one."""
    return {secret[i : i + size] for i in range(len(secret) - size + 1)}


@pytest.mark.parametrize("context", list(_CONTEXTS))
@pytest.mark.parametrize("url", list(_BOT_API_URLS))
def test_a_bot_token_in_a_bot_api_path_keeps_only_the_public_bot_id(url, context):
    out = redact_url_credentials(_CONTEXTS[context](_BOT_API_URLS[url].format(secret=_BOT_TOKEN_SECRET)))

    assert not any(part in out for part in _windows_of(_BOT_TOKEN_SECRET))
    assert _BOT_API_URLS[url].format(secret="***") in out


# ---- D2-1: linear time. The helper runs on every log line and in Sentry's before_send, and payment_webhook logs the
# unauthenticated body at INFO before any signature check. A quadratic pattern took ~40 s on 200k letters, holding
# the GIL past gunicorn's timeout; the linear one takes tens of milliseconds, so 1 s is a wide, load-proof margin.
# Fix round 2 (N1): the round-1 pattern still rescanned every `://` from up to 32 start positions; the literal-led
# pattern scans each `://` once. Fix round 3 (G2): each 4 MB shape below that comes before the letter run takes 1.7 to
# 2 s on the round-1 pattern (rescanning), so it fails there. The letter run is the round-0 (quadratic) guard. The last
# 4 MB shape is the worst found for round 3's widened password run: every `://` becomes a match only that run makes,
# so the substitution itself is the cost. Fix round 3b: the query-password and Bot API path runs, each at its worst
# shape found (every occurrence a match).
_HUGE = 200_000
_FOUR_MB = 4 * 1024 * 1024
_LINEAR_BUDGET_S = 1.0


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a" * 32 + "://:" + "x" * (_FOUR_MB - 38) + " @", id="n1-scheme-led-unclosed-password-4mb"),
        pytest.param(("a" * 31 + "://:" + "x" * 200 + " ") * (_FOUR_MB // 236), id="near-urls-4mb"),
        pytest.param(("a" * 1000 + "://") * (_FOUR_MB // 1003), id="long-scheme-runs-4mb"),
        pytest.param("a" * _FOUR_MB, id="round-0-quadratic-guard-letter-run-4mb"),
        pytest.param("://:?@" * (_FOUR_MB // 6), id="widened-password-run-every-url-a-match-4mb"),
        pytest.param("&password=x" * (_FOUR_MB // 11), id="query-password-every-value-a-match-4mb"),
        pytest.param("/bot1:x/" * (_FOUR_MB // 8), id="bot-api-path-every-token-a-match-4mb"),
        pytest.param("a://" + "b" * _HUGE, id="run-after-a-scheme"),
        pytest.param(" ".join(["0123456789abcdef" * 3125] * 4), id="hex-runs"),
        pytest.param(("a" * 32 + "://:" + "x" * 200 + " ") * (_HUGE // 240), id="many-near-urls"),
    ],
)
def test_redact_url_credentials_is_linear(text):
    started = time.perf_counter()
    redact_url_credentials(text)
    assert time.perf_counter() - started < _LINEAR_BUDGET_S


def test_click_webhook_with_a_huge_body_is_logged_in_linear_time(app, client, monkeypatch):
    """The attack path: an unsigned Click webhook whose JSON carries a long run of letters. payment_webhook logs the
    body at INFO; the record goes through the SecuritySensitiveFilter that production's console handler carries."""
    from business_app.utils.logging_config import SecuritySensitiveFilter

    console = next(h for h in app.logger.handlers if type(h) is logging.StreamHandler)
    production_filters = [f for f in console.filters if isinstance(f, SecuritySensitiveFilter)]
    assert production_filters, "the console handler must carry the security filter"
    messages = []

    class _Collect(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    collector = _Collect(logging.INFO)  # the test config logs at ERROR; the collector runs the real filter at INFO
    for production_filter in production_filters:
        collector.addFilter(production_filter)
    monkeypatch.setattr(app.logger, "disabled", False)
    previous_level = app.logger.level
    app.logger.setLevel(logging.INFO)
    app.logger.addHandler(collector)
    try:
        started = time.perf_counter()
        client.post("/api/v1/payments/webhook/click", json={"click_trans_id": 1, "note": "A" * _HUGE})
        elapsed = time.perf_counter() - started
    finally:
        app.logger.removeHandler(collector)
        app.logger.setLevel(previous_level)
    assert any(m.startswith("/webhook/click started with payload") for m in messages)
    assert elapsed < 2 * _LINEAR_BUDGET_S  # the request itself, not only the filter


def test_sentry_before_send_is_linear_on_a_huge_breadcrumb():
    from business_app.utils.sentry import before_send

    event = {"breadcrumbs": {"values": [{"message": "/webhook/click started with payload: " + "A" * _HUGE}]}}
    started = time.perf_counter()
    before_send(event)
    assert time.perf_counter() - started < _LINEAR_BUDGET_S


def _token_service_logs(monkeypatch, *, ping):
    """The messages `TokenService._initialize_redis` logs. A handler on the service's own logger collects them, so
    the app's logging setup (propagation, levels) cannot hide them."""
    from business_app.services.token_service import TokenService

    url = f"redis://:{PASSWORD}@redis-host:6379/3"
    monkeypatch.setenv("REDIS_URL", url)
    client = MagicMock()
    client.ping.side_effect = ping
    service = object.__new__(TokenService)
    service.redis_available = False
    messages = []

    class _Collect(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    token_logger = logging.getLogger("business_app.services.token_service")
    handler = _Collect(logging.DEBUG)
    monkeypatch.setattr(token_logger, "disabled", False)
    previous_level = token_logger.level
    token_logger.setLevel(logging.DEBUG)  # setLevel, not an attribute write: it clears the logger's level cache
    token_logger.addHandler(handler)
    try:
        with patch("business_app.services.token_service.redis.from_url", return_value=client):
            # new=MagicMock(): patch() would otherwise build an AsyncMock for the LocalProxy
            with patch("business_app.services.token_service.current_app", new=MagicMock()) as app:
                app.config.get.return_value = None
                service._initialize_redis()
    finally:
        token_logger.removeHandler(handler)
        token_logger.setLevel(previous_level)
    return messages


def test_token_service_connected_log_has_host_not_password(monkeypatch):
    messages = _token_service_logs(monkeypatch, ping=None)
    assert any("redis-host" in m for m in messages)
    assert not any(PASSWORD in m for m in messages)


def test_token_service_unavailable_log_has_no_password(monkeypatch):
    err = ConnectionError(f"Error connecting to redis://:{PASSWORD}@redis-host:6379/3")
    messages = _token_service_logs(monkeypatch, ping=err)
    assert any("not available" in m for m in messages)
    assert not any(PASSWORD in m for m in messages)


def test_security_filter_redacts_credential_url():
    from business_app.utils.logging_config import SecuritySensitiveFilter

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "connected to %s", (f"redis://:{PASSWORD}@h:6379/0",), None)
    SecuritySensitiveFilter().filter(record)
    assert PASSWORD not in record.getMessage()
    assert "redis://:***@h:6379/0" in record.getMessage()


_REDACTED_URL = "redis://:***@h:6379/0"


def test_sentry_before_send_redacts_credential_urls_and_keeps_key_scrubbing():
    from business_app.utils.sentry import before_send

    url = f"redis://:{PASSWORD}@h:6379/0"
    event = {
        "message": f"conn {url}",
        "logentry": {"message": f"conn {url}", "formatted": f"conn {url}", "params": [url, 5]},
        "exception": {
            "values": [
                {
                    "type": "ConnectionError",
                    "value": f"failed {url}",
                    # Sentry sends frame locals as repr strings, keyed by the variable's name
                    "stacktrace": {"frames": [{"vars": {"redis_url": f"'{url}'", "password": "hunter2"}}]},
                }
            ]
        },
        "breadcrumbs": {"values": [{"message": f"Redis connected to {url}", "data": {"dsn": url, "n": 1}}]},
        "extra": {"password": "hunter2", "connected_to": url},
        "tags": {"broker": url},
        "contexts": {"celery": {"broker": url}},
    }
    out = before_send(event)
    assert PASSWORD not in repr(out)
    assert out["message"] == f"conn {_REDACTED_URL}"
    assert out["logentry"]["params"][0] == _REDACTED_URL
    assert out["breadcrumbs"]["values"][0]["data"]["n"] == 1
    frame_vars = out["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert frame_vars["redis_url"] == f"'{_REDACTED_URL}'"
    assert out["extra"]["connected_to"] == _REDACTED_URL
    assert out["tags"]["broker"] == _REDACTED_URL
    assert out["contexts"]["celery"]["broker"] == _REDACTED_URL
    # the key scrubbing still holds
    assert out["extra"]["password"] == "[REDACTED]"
    assert frame_vars["password"] == "[REDACTED]"
    assert "hunter2" not in repr(out)


def test_redact_event_urls_walks_every_string_and_serves_as_a_before_send():
    from shared.url_redaction import redact_event_urls

    url = f"redis://:{PASSWORD}@h:6379/0"
    event = {
        "message": f"conn {url}",
        "logentry": {"message": "conn %s", "params": [url]},
        "exception": {"values": [{"value": f"failed {url}", "stacktrace": {"frames": [{"vars": {"u": f"'{url}'"}}]}}]},
        "breadcrumbs": {"values": [{"message": f"Redis connected to {url}", "data": {"args": (url, 1)}}]},
        "request": {"url": "https://bluestream.uz/x", "data": {"next": url}},
        "extra": {"nested": [{"deep": url}]},
        "contexts": {"celery": {"broker": url}},
        "tags": {"broker": url},
        "level": "error",
        "timestamp": 1.5,
    }
    out = redact_event_urls(event, {"exc_info": None})  # the before_send signature: (event, hint)
    assert PASSWORD not in repr(out)
    assert out["breadcrumbs"]["values"][0]["data"]["args"] == (_REDACTED_URL, 1)
    assert out["extra"]["nested"][0]["deep"] == _REDACTED_URL
    assert out["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]["u"] == f"'{_REDACTED_URL}'"
    assert (out["request"]["url"], out["level"], out["timestamp"]) == ("https://bluestream.uz/x", "error", 1.5)
    assert redact_event_urls(None) is None


def _event_with_credential_url_keys(url):
    """G3 (fix round 3): a credential URL as a dict KEY. Sentry serialises a frame local `{url: pool}` as a nested
    dict with the same str key, and `set_context(url, ...)` or a context dict keyed by URLs puts one in `contexts`."""
    return {
        "exception": {"values": [{"stacktrace": {"frames": [{"vars": {"pools": {url: 1}}}]}}]},
        "contexts": {url: {"state": "down"}, "celery": {"brokers": {url: "primary"}}},
    }


def _through_redact_event_urls(event):
    from shared.url_redaction import redact_event_urls

    return redact_event_urls(event)


def _through_backend_before_send(event):
    from business_app.utils.sentry import before_send

    return before_send(event)


def _through_customer_bot_before_send(event):
    from telegram_bot.sentry_scrub import before_send  # the bot imports it as `sentry_scrub`; the same file

    return before_send(event)


@pytest.mark.parametrize(
    "redact",
    [_through_redact_event_urls, _through_backend_before_send],
    ids=["redact_event_urls", "backend-before_send"],
)
def test_credential_urls_in_dict_keys_are_rewritten(redact):
    out = redact(_event_with_credential_url_keys(f"redis://:{PASSWORD}@h:6379/0"))

    assert PASSWORD not in repr(out)
    frame_vars = out["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert frame_vars == {"pools": {_REDACTED_URL: 1}}
    assert out["contexts"] == {_REDACTED_URL: {"state": "down"}, "celery": {"brokers": {_REDACTED_URL: "primary"}}}


def _error_event_after_a_bot_api_call(redact):
    """The error event the real Sentry SDK hands `before_send` after a Bot API request. The httpx integration (the
    transport of python-telegram-bot; the SDK enables it by default in both bots) records the request as an
    `httplib` breadcrumb whose `url` holds the token. Only the network is mocked, and the event is dropped in
    `before_send`: the client has no DSN (tests/conftest.py) and returns None, so nothing leaves the process."""
    import httpx
    import sentry_sdk
    from sentry_sdk.integrations.httpx import HttpxIntegration

    seen = []
    client = sentry_sdk.Client(
        before_send=lambda event, hint: seen.append(redact(event)),
        integrations=[HttpxIntegration()],
        default_integrations=False,
        auto_enabling_integrations=False,
    )
    with sentry_sdk.isolation_scope():
        sentry_sdk.get_current_scope().set_client(client)
        network = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "result": []}))
        with httpx.Client(transport=network) as http:
            http.post(_BOT_API_URLS["method"].format(secret=_BOT_TOKEN_SECRET))
        sentry_sdk.capture_message("Update polling failed")
    (event,) = seen
    return event


@pytest.mark.parametrize(
    "redact",
    [_through_redact_event_urls, _through_customer_bot_before_send, _through_backend_before_send],
    ids=["staff_bot-redact_event_urls", "telegram_bot-sentry_scrub", "backend-before_send"],
)
def test_a_bot_api_breadcrumb_reaches_sentry_with_the_bot_id_only(redact):
    event = _error_event_after_a_bot_api_call(redact)

    (crumb,) = [crumb for crumb in event["breadcrumbs"]["values"] if crumb.get("category") == "httplib"]
    assert crumb["data"]["url"] == _BOT_API_URLS["method"].format(secret="***")
    assert not any(part in repr(event) for part in _windows_of(_BOT_TOKEN_SECRET))


# ---- R5: the class guard -------------------------------------------------------------------------------------

_CONN_NAME = re.compile(r"(?i)(redis|database|db|broker|backend|sqlalchemy|celery|amqp|dsn).*(url|uri|dsn)$")
_BARE_NAME = re.compile(r"(?i)^(url|uri|dsn)$")
# webhook_url / base_url / BASE_URL are public endpoints with no credentials, so they are not connection URLs. Only
# those exact names (bare or an attribute tail): `redis_base_url` or `db_base_url` can carry a password.
_NOT_CONNECTION = re.compile(r"(?i)^(webhook_url|base_url)$")
# `.get("REDIS_URL")` (os.environ.get, config.get) and `os.getenv("REDIS_URL")`; `os.environ["REDIS_URL"]` is a
# Subscript, which `_name_of` already names by its key.
_ENV_READS = {"get", "getenv"}
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_ROOTS = ("business_app", "staff_bot", "telegram_bot", "shared")


def _name_of(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value
    return None


def _dotted(node):
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else node.attr
    return _name_of(node)


def _is_connection_name(name, dotted=None):
    """A bare `url` / `uri` / `dsn` counts only as the tail of a connection-ish source (`config.database.url`)."""
    if not name or _NOT_CONNECTION.search(name):
        return False
    if _CONN_NAME.search(name):
        return True
    return bool(_BARE_NAME.match(name) and dotted and _CONN_NAME.search(dotted))


def _is_redacted(node):
    return isinstance(node, ast.Call) and _name_of(node.func) == "redact_url_credentials"


def _leaks(expr):
    """Connection-URL-named expressions inside `expr` that are not wrapped in redact_url_credentials."""
    found = []

    def walk(node):
        if _is_redacted(node):
            return
        name = _name_of(node)
        if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)) and _is_connection_name(name, _dotted(node)):
            found.append(name)
            return
        if isinstance(node, ast.Call) and _name_of(node.func) in _ENV_READS and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and _is_connection_name(str(arg.value)):
                found.append(str(arg.value))
                return
        if isinstance(node, ast.IfExp):  # the condition is tested, never interpolated
            walk(node.body)
            walk(node.orelse)
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(expr)
    return found


def _leaky_calls(tree):
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_print = isinstance(func, ast.Name) and func.id == "print"
        is_log = isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS
        if not (is_print or is_log):
            continue
        for arg in list(node.args) + [k.value for k in node.keywords]:
            for name in _leaks(arg):
                out.append((node.lineno, name))
    return out


def _scan(source_by_path):
    return [(p, ln, n) for p, src in source_by_path.items() for ln, n in _leaky_calls(ast.parse(src))]


def test_guard_bites_on_the_base_line():
    base = 'logger.info(f"Redis connected successfully to {redis_url}")'
    assert _scan({"token_service.py": base}) == [("token_service.py", 1, "redis_url")]
    assert _scan({"x.py": 'logger.info("c %s", redis_url)'})
    assert _scan({"x.py": 'logger.info("c {}".format(self.database_url))'})
    assert not _scan({"x.py": 'logger.info(f"c {redact_url_credentials(redis_url)}")'})
    assert _scan({"x.py": 'logger.info(f"d {config.database.url.split(chr(64))[0]}")'})
    assert not _scan({"x.py": 'logger.info(f"u {url} {response.url}")'})
    assert not _scan({"x.py": 'logger.info(f"hook {webhook_url} {self.base_url} {BASE_URL}")'})


@pytest.mark.parametrize(
    "line",
    [
        "logger.info(f\"x {os.getenv('REDIS_URL')}\")",
        "logger.info(f\"x {os.environ.get('DATABASE_URL')}\")",
        "logger.info(f\"x {os.environ['CELERY_BROKER_URL']}\")",
        'logger.info(f"x {redis_base_url}")',
        'logger.info("x %s", db_base_url)',
        'logger.info("x %s", self.redis_base_url)',
    ],
)
def test_guard_bites_on_environment_reads_and_base_url_lookalikes(line):
    assert _scan({"x.py": line})


def _repo_sources():
    sources = {}
    for root in _ROOTS:
        for path in (REPO / root).rglob("*.py"):
            parts = path.relative_to(REPO).parts
            if "tests" in parts or "migrations" in parts or "__pycache__" in parts:
                continue
            sources[path.relative_to(REPO).as_posix()] = path.read_text(encoding="utf-8")
    return sources


def test_no_log_or_print_interpolates_an_unredacted_connection_url():
    sources = _repo_sources()
    # an empty scan passes vacuously: pin the file the leak was in and at least one file under every root
    assert "business_app/services/token_service.py" in sources
    assert all(any(path.startswith(f"{root}/") for path in sources) for root in _ROOTS)
    assert _scan(sources) == []
