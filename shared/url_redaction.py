"""Redaction of secrets before a log line or a Sentry event carries them: credentials embedded in URLs
(`redis://:password@host`, `?password=`, a Bot API path's `/bot<id>:<secret>`) and a bot token's secret half. Shared
by the backend, Celery and the bots."""

import re
from typing import Any, Dict, Optional

# `://[user]:<password>@` ; a URL with only a user (`scheme://user@host`) has no password and does not match.
# Fail closed: the password run stops only at `/` and whitespace. Compose and shared/secrets_manager.py put the
# passwords into URLs raw, so a working URL's password may hold a quote, `<`, `>`, `?`, `#`, `:` or `@`, and leaking
# it is worse than over-masking a log line. The greedy run backs off to its LAST `@`, as urlparse and redis-py read
# the URL, so a raw `@` in a password is masked whole.
# `/` stays a stop on purpose. Every `://` holds one, so the runs that start at two different `://` never overlap:
# a match starts only at a literal `://`, each `://` is scanned once up to the next `/` or whitespace, and the cost is
# linear for any input. It runs on every log line and on unauthenticated webhook bodies, so that bound matters.
# The user run also stops at `?`, `#`, `@`, a quote and an angle bracket. A URL whose host is followed by one of
# those before any `:` has no password, so compact JSON (`{"origin":"https://bluestream.uz","email":"x@y.com"}`) and
# `http://host?t=12:30@home` stay untouched.
# Accepted over-mask: a host and port followed directly by text that holds an `@` reads as `host:<password>@`.
# `https://host:443?email=a@b.com` becomes `https://host:***@b.com`, and in compact JSON a credential URL with no
# path hides the fields up to the next `@`. More is hidden, never the password.
# Not covered: a `/` or whitespace in a password (no version of this pattern masked them); a user name holding `@`,
# whitespace, a quote, `<`, `>`, `?` or `#` (the Redis user is empty and the database user is `postgres`); a query
# password holding `@` in a URL with no path (`host:6379?password=a@b` keeps `@b`). Rotated passwords must be
# [A-Za-z0-9] only.
# No scheme part: whatever precedes `://` (a digit, dots, a long run of letters) the password is masked, and no scheme
# run is rescanned from each of its start positions, which is what made the earlier patterns slow on long runs.
_URL_CREDENTIALS = re.compile(r"://(?P<user>[^:/?#@\s\"'<>]*):(?P<password>[^/\s]*)@")
# A password the client reads from the query: redis-py (`?password=`, its documented form for a unix socket) and libpq
# (directly or through SQLAlchemy). Fail closed as above: the value ends only at `&` (where every client ends it too),
# `/` and whitespace, so in quoted or JSON text the closing quote goes with it. A match starts only at `?password=` or
# `&password=` and the run needs nothing after it, so nothing is rescanned and the cost is linear. Other keys
# (`sslpassword=`, `ssl_password=`, `passwd=`) and percent-encoded key names are not covered; this project uses none.
_QUERY_PASSWORD = re.compile(r"(?P<key>[?&]password=)[^&/\s]+")
# A bot token in a Telegram Bot API path (`/bot<id>:<secret>/getUpdates`, `/file/bot<id>:<secret>/...`): httpx in the
# bots and requests in the backend send it, and Sentry's HTTP breadcrumbs record it. The bot id is public and stays,
# as `redact_bot_token` shows it; the secret ends at `/` or whitespace. Linear for the same reason.
_BOT_TOKEN_IN_PATH = re.compile(r"/bot(?P<bot_id>\d+):[^/\s]+")


def redact_url_credentials(text: Optional[str]) -> Optional[str]:
    """`text` with every credential a URL carries replaced by `***`: the password of a `scheme://[user]:password@host`
    URL, a `password=` query value and the secret half of a bot token in a Bot API path. The rest of the URL stays (but
    see the accepted over-masks above). `None` and `""` come back as they are."""
    if not text:
        return text
    text = _URL_CREDENTIALS.sub(r"://\g<user>:***@", text)
    text = _QUERY_PASSWORD.sub(r"\g<key>***", text)
    return _BOT_TOKEN_IN_PATH.sub(r"/bot\g<bot_id>:***", text)


def _redact_strings(value: Any) -> Any:
    if isinstance(value, str):
        return redact_url_credentials(value)
    if isinstance(value, dict):
        # keys too: a frame local `{url: pool}` or a `set_context` dict can hold a credential URL as a key
        return {_redact_strings(key): _redact_strings(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_redact_strings(inner) for inner in value]
    if isinstance(value, tuple):
        return tuple(_redact_strings(inner) for inner in value)
    return value


def redact_event_urls(
    event: Optional[Dict[str, Any]], hint: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """A Sentry event with `redact_url_credentials` applied to every string in it, dict keys and values alike, through
    nested dicts, lists and tuples: message, logentry, exception values, frame vars, breadcrumbs, request, extra,
    contexts and tags. Key scrubbing cannot see a URL's password, which has no key. Usable as a `before_send` as it
    is. Two keys that redact to the same text keep the later value. The SDK cuts strings at `max_value_length`
    (100,000) before `before_send` runs, so a credential URL lying across that cut cannot be matched. Transactions
    do not pass through `before_send`."""
    return _redact_strings(event)


def redact_bot_token(token: Optional[str]) -> str:
    """A Telegram bot token as a log line may show it: the bot id, the text before the FIRST `:`, which is public,
    then `:***`. `***` for a token with no `:`, `NOT SET` for an empty or missing one. Never a secret character."""
    if not token:
        return "NOT SET"
    if ":" not in token:
        return "***"
    return token.partition(":")[0] + ":***"
