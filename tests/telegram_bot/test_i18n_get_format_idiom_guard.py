"""``i18n.get(...).format(...)`` must not exist in either bot. A lint, not a unit test.

WHY THIS FILE EXISTS
--------------------
``shared/i18n_rendering.render_translation`` — the one rendering rule both bots
now share — guarantees that ``i18n.get()`` NEVER hands back a string with an
unresolved ``{...}`` in it. When the caller passes no values, a template that
still carries a replacement field is treated as broken copy and degraded to the
humanised key ("Now using", "Joined session").

That guarantee silently breaks every call site that kept the old two-step shape:

    text = i18n.get('telegram.language.now_using', lang).format(language=name)

``get()`` returns ``"Now using"``; ``.format()`` then finds no placeholder to
fill, succeeds, and returns ``"Now using"``. No exception, no log line, no
failing test — just a customer reading a sentence with the important half
missing. Five live sites shipped exactly that (the language-switch confirmation
screen, and three staff-bot co-driver screens that dropped the driver's NAME).

The fix is always the same: pass the values INTO ``get()``. This lint is what
stops the class coming back, because the broken form is indistinguishable from
the working one at a glance and produces no runtime signal at all.

WHY AN AST WALK AND NOT A GREP
------------------------------
The worst of the five sites spanned two lines — the ``get()`` result was bound
to a name and formatted on the NEXT statement — so a line-oriented regex would
have missed the one that mattered most. The visitor below tracks names bound to
an ``i18n.get(...)`` call and flags ``.format()`` on them too.

``test_the_detector_catches_every_shape_the_idiom_has_taken`` is the positive
control: it replays the five real pre-fix snippets through the detector. Without
it this file could rot into a lint that detects nothing and passes forever.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Both bots, because the rule they now share lives in `shared/` and a call site
# in either tree breaks it the same way.
BOT_TREES = ("telegram_bot", "staff_bot")


class Violation(NamedTuple):
    path: str
    lineno: int
    how: str
    source: str

    def __str__(self) -> str:  # pragma: no cover - only rendered on failure
        return f"{self.path}:{self.lineno} [{self.how}] {self.source}"


def _is_i18n_get(node: ast.AST) -> bool:
    """True for ``i18n.get(...)`` / ``self.i18n.get(...)`` / ``mod.i18n.get(...)``.

    Matched on the RECEIVER rather than on an import alias so a call reached
    through any of the several spellings both bots use still counts. Receivers
    that merely happen to expose ``.get`` (``context.user_data.get``,
    ``response.data.get``) do not mention i18n and are left alone.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "get":
        return False
    try:
        receiver = ast.unparse(func.value)
    except Exception:  # pragma: no cover - defensive
        return False
    return "i18n" in receiver.lower()


