"""SEC-002: lint test that every staff-bot handler carries a role-guard decorator.

Telegram callback queries can be replayed by a user whose staff role has been
revoked since the conversation started. The defense is a require_* decorator
on EVERY state-mutating handler so each call re-checks the role from
``context.user_data['staff_roles']``.

This test AST-walks ``staff_bot/handlers/`` and fails if any async handler
function (signature: ``(self_or_update, [context], ...)``) is missing one of
the guard decorators. Auth-flow handlers that legitimately run pre-login are
declared in ``ALLOWLIST`` below — extending the allowlist requires a code
review and a justification comment.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, List, Tuple

# Decorator names that count as a valid guard. Adding a new one (e.g. for a
# new role) requires updating this set.
GUARD_DECORATORS = {
    "require_role",
    "require_delivery_driver",
    "require_operator",
    "require_any_staff_role",
    "require_auth",
}

# Handlers that legitimately have no guard. Each entry MUST be justified —
# typically an entry-point that runs before the user is authenticated.
ALLOWLIST: set[Tuple[str, str]] = {
    # /start command — entry point, runs before any auth state exists.
    ("staff_bot/handlers/start.py", "start"),
    # Language picker shown during the /start conversation, before auth.
    ("staff_bot/handlers/start.py", "language_selected"),
    # /cancel inside the start ConversationHandler.
    ("staff_bot/handlers/start.py", "cancel"),
}

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDLERS_DIR = REPO_ROOT / "staff_bot" / "handlers"


def _decorator_names(decorators: Iterable[ast.expr]) -> List[str]:
    """Extract the decorator name (e.g. 'require_role' from @require_role(...))."""
    names: List[str] = []
    for d in decorators:
        if isinstance(d, ast.Call):
            f = d.func
            if isinstance(f, ast.Attribute):
                names.append(f.attr)
            elif isinstance(f, ast.Name):
                names.append(f.id)
        elif isinstance(d, ast.Name):
            names.append(d.id)
        elif isinstance(d, ast.Attribute):
            names.append(d.attr)
    return names


def _is_handler_function(node: ast.AST) -> bool:
    """Heuristic: async function whose first arg is ``update`` or ``self``."""
    if not isinstance(node, ast.AsyncFunctionDef):
        return False
    args = node.args.args
    if not args:
        return False
    arg_names = [a.arg for a in args]
    return arg_names[0] in ("update", "self") and (
        "update" in arg_names or "context" in arg_names
    )


def _walk_handler_files() -> List[Tuple[str, ast.AsyncFunctionDef]]:
    """Yield (relative_path, function_node) for every handler in staff_bot/handlers/."""
    found: List[Tuple[str, ast.AsyncFunctionDef]] = []
    for path in sorted(HANDLERS_DIR.rglob("*.py")):
        if path.name.startswith("__"):
            continue
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if _is_handler_function(node):
                found.append((rel, node))
    return found


def test_every_staff_handler_has_role_guard() -> None:
    """Every staff handler must carry a require_* decorator (or be allowlisted)."""
    unguarded: List[str] = []

    for rel_path, node in _walk_handler_files():
        # Skip private helpers (convention: leading underscore = internal use).
        if node.name.startswith("_"):
            continue
        if (rel_path, node.name) in ALLOWLIST:
            continue
        decs = set(_decorator_names(node.decorator_list))
        if not (decs & GUARD_DECORATORS):
            unguarded.append(f"  {rel_path}:{node.lineno}  {node.name}  decorators={sorted(decs)}")

    assert not unguarded, (
        "SEC-002: the following staff-bot handlers are missing a require_* "
        "guard decorator. Either add the appropriate decorator OR justify "
        "the exception by adding the (path, name) tuple to ALLOWLIST in this "
        "test (with a comment explaining why).\n\n"
        + "\n".join(unguarded)
    )


def test_allowlist_entries_are_real() -> None:
    """Every ALLOWLIST entry must point to an actually-existing handler.

    Catches bitrot: a handler is renamed/deleted, the allowlist entry is left
    behind, and a future handler accidentally inherits the exemption.
    """
    handlers_by_path: dict[str, set[str]] = {}
    for rel_path, node in _walk_handler_files():
        handlers_by_path.setdefault(rel_path, set()).add(node.name)

    stale: List[str] = []
    for rel_path, name in ALLOWLIST:
        if rel_path not in handlers_by_path:
            stale.append(f"  ALLOWLIST entry ({rel_path}, {name}) — file doesn't exist")
        elif name not in handlers_by_path[rel_path]:
            stale.append(f"  ALLOWLIST entry ({rel_path}, {name}) — function doesn't exist in that file")

    assert not stale, (
        "ALLOWLIST contains entries that no longer match any handler. Remove "
        "them so the allowlist accurately reflects intentional exceptions:\n\n"
        + "\n".join(stale)
    )
