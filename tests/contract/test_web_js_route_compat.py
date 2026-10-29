"""Website JS ↔ backend route compatibility (plan 2026-10-26 Task 4a; spec 2026-10-26 §10).

Every `apiRequest(...)` call in `business_app/static/js/**` names a backend route: base.js prefixes the path
with `API_BASE_URL` (`<root>/api/v1`). This test extracts each call statically (the first argument and the
`method:` of its options) and asserts that the route and method exist on the Flask url_map, the website's
twin of `test_bot_backend_route_compat.py`. It caught the old /my-subscriptions calling
`/subscriptions/upcoming-deliveries` and `/subscriptions/<id>/modify`, which never existed.

Paths are compared structurally: a string piece is kept, anything else (a variable, a call) is one `<var>`
segment, a query string is dropped, and a trailing slash does not matter. A call whose path is built
elsewhere (a variable or a helper) is listed in DYNAMIC_CALLS with the routes it can resolve to, so it is
still checked.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_JS = REPO_ROOT / "business_app" / "static" / "js"

# (file relative to static/js, first-argument source) -> the (method, path) pairs it resolves to.
DYNAMIC_CALLS: Dict[Tuple[str, str], Set[Tuple[str, str]]] = {
    # address-modal.js saveAddress: `url` is '/addresses/' + id (PUT) or '/addresses/' (POST).
    ("address-modal.js", "url"): {("PUT", "/addresses/<var>"), ("POST", "/addresses")},
    # subscription-constructor.js: '/delivery/time-slots?weekday=..' and '/payments/methods?context=..'.
    ("subscription-constructor.js", "this.timeSlotsQuery()"): {("GET", "/delivery/time-slots")},
    ("subscription-constructor.js", "this.paymentMethodsQuery()"): {("GET", "/payments/methods")},
}

# Calls to routes that do not exist, found by this test on its first run and outside this change's scope.
# Each is (file, method, normalised path). Prefer fixing the caller to adding an entry.
KNOWN_DEAD_WEB_CALLS: Set[Tuple[str, str, str]] = {
    # contact.js submits the contact form to /api/v1/contact; no such API route exists (the form fails).
    ("pages/contact.js", "POST", "/contact"),
    # my-account.js reads /api/v1/orders/summary; the backend serves /api/v1/auth/orders/summary.
    ("pages/my-account.js", "GET", "/orders/summary"),
    # "Reorder" in the order modal and on /my-orders posts /orders/<id>/reorder; the backend has
    # /orders/quick-reorder and /orders/reorder-info only.
    ("pages/order-detail-modal.js", "POST", "/orders/<var>/reorder"),
    ("pages/orders.js", "POST", "/orders/<var>/reorder"),
}

_CALL = re.compile(r"\bapiRequest\s*\(")
_METHOD = re.compile(r"""\bmethod\s*:\s*['"`](GET|POST|PUT|PATCH|DELETE)['"`]""", re.I)


def _call_args(source: str, start: int) -> str:
    """The text between `apiRequest(` and its closing parenthesis, skipping strings."""
    depth, i, quote = 1, start, None
    while i < len(source):
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return source[start:i]
        i += 1
    raise AssertionError("unbalanced apiRequest( call")


def _first_argument(args: str) -> str:
    depth, quote = 0, None
    for i, ch in enumerate(args):
        if quote:
            if ch == quote and args[i - 1] != "\\":
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return args[:i].strip()
    return args.strip()


_PIECE = re.compile(r"""'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|`((?:[^`\\]|\\.)*)`""")


def _split_plus(expression: str) -> List[str]:
    """Top-level `+` operands of a JS expression."""
    parts, depth, quote, current = [], 0, None, ""
    for i, ch in enumerate(expression):
        if quote:
            current += ch
            if ch == quote and expression[i - 1] != "\\":
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "+" and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += ch
    parts.append(current.strip())
    return parts


def _path_of(expression: str):
    """The normalised path of a literal-built first argument, or None when it is built elsewhere."""
    operands = _split_plus(expression)
    if not any(_PIECE.fullmatch(op) for op in operands):
        return None
    path = ""
    for op in operands:
        literal = _PIECE.fullmatch(op)
        if literal:
            piece = next(group for group in literal.groups() if group is not None)
            if op.startswith("`"):
                piece = re.sub(r"\$\{[^}]*\}", "<var>", piece)
            path += piece
        else:
            path += "<var>"
    path = path.split("?", 1)[0]
    # A variable glued to a path segment (no "/" before it) is a query suffix, e.g. '/x' + (q ? '?a=' + q : '').
    path = re.sub(r"(?<=[^/])<var>$", "", path)
    return path.rstrip("/") or "/"


