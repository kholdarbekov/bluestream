"""Defence in depth: the public nginx hosts must answer 404 for the bot-only
and the deleted identity endpoints, ahead of every other matching location.

The regex under test is read out of the real config files, not copied here.
"""
import re
from pathlib import Path

import pytest

SITES = Path(__file__).resolve().parents[2] / "nginx" / "conf.d" / "sites"

# (file, first location that would otherwise proxy these paths to Flask)
HOSTS = [
    ("aqua-element.conf", r"location ~ \^/api/v1/\(auth\|login\|register\)/"),
    ("cabinet.aqua-element.conf", r"location /api/ \{"),
    ("admin.aqua-element.conf", r"location /api/ \{"),
]

BOT_ONLY = [
    "telegram-login",
    "telegram-register",
    "check-phone-availability",
    "link-phone-account/send-otp",
    "link-phone-account/verify",
]
DELETED = [
    "link-telegram",
    "link-web-account",
    "sync-profile",
    "suggest-auto-link",
    "generate-telegram-auth",
    "generate-web-auth",
    "verify-telegram-auth/ABC123",
    "verify-web-auth/tok_en-1",
]
STILL_PUBLIC = [
    "login",
    "register",
    "refresh",
    "send-otp",
    "verify-otp",
    "forgot-password",
    "reset-password",
    "link-phone-account",
    "telegram-link",
]

BLOCK_RE = re.compile(
    r"location ~ (\^/api/v1/auth/\S+)\s*\{\s*return 404;\s*\}"
)


def _read(name):
    return (SITES / name).read_text()


@pytest.mark.parametrize("name,next_location", HOSTS)
def test_block_exists_and_precedes_the_proxying_location(name, next_location):
    text = _read(name)
    block = BLOCK_RE.search(text)
    assert block, f"{name}: no 'return 404' location for the identity paths"
    proxied = re.search(next_location, text)
    assert proxied, f"{name}: expected location not found"
    assert block.start() < proxied.start(), (
        f"{name}: nginx takes the first matching regex location, so the 404 "
        "block must come before the proxying one"
    )


@pytest.mark.parametrize("name,_", HOSTS)
def test_block_matches_exactly_the_bot_only_and_deleted_paths(name, _):
    pattern = re.compile(BLOCK_RE.search(_read(name)).group(1))
    for tail in BOT_ONLY + DELETED:
        assert pattern.match(f"/api/v1/auth/{tail}"), tail
    for tail in STILL_PUBLIC:
        assert not pattern.match(f"/api/v1/auth/{tail}"), tail


def test_all_three_hosts_use_the_same_regex():
    regexes = {BLOCK_RE.search(_read(n)).group(1) for n, _ in HOSTS}
    assert len(regexes) == 1


# Every /api/v1/bot/ route is bot-only and signed; the bots reach business_app on the internal
# Docker network, and nothing public calls them (spec 2026-10-19 D18).
BOT_BLOCK_RE = re.compile(r"location ~ (\^/api/v1/bot/\S*)\s*\{\s*return 404;\s*\}")
_REGEX_LOCATION = re.compile(r"location ~\*? (\S+)\s*\{")
BOT_PATHS = (
    "web-link/preview",
    "web-link/confirm",
    "telegram-reachability",
    "reload-translations",
    "health",
    "stats",
)


@pytest.mark.parametrize("name,_", HOSTS)
def test_the_bot_routes_are_unreachable_from_every_public_host(name, _):
    text = _read(name)
    block = BOT_BLOCK_RE.search(text)
    pattern = re.compile(block.group(1))
    assert all(pattern.match(f"/api/v1/bot/{tail}") for tail in BOT_PATHS)
    assert not any(pattern.match(p) for p in ("/api/v1/auth/login", "/api/v1/botanica", "/api/v1/orders"))
    # nginx takes the first matching regex location, so no earlier one may claim a bot path.
    earlier = [m.group(1) for m in _REGEX_LOCATION.finditer(text[: block.start()])]
    assert not any(re.compile(p).match("/api/v1/bot/web-link/confirm") for p in earlier)


def test_all_three_hosts_block_the_bot_routes_the_same_way():
    assert len({BOT_BLOCK_RE.search(_read(name)).group(1) for name, _ in HOSTS}) == 1
