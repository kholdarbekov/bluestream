"""What a secret is, for every process that reports to Sentry: the backend (business_app/utils/sentry.py, and the
Celery tasks' secret arguments, business_app/tasks/secret_arguments.py), the customer bot (telegram_bot/sentry_scrub.py)
and the staff bot (staff_bot/bot.py). One rule, here, importing nothing of the backend or the bots.

`scrub_event` applies it to a Sentry event in three ways:

1. By name. Whatever sits under a secret name (`names_a_secret`) becomes `[REDACTED]` and the name stays: in every
   frame's locals (an exception's frames, the `threads` stack `attach_stacktrace=True` adds to a logged error, a bare
   stacktrace), in extra, contexts, tags, breadcrumb data and the request (body, headers, cookies).
2. By value. A secret a frame holds by name travels under other names: a sign-up code is also in the SMS text and in
   the provider call's `message`. Each such value is hidden wherever else the event shows it.
3. By shape, for a secret with no name of its own, in every string: a JWT, a `Bearer` value, a `name=value` pair whose
   name is secret (a query, a cookie line, free text), the token at the end of a Redis key that ends in one, the token
   in a link whose path carries one, and a URL's password (`shared.url_redaction.redact_event_urls`).
"""

import re
from functools import lru_cache
from typing import Any, Callable, Dict, Iterable, Optional, Set
from urllib.parse import unquote_plus

from sentry_sdk.utils import iter_event_frames

from shared.constants import STAFF_INVITE_START_PREFIX, WEB_LINK_START_PREFIX
from shared.redis_keyspace import RedisKeyspace
from shared.url_redaction import redact_event_urls

REDACTED = "[REDACTED]"

# ----------------------------------------------------------------------------------------------------------- names

# Anywhere in a name: the request log's names (business_app/utils/error_handlers.py `sanitize_request_data` still
# keeps a copy of them; tests/integration/test_sentry_one_secret_rule.py holds this rule to cover every one of them).
# `key` and `pin` are wide on purpose, fail closed: `api_key`, `X-Api-Key`, `merchantKey`, `pin_code`.
_SECRET_NAME_PARTS = (
    "password",
    "token",
    "secret",
    "key",
    "authorization",
    "cookie",  # the log filter's names too (business_app/utils/logging_config.py SecuritySensitiveFilter)
    "csrf",
    "credit_card",
    "card_number",
    "cvv",
    "pin",
    "ssn",
    "passport",
)
# A word of a name: a one-time code (`otp`, `otp_code`, `stored_otp`), a signature (a webhook's `signature`, Click's
# `sign_string` and `sign_source`, which carries the merchant's secret key).
_SECRET_WORDS = frozenset({"otp", "sign", "signature"})
# The whole name: `code` is the card's SMS code in api/payments.verify_card and Google's OAuth code; `session` is
# Flask's signed session cookie. A `session_id` names a session (a JWT claim, /auth/sessions/<session_id>) and stays.
_SECRET_WHOLE_NAMES = frozenset({"code", "session"})
# `<purpose>_code`: a code sent to prove a phone or an address (`verification_code`, `sms_code`, `confirmation_code`).
# Every other code is data and stays readable: `status_code`, `error_code`, `language_code`, `promo_code`,
# `verification_reason_code`.
_SECRET_CODE_PURPOSES = frozenset({"verification", "verify", "sms", "confirmation", "confirm"})
# The words of `smsCode`, `OTPCode`, `X-Api-Key`, `sign_string`.
_NAME_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


@lru_cache(maxsize=4096)
def names_a_secret(name: str) -> bool:
    """Whether what sits under `name` (a variable, a dict key, a header, a query or cookie name) is a secret."""
    lowered = name.lower()
    words = [word.lower() for word in _NAME_WORD.findall(name)]
    return (
        any(part in lowered for part in _SECRET_NAME_PARTS)
        or lowered in _SECRET_WHOLE_NAMES
        or not _SECRET_WORDS.isdisjoint(words)
        or any(word in _SECRET_CODE_PURPOSES and after == "code" for word, after in zip(words, words[1:]))
    )