class _GetThenFormatVisitor(ast.NodeVisitor):
    """Collects both shapes of the idiom in one module."""

    def __init__(self, path: str, source_lines: list[str]):
        self.path = path
        self.source_lines = source_lines
        self.violations: list[Violation] = []
        # name -> line it was bound to an i18n.get(...) call.
        self.bound_to_translation: dict[str, int] = {}

    # -- name binding ------------------------------------------------------
    def _remember(self, target: ast.AST, value: ast.AST, lineno: int) -> None:
        if isinstance(target, ast.Name) and _is_i18n_get(value):
            self.bound_to_translation[target.id] = lineno

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._remember(target, node.value, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._remember(node.target, node.value, node.lineno)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._remember(node.target, node.value, node.lineno)
        self.generic_visit(node)

    # -- the idiom ---------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "format":
            if _is_i18n_get(func.value):
                self._record(node, "i18n.get(...).format(...)")
            elif isinstance(func.value, ast.Name) and func.value.id in self.bound_to_translation:
                bound_at = self.bound_to_translation[func.value.id]
                self._record(
                    node,
                    f"{func.value.id} = i18n.get(...) on line {bound_at}, "
                    f"then {func.value.id}.format(...)",
                )
        self.generic_visit(node)

    def _record(self, node: ast.Call, how: str) -> None:
        line = ""
        if 0 < node.lineno <= len(self.source_lines):
            line = self.source_lines[node.lineno - 1].strip()
        self.violations.append(Violation(self.path, node.lineno, how, line))


def find_get_then_format(source: str, path: str = "<memory>") -> list[Violation]:
    """Every ``.format()`` applied to the result of an ``i18n.get()`` call."""
    tree = ast.parse(source)
    visitor = _GetThenFormatVisitor(path, source.splitlines())
    visitor.visit(tree)
    return sorted(visitor.violations, key=lambda v: (v.path, v.lineno))


def _bot_modules() -> list[Path]:
    modules: list[Path] = []
    for tree in BOT_TREES:
        root = REPO_ROOT / tree
        assert root.is_dir(), f"{tree}/ is missing — this lint would silently scan nothing"
        modules.extend(
            path for path in sorted(root.rglob("*.py"))
            if "__pycache__" not in path.parts
        )
    return modules


# ---------------------------------------------------------------------------
# Positive control: the five real sites, exactly as they were written
# ---------------------------------------------------------------------------
# Verbatim pre-fix snippets. If the detector stops recognising any of them it is
# no longer guarding anything, and the lint below would pass vacuously.
KNOWN_HISTORICAL_SITES = {
    # telegram_bot/handlers/language.py — the two-LINE form. The customer who
    # had just switched language read "✅ Now using" with no language name.
    "language.py (two-line, via a bound name)": """
def confirm(i18n, language_code, language_name):
    now_using_template = i18n.get('telegram.language.now_using', language_code)
    now_using_text = now_using_template.format(
        language=language_name,
        language_name=language_name,
    )
    return now_using_text
""",
    # telegram_bot/handlers/__init__.py — inside a call argument.
    "handlers/__init__.py (inline argument)": """
async def handle(self, update, context, text):
    await self._send_response(
        update,
        i18n.get('telegram.support.message_received', language).format(message=text)
    )
""",
    # staff_bot/handlers/delivery/bottle_session.py:201 — inside an f-string.
    "bottle_session.py joined_session (inside an f-string)": """
def joined(i18n, language, owner_name):
    return f"{i18n.get('staff.bottles.joined_session', language).format(name=owner_name)}"
""",
    # staff_bot/handlers/delivery/bottle_session.py:294 — two values dropped.
    "bottle_session.py current_membership (multi-line call)": """
def membership(i18n, language, owner_name, inventory):
    return i18n.get('staff.bottles.current_membership', language).format(
        name=owner_name, qty=inventory
    )
""",
    # staff_bot/handlers/delivery/bottle_session.py:478.
    "bottle_session.py codriver_invited": """
def invited(i18n, language, member_name):
    return i18n.get('staff.bottles.codriver_invited', language).format(name=member_name)
""",
}


@pytest.mark.unit
@pytest.mark.parametrize("label", sorted(KNOWN_HISTORICAL_SITES))
def test_the_detector_catches_every_shape_the_idiom_has_taken(label):
    """Each of the five sites that actually shipped is detected."""
    violations = find_get_then_format(KNOWN_HISTORICAL_SITES[label], label)
    assert violations, (
        f"the detector no longer recognises the {label} shape — this lint has "
        "gone blind and the guard below is worthless"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "label, source",
    [
        (
            "the fix: values passed INTO get()",
            "text = i18n.get('telegram.language.now_using', lang, language=name)\n",
        ),
        (
            "a plain literal template",
            "text = 'Hello {name}'.format(name=name)\n",
        ),
        (
            "a dict lookup that merely spells .get",
            "text = context.user_data.get('template', '').format(name=name)\n",
        ),
        (
            "a name bound to something that is not a translation",
            "row = response.data.get('label')\ntext = row.format(name=name)\n",
        ),
    ],
)
def test_the_detector_leaves_correct_code_alone(label, source):
    """A lint that flags the fix would push people back to the broken form."""
    assert find_get_then_format(source, label) == [], label


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_no_bot_module_formats_the_result_of_i18n_get():
    """``get()`` no longer returns a template — nothing may format its result.

    Whatever this reports is a screen that is CURRENTLY dropping the values it
    means to show. Fix it by passing those values to ``i18n.get(key, language,
    **values)``; do not add an allowlist.
    """
    violations: list[Violation] = []
    for module in _bot_modules():
        source = module.read_text(encoding="utf-8")
        violations.extend(
            find_get_then_format(source, str(module.relative_to(REPO_ROOT)))
        )

    assert not violations, (
        "i18n.get(...) no longer returns a fillable template — these call sites "
        "are formatting the HUMANISED KEY and silently dropping their values:\n  "
        + "\n  ".join(str(v) for v in violations)
    )