def _web_calls() -> List[Tuple[str, str, str, str]]:
    """(file, method, normalised path, first argument) for every apiRequest call in the website JS."""
    calls = []
    for js in sorted(WEB_JS.rglob("*.js")):
        source = js.read_text("utf-8")
        relative = js.relative_to(WEB_JS).as_posix()
        for match in _CALL.finditer(source):
            if source[max(0, match.start() - 9) : match.start()].endswith("function "):
                continue  # the definition in base.js
            args = _call_args(source, match.end())
            first = _first_argument(args)
            if not first:
                continue
            method = (_METHOD.search(args[len(first) :]) or [None, "GET"])[1].upper()
            path = _path_of(first)
            if path is None:
                key = (js.name, first)
                assert key in DYNAMIC_CALLS, (
                    f"{relative}: apiRequest({first}, …) builds its path elsewhere; list what it resolves to in "
                    "DYNAMIC_CALLS"
                )
                for dynamic_method, dynamic_path in DYNAMIC_CALLS[key]:
                    calls.append((relative, dynamic_method, dynamic_path, first))
                continue
            calls.append((relative, method, path, first))
    return calls


def _route_inventory(app) -> Set[Tuple[str, str]]:
    inventory = set()
    for rule in app.url_map.iter_rules():
        if not str(rule.rule).startswith("/api/v1/"):
            continue
        normalised = re.sub(r"<[^>]+>", "<var>", str(rule.rule))[len("/api/v1") :].rstrip("/") or "/"
        for method in rule.methods or ():
            if method not in {"HEAD", "OPTIONS"}:
                inventory.add((method, normalised))
    return inventory


def test_the_extractor_reads_the_call_shapes_the_website_uses():
    assert _path_of("'/orders/' + orderId + '/cancel'") == "/orders/<var>/cancel"
    assert _path_of("`/addresses/${addressId}/set-default`") == "/addresses/<var>/set-default"
    assert _path_of("'/loyalty/history' + (filter ? '?type=' + filter : '')") == "/loyalty/history"
    assert _path_of("'/subscriptions/' + id + '/history?page=' + page + '&per_page=' + size") == (
        "/subscriptions/<var>/history"
    )
    assert _path_of("LIST_URL") is None


def _constant_path(relative: str, name: str) -> str:
    """The path a page script keeps in a `var NAME = '…'` constant, normalised like a literal."""
    constant = re.search(rf"var {name} = '([^']+)'", (WEB_JS / relative).read_text("utf-8"))
    assert constant, f"{relative}: {name} moved; update DYNAMIC_CALLS"
    return _path_of(f"'{constant.group(1)}'")


# my-subscriptions.js keeps its read URLs in constants (the fixture generators read the same values): the list,
# the payment menu (its address appended), the product picker, and the slots query built from SLOTS_URL.
DYNAMIC_CALLS[("my-subscriptions.js", "LIST_URL")] = {("GET", _constant_path("pages/my-subscriptions.js", "LIST_URL"))}
DYNAMIC_CALLS[("my-subscriptions.js", "METHODS_URL + sub.delivery_address_id")] = {
    ("GET", _constant_path("pages/my-subscriptions.js", "METHODS_URL"))
}
DYNAMIC_CALLS[("my-subscriptions.js", "PRODUCTS_URL")] = {
    ("GET", _constant_path("pages/my-subscriptions.js", "PRODUCTS_URL"))
}
DYNAMIC_CALLS[("my-subscriptions.js", "query")] = {("GET", _constant_path("pages/my-subscriptions.js", "SLOTS_URL"))}


@pytest.mark.contract
def test_every_website_api_call_resolves_to_a_backend_route(app):
    calls = _web_calls()
    assert len(calls) > 40, "extracted too few apiRequest calls: the extractor regex needs an update"
    routes = _route_inventory(app)
    missing = sorted(
        (file, method, path)
        for file, method, path, _ in calls
        if (method, path) not in routes and (file, method, path) not in KNOWN_DEAD_WEB_CALLS
    )
    assert not missing, "website JS calls routes the backend does not have:\n" + "\n".join(
        f"  {file}: {method} /api/v1{path}" for file, method, path in missing
    )


def test_every_known_dead_call_is_still_made():
    """An allowlist entry whose call is gone must go too."""
    made = {(file, method, path) for file, method, path, _ in _web_calls()}
    assert KNOWN_DEAD_WEB_CALLS <= made, KNOWN_DEAD_WEB_CALLS - made