# ---------------------------------------------------------------------------------------------------------- values

# The values under secret names that are looked for elsewhere in the event: at least 4 characters with a digit, as a
# code, a token and a password (the password rule demands one) have. A shorter value would match everywhere, and the
# names are wide (`key`, `pin`): a word such as `name` held by a loop's `key` stays where it is.
_SHORTEST_TRAVELLING_SECRET = 4
_DIGIT = re.compile(r"\d")
# Times and ids, never text: a code's digits can be a timestamp's microseconds or part of an id's hex.
_NOT_TEXT = frozenset({"timestamp", "start_timestamp", "event_id", "trace_id", "span_id", "parent_span_id"})


def worth_following(value: str) -> bool:
    """Whether a string found under a secret name is looked for elsewhere in an event (see above). The one rule for
    the frame locals here and for the nested values of a Celery task's secret arguments
    (business_app/tasks/secret_arguments.py)."""
    return len(value) >= _SHORTEST_TRAVELLING_SECRET and bool(_DIGIT.search(value))


def _remember(local: Any, travelling: Set[str]) -> None:
    """A secret frame local's value, to hide wherever else it appears. The SDK writes a string local as its repr
    (`'482913'`); anything else (a number, `None`, an object's repr) is not followed."""
    if isinstance(local, str) and len(local) >= 2 and local[0] == local[-1] and local[0] in "'\"":
        value = local[1:-1]
        if worth_following(value):
            travelling.add(value)


# ---------------------------------------------------------------------------------------------------------- shapes

# A JWT (a header `{"...` in base64url, a payload, a signature) and a `Bearer` token (RFC 6750 `b64token`; from eight
# characters, so prose such as "the bearer of" keeps its next word), anywhere in a string. A JWT starts only where no
# base64url character precedes it: `\b` would also start one before every `eyJ` that follows a `-` inside a long run,
# and each such start rescans the run to its end, quadratic on a string an attacker shapes ("-eyJ" * n).
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_BEARER = re.compile(r"(?P<kept>\b(?i:bearer)\s+)[A-Za-z0-9._~+/-]{8,}=*")
# Strings that end in the secret itself, kept readable up to it. Redis keys: a password-reset or email-verification
# token (`verification:<kind>:<token>`, which Sentry's Redis integration writes into every span and breadcrumb), a
# one-time web-to-Telegram sign-in link, a one-time staff invite. Page paths, every route of the site with a
# secret-named variable (tests/integration/test_sentry_one_secret_rule.py holds this list to the routes): the emailed
# password-reset link, the reorder reminder's unsubscribe link and its undo. The secret runs to a quote, whitespace or
# the next URL part.
_SECRET_AT_THE_END_OF = (
    r"(?<![\w:])verification:\w+:",
    r"(?<![\w:])" + re.escape(RedisKeyspace.customer_telegram_link("")),
    r"(?<![\w:])" + re.escape(RedisKeyspace.staff_bot_invite("")),
    r"/reset-password/",
    r"/reminders/unsubscribe/undo/",  # before its parent, which would take `undo` for the token
    r"/reminders/unsubscribe/",
    # The same one-time tokens as Telegram deep links (`https://t.me/<bot>?start=web_<token>`) and as the `/start`
    # message the bot then receives (`/start staff_invite_<token>`). Order and subscription links carry ids only.
    r"[?&]start=(?:%s)" % "|".join(map(re.escape, (WEB_LINK_START_PREFIX, STAFF_INVITE_START_PREFIX))),
    r"/start\s+(?:%s)" % "|".join(map(re.escape, (WEB_LINK_START_PREFIX, STAFF_INVITE_START_PREFIX))),
)
_SECRET_IN_SHAPE = re.compile(r"(?P<kept>%s)[^\s/?#'\"]+" % "|".join(_SECRET_AT_THE_END_OF))
# A `name=value` pair at the start of a string or after `?`, `&`, a quote, `;` or whitespace: a query
# (`/verify-email?token=`, the request's query string), a cookie line (`refresh_token_cookie=...; session=...`), free
# text (`retry with api_key=...`). Its value goes when the name is a secret. Each name run stops at the next place a
# pair may start, so no text is scanned twice.
_NAMED_PAIR = re.compile(r"(?:^|(?<=[?&'\";\s]))(?P<name>[^&=?#\s'\";]+)=[^&#\s'\";]*")


