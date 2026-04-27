"""Bot ↔ Backend route-compatibility test (TST-002).

Statically extracts every ``/api/v1/...`` URL the customer + staff bots
hit (via ``_make_request`` / ``self._client.<verb>(...)``) and asserts
each one resolves to a real Flask route on the backend.

Why this and not full Pact:
- Pact's primary value is the route-existence + method guarantee, which
  this test covers directly without dragging in a Pact mock-server, broker,
  or provider-verifier subprocess.
- Shape contracts are covered by the existing
  [test_api_response_contracts.py](test_api_response_contracts.py).
- Static extraction is fast, hermetic (no network), and runs as a normal
  pytest unit; we don't need a separate pact-runner CI lane.

What this catches (failure modes):
- Bot calls a route that has been renamed/removed in the backend.
- Bot calls a method that is no longer accepted on a route.
- Brand-new bot endpoints don't accidentally bypass review by being
  added to ``api_client.py`` without a matching Flask route.

What this does NOT catch (out of scope, by design):
- Request/response payload-shape drift — covered by
  ``test_api_response_contracts.py`` and the per-blueprint integration
  tests under ``tests/unit/test_*_api_routes.py``.
- Auth/authz changes — covered by the per-route tests.
- Behavioural drift — covered by the integration suite.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Set, Tuple

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

# Bots in scope. Each entry: (label, file path).
BOT_API_CLIENTS: Tuple[Tuple[str, Path], ...] = (
    ('customer_bot', REPO_ROOT / 'telegram_bot' / 'api_client.py'),
    ('staff_bot', REPO_ROOT / 'staff_bot' / 'api_client.py'),
)

# Dead bot api_client methods that target non-existent backend routes but
# are never called from any handler today. Surfaced by TST-002 on its first
# run; prefer an explicit allowlist over silently fixing them in this PR
# because route renames are a business-logic concern (which method on the
# backend is the canonical one?). Cleanup is a follow-up.
#
# Each entry: (label, method, normalised_path, reason).
KNOWN_DEAD_BOT_CALLS: Set[Tuple[str, str, str]] = {
    # telegram_bot/api_client.py::get_payment_status — calls /api/v1/payments/<id>
    # but backend exposes /api/v1/payments/<id>/status. Method is unused; no
    # handler imports it. Cleanup: rename URL or delete the method.
    ('customer_bot', 'GET', '/api/v1/payments/<var>'),
    # telegram_bot/api_client.py::get_delivery_slots — calls
    # /api/v1/delivery/slots/<address_id>. Backend has /api/v1/orders/delivery-slots
    # (no path param) and /api/v1/delivery/time-slots. Method is unused.
    ('customer_bot', 'GET', '/api/v1/delivery/slots/<var>'),
    # telegram_bot/api_client.py::get_analytics_overview — calls
    # /api/v1/analytics/overview. Backend has /analytics/dashboard and others.
    # Marked "(admin)" in the docstring; customer bot doesn't surface admin
    # analytics. Method is unused.
    ('customer_bot', 'GET', '/api/v1/analytics/overview'),
}

# Bot calls follow the shape:
#   self._make_request('GET', '/api/v1/orders', ...)
#   self._make_request('POST', f'/api/v1/auth/addresses/{addr_id}/set-default', ...)
# Both single and double quoted, plain and f-strings.
_VERB_THEN_PATH = re.compile(
    r"""_make_request\s*\(\s*
        ['"](?P<method>GET|POST|PUT|PATCH|DELETE)['"]\s*,\s*
        f?['"](?P<path>/api/v1/[^'"]*)['"]
    """,
    re.VERBOSE,
)


def _normalise_bot_path(path: str) -> str:
    """Replace bot-side f-string placeholders with Flask-style converters.

    Bot writes ``f"/api/v1/orders/{order_id}"``; Flask registers the
    rule as ``/api/v1/orders/<int:order_id>``. Normalise both sides so
    comparison is structural, not literal. We don't care about the
    converter type — only about the *position* of variable segments.

    Also strips query strings (``?lang=ru&...``) — Flask's url_map keys
    only on the path; the bot client appends query params via httpx's
    ``params=`` kwarg in some places and inline in the URL elsewhere.
    """
    path = path.split('?', 1)[0]
    return re.sub(r'\{[^}]+\}', '<var>', path).rstrip('/')


def _normalise_flask_rule(rule: str) -> str:
    """Replace Flask converters with the same placeholder used above."""
    return re.sub(r'<[^>]+>', '<var>', rule).rstrip('/')


def _extract_bot_calls(text: str) -> Set[Tuple[str, str]]:
    """Return ``{(method, normalised_path)}`` pairs from a bot-source file."""
    found: Set[Tuple[str, str]] = set()
    for match in _VERB_THEN_PATH.finditer(text):
        method = match.group('method').upper()
        path = _normalise_bot_path(match.group('path'))
        found.add((method, path))
    return found


def _flask_route_inventory(app) -> Set[Tuple[str, str]]:
    """Return ``{(method, normalised_rule)}`` for every ``/api/v1`` route."""
    inventory: Set[Tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        if not str(rule.rule).startswith('/api/v1/'):
            continue
        normalised = _normalise_flask_rule(str(rule.rule))
        for method in (rule.methods or set()):
            if method in {'HEAD', 'OPTIONS'}:
                continue
            inventory.add((method, normalised))
    return inventory


def _bot_files_present() -> List[Tuple[str, Path]]:
    return [(label, p) for label, p in BOT_API_CLIENTS if p.exists()]


@pytest.mark.contract
@pytest.mark.parametrize('bot_label,bot_file', _bot_files_present(),
                         ids=[label for label, _ in _bot_files_present()])
def test_bot_calls_resolve_to_backend_routes(app, bot_label, bot_file):
    """Every backend route the bot hits must exist on the Flask url_map."""
    text = bot_file.read_text()
    bot_calls = _extract_bot_calls(text)
    assert bot_calls, (
        f"Extracted zero bot API calls from {bot_file}. "
        f"Either the bot stopped using _make_request, or the regex needs an update."
    )

    backend_routes = _flask_route_inventory(app)

    allowlisted_for_label = {
        (method, path)
        for label, method, path in KNOWN_DEAD_BOT_CALLS
        if label == bot_label
    }
    missing: List[Tuple[str, str]] = sorted(bot_calls - backend_routes - allowlisted_for_label)
    if missing:
        # Format the gap with the closest backend match for fast triage.
        backend_paths_by_method = {
            method: sorted({path for m, path in backend_routes if m == method})
            for method in {m for m, _ in backend_routes}
        }
        lines = [f"{bot_label} calls backend routes that don't exist:"]
        for method, path in missing:
            lines.append(f"  {method} {path}")
            same_method = backend_paths_by_method.get(method, [])
            close = [p for p in same_method if path.split('<var>')[0][:25] in p][:3]
            if close:
                lines.append(f"    closest matches: {close}")
        pytest.fail('\n'.join(lines))


@pytest.mark.contract
def test_known_dead_bot_routes_still_dead(app):
    """Allowlist hygiene: every entry in KNOWN_DEAD_BOT_CALLS must still be
    referenced from the bot's api_client (otherwise it's stale) AND still
    not exist on the backend (otherwise the bug is fixed and the entry
    should be removed).

    Stops the allowlist from rotting into a permanent escape hatch.
    """
    backend_routes = _flask_route_inventory(app)
    bot_calls_by_label = {
        label: _extract_bot_calls(p.read_text()) for label, p in _bot_files_present()
    }

    stale: List[str] = []
    for label, method, path in sorted(KNOWN_DEAD_BOT_CALLS):
        bot_calls = bot_calls_by_label.get(label, set())
        if (method, path) not in bot_calls:
            stale.append(
                f"  ({label!r}, {method!r}, {path!r}) — bot no longer calls this; "
                f"remove the allowlist entry"
            )
        elif (method, path) in backend_routes:
            stale.append(
                f"  ({label!r}, {method!r}, {path!r}) — backend now exposes this route; "
                f"remove the allowlist entry"
            )
    if stale:
        pytest.fail(
            "KNOWN_DEAD_BOT_CALLS allowlist contains stale entries:\n"
            + '\n'.join(stale)
        )


@pytest.mark.contract
def test_bot_calls_use_known_methods(app):
    """Each bot call's HTTP method must be accepted by the matching route.

    Catches the case where the bot writes ``POST`` against a route that's
    been changed to ``PUT``-only, etc. Distinct from the test above: that
    one ignores method-mismatches if the rule itself exists for *some*
    method, this one fails on exact (method, rule) mismatch.
    """
    backend_routes = _flask_route_inventory(app)
    backend_paths_any_method = {path for _, path in backend_routes}

    bad: List[Tuple[str, str, str]] = []
    for label, bot_file in _bot_files_present():
        for method, path in _extract_bot_calls(bot_file.read_text()):
            if path in backend_paths_any_method and (method, path) not in backend_routes:
                accepted = sorted(m for m, p in backend_routes if p == path)
                bad.append((label, f"{method} {path}", f"backend accepts {accepted}"))

    if bad:
        lines = ["Bot calls use methods the backend route doesn't accept:"]
        for label, call, hint in bad:
            lines.append(f"  [{label}] {call} -- {hint}")
        pytest.fail('\n'.join(lines))
