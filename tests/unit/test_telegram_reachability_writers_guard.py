"""Only TelegramReachabilityService writes `users.is_bot_active` and `users.telegram_blocked_at`
(spec 2026-10-09 §7.2 "one writer", §15.5).

The two fields decide the customer's channel (`NotificationService._resolve_telegram_else_email`)
and whether a reminder may go at all. Before §7.2 four places set `is_bot_active = True` inline and
none ever set it False, so a customer who blocked the bot stayed "connected" for ever; a fifth
inline writer would re-open that, because a login or a merge that sets the flag without the block
timestamp undoes a block the service is holding.

An AST walk, not a grep: reads (`user.is_bot_active`, `"is_bot_active": user.is_bot_active` in a
serializer, `User.is_bot_active == False` in a filter) never trip it, while every way Python or SQL
can WRITE the column does: attribute assignment, a `User(...)` keyword, a bulk `.update({...})` or
`.values(...)`, `setattr`, and a SQL `SET` inside a string.
"""

import ast
import inspect
import re
from pathlib import Path

import pytest

from business_app.services.auth_service import AuthService

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
FIELDS = frozenset({"is_bot_active", "telegram_blocked_at"})
SCANNED_TREES = ("business_app", "shared", "telegram_bot", "staff_bot")
SKIPPED_PARTS = frozenset({"migrations", "__pycache__"})
THE_WRITER = Path("business_app/services/telegram_reachability_service.py")
SQL_WRITE = re.compile(r"\bSET\b[^;]*?\b(?:is_bot_active|telegram_blocked_at)\s*=(?!=)", re.IGNORECASE | re.DOTALL)


def _called_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _key_name(key):
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    if isinstance(key, ast.Attribute):
        return key.attr
    return None


def writes(source: str):
    """Every (line, form) in `source` that writes one of FIELDS."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        else:
            targets = []
        for target in targets:
            for leaf in ast.walk(target):
                if isinstance(leaf, ast.Attribute) and leaf.attr in FIELDS:
                    found.append((node.lineno, f".{leaf.attr} ="))
        if isinstance(node, ast.Call):
            name = _called_name(node.func)
            if name in ("User", "values"):
                found.extend((node.lineno, f"{name}({kw.arg}=)") for kw in node.keywords if kw.arg in FIELDS)
            if name == "update" and node.args and isinstance(node.args[0], ast.Dict):
                found.extend(
                    (node.lineno, f".update({_key_name(key)!r})")
                    for key in node.args[0].keys
                    if _key_name(key) in FIELDS
                )
            if name == "setattr" and len(node.args) >= 2 and _key_name(node.args[1]) in FIELDS:
                found.append((node.lineno, f"setattr({_key_name(node.args[1])!r})"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL_WRITE.search(node.value):
            found.append((node.lineno, "SQL SET"))
    return found


def _scanned_files():
    for tree in SCANNED_TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            relative = path.relative_to(ROOT)
            if SKIPPED_PARTS & set(relative.parts):
                continue
            yield relative, path


def test_only_the_reachability_service_writes_the_two_fields():
    offenders = [
        (str(relative), line, form)
        for relative, path in _scanned_files()
        if relative != THE_WRITER
        for line, form in writes(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        "Write is_bot_active / telegram_blocked_at through TelegramReachabilityService "
        f"(spec 2026-10-09 §7.2): {offenders}"
    )


def test_the_service_is_where_the_writes_are():
    assert writes((ROOT / THE_WRITER).read_text(encoding="utf-8")), "the scan sees no writer at all"


@pytest.mark.parametrize(
    "snippet",
    [
        "user.is_bot_active = True",
        "web_user.telegram_blocked_at = None",
        "User(telegram_id='1', is_bot_active=True)",
        "User.query.filter(x).update({'is_bot_active': False})",
        "User.query.filter(x).update({User.telegram_blocked_at: None})",
        "update(User).values(is_bot_active=False)",
        "setattr(user, 'is_bot_active', True)",
        "QUERY = 'UPDATE users SET bot_state = $1, is_bot_active = false WHERE telegram_id = $2'",
    ],
    ids=["attribute", "attribute-blocked-at", "constructor", "bulk-update-key", "bulk-update-column", "values", "setattr", "sql"],
)
def test_the_scan_sees_every_way_to_write(snippet):
    assert writes(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "active = user.is_bot_active",
        "payload = {'is_bot_active': user.is_bot_active}",
        "User.query.filter(User.is_bot_active == False)",
        "connected = bool(getattr(user, 'is_bot_active', False))",
        "QUERY = 'SELECT telegram_blocked_at FROM users WHERE telegram_id = $1'",
    ],
    ids=["read", "serializer", "filter", "getattr", "sql-read"],
)
def test_the_scan_ignores_reads(snippet):
    assert writes(snippet) == []


def test_register_user_no_longer_accepts_is_bot_active_as_a_kwarg():
    """§7.2 d: `is_bot_active` leaves the register_user kwargs whitelist."""
    assert '"is_bot_active"' not in inspect.getsource(AuthService.register_user)