def _hide_shapes(text: str, names: Callable[[str], bool]) -> str:
    """`text` with every secret in shape replaced (see the patterns above)."""

    def _pair(pair: "re.Match[str]") -> str:
        return f"{pair['name']}={REDACTED}" if names(unquote_plus(pair["name"])) else pair[0]

    text = _BEARER.sub(rf"\g<kept>{REDACTED}", _JWT.sub(REDACTED, text))
    return _NAMED_PAIR.sub(_pair, _SECRET_IN_SHAPE.sub(rf"\g<kept>{REDACTED}", text))


# ----------------------------------------------------------------------------------------------------------- event


def _hide_named(value: Any, names: Callable[[str], bool], travelling: Optional[Set[str]] = None) -> Any:
    """`value` with whatever sits under a secret name, at any depth, replaced by REDACTED. Frame locals pass
    `travelling`, which collects each string so hidden."""
    if isinstance(value, dict):
        hidden = {}
        for key, item in value.items():
            if isinstance(key, str) and names(key):
                if travelling is not None:
                    _remember(item, travelling)
                hidden[key] = REDACTED
            else:
                hidden[key] = _hide_named(item, names, travelling)
        return hidden
    if isinstance(value, list):
        return [_hide_named(item, names, travelling) for item in value]
    return value


def _hide_text(value: Any, travelling: Iterable[str], names: Callable[[str], bool]) -> Any:
    """`value` with every travelling secret and every secret in shape replaced, in every string at any depth."""
    if isinstance(value, str):
        for secret in travelling:
            value = value.replace(secret, REDACTED)
        return _hide_shapes(value, names)
    if isinstance(value, dict):
        return {key: item if key in _NOT_TEXT else _hide_text(item, travelling, names) for key, item in value.items()}
    if isinstance(value, list):
        return [_hide_text(item, travelling, names) for item in value]
    return value


def scrub_event(
    event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None, *, names: Callable[[str], bool] = names_a_secret
) -> Dict[str, Any]:
    """The event with no secret left in it, by name, by value and by shape (see the module docstring). Usable as a
    `before_send` as it is. `names` widens the rule for one process (the customer bot also hides personal data)."""
    for section in ("extra", "contexts", "tags"):
        if isinstance(event.get(section), dict):
            event[section] = _hide_named(event[section], names)

    request = event.get("request")
    if isinstance(request, dict):
        for key in ("data", "headers", "cookies"):  # the query string is text: `_hide_text` reads its pairs
            if isinstance(request.get(key), dict):
                request[key] = _hide_named(request[key], names)

    travelling: Set[str] = set()
    for frame in iter_event_frames(event):  # an exception's, the logged error's thread, a bare stacktrace
        if isinstance(frame.get("vars"), dict):
            frame["vars"] = _hide_named(frame["vars"], names, travelling)

    for crumb in (event.get("breadcrumbs") or {}).get("values") or []:
        if isinstance(crumb.get("data"), dict):
            crumb["data"] = _hide_named(crumb["data"], names)

    longest_first = sorted(travelling, key=len, reverse=True)  # a secret inside a longer one goes with it
    return redact_event_urls(_hide_text(event, longest_first, names))
