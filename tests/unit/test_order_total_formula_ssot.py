"""ONE FORMULA FOR AN ORDER TOTAL, AS A TEST THAT RUNS.

------------------------------------------------------------------------------
WHY THIS FILE EXISTS
------------------------------------------------------------------------------
`subtotal - discount_amount + delivery_fee - loyalty_discount` was re-typed in
TEN production places (design spec 2026-08-27 §4.6). They had already drifted:

  * `OrderService.create_order` omitted `loyalty_discount` entirely.
  * `StaffService.price_phone_order` carried NO discount term at all.
  * `OrderEditService._project_totals_after` (the edit PREVIEW) hardcoded
    `loyalty_discount: 0.0` while `_recompute_totals` (the APPLY) read the
    column — a preview that understated the discount it was about to write.
  * `order_tasks.validate_order_integrity` computed a loyalty-blind total AND
    COMMITTED IT BACK to the row. It was disarmed only by having no caller.

A sixth term added to some but not all of them produces a quoted total that
disagrees with the charged total, per surface. This file makes that
mechanically impossible in the only way that survives the next refactor: it
FAILS THE SUITE when a collapsed site stops calling the shared function, and it
FAILS THE SUITE when a NEW expression of the formula appears anywhere in
`business_app/`.

Modelled on `tests/unit/test_show_vs_settle_invariant.py`, which caught the
same defect family on the client side.

------------------------------------------------------------------------------
THE ONE REMAINING SITE, ON THE RECORD (the IOU)
------------------------------------------------------------------------------
`business_app/static/js/pages/checkout.js` (`renderOrderSummary` :45-91 /
`updateSummaryTotals` :93-158) still sums the basket in client-side floats. It
is NOT scanned here because it is JavaScript, and it is NOT fixed here because
the fix is to render the server's estimate response rather than compute
anything (design spec §5.2) — the web-checkout change, not this one. Until that
lands, the web checkout is the eleventh expression of the formula. This
paragraph is the IOU; delete it when checkout.js stops doing arithmetic.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The module that HOLDS the formula, excluded from the scan in PART 2 for the
# obvious reason: it is the implementation every other site routes through.
_THE_IMPLEMENTATION = "business_app/utils/order_totals.py"


# ===========================================================================
# PART 1 -- every collapsed site still calls the shared function
# ===========================================================================
#
# key   = (importable module, "Class.method")
# value = the call that must appear in that method's source.
#
# `Order.calculate_total` is the only member that calls `compute_order_total`
# on behalf of a persisted row; anything that OWNS an Order goes through IT,
# and anything that prices a basket with no row yet calls the function direct.

_REQUIRED_CALL_SITES: Dict[Tuple[str, str], str] = {
    ("business_app.models.order", "Order.calculate_total"): "compute_order_total(",
    ("business_app.services.order_service", "OrderService.create_order"): "compute_order_total(",
    (
        "business_app.services.order_edit_service",
        "OrderEditService._project_totals_after",
    ): "compute_order_total(",
    (
        "business_app.services.order_edit_service",
        "OrderEditService._recompute_totals",
    ): "order.calculate_total()",
    (
        "business_app.services.loyalty_service",
        "LoyaltyService.apply_reward_to_order",
    ): "compute_order_total(",
    (
        "business_app.services.staff_service",
        "StaffService.price_phone_order",
    ): "compute_order_total(",
    (
        "business_app.services.cart_service",
        "CartService.calculate_cart_estimate",
    ): "compute_order_total(",
    (
        "business_app.services.cart_service",
        "CartService.get_cart_summary",
    ): "compute_order_total(",
    (
        "business_app.services.subscription_service",
        "SubscriptionService.calculate_subscription_preview",
    ): "compute_order_total(",
}


def _has_real_call(src: str, callee_expr: str) -> bool:
    """True if `src` contains an executed `ast.Call` whose callee IS `callee_expr`.

    `required_call` strings look like `"compute_order_total("` or
    `"order.calculate_total()"`; callers pass everything before the first `(`
    as `callee_expr`. Walking `ast.Call` nodes -- and comparing the UNPARSED
    callee expression, not the source text -- is what a substring check on
    `src` cannot do: a decoy comment or a docstring containing the same
    characters never becomes a `Call` node, so it cannot satisfy this.
    """
    tree = ast.parse(textwrap.dedent(src))
    return any(
        isinstance(node, ast.Call) and ast.unparse(node.func) == callee_expr
        for node in ast.walk(tree)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("module_name", "qualname", "required_call"),
    [(m, q, c) for (m, q), c in _REQUIRED_CALL_SITES.items()],
    ids=[q for (_, q) in _REQUIRED_CALL_SITES],
)
def test_collapsed_site_still_calls_the_one_formula(module_name, qualname, required_call):
    """A site that stops delegating has re-opened the drift this phase closed."""
    module = importlib.import_module(module_name)
    cls_name, method_name = qualname.split(".")
    src = inspect.getsource(getattr(getattr(module, cls_name), method_name))
    callee_expr = required_call.split("(", 1)[0]
    assert _has_real_call(src, callee_expr), (
        f"{qualname} no longer goes through `{required_call}`. The order total "
        "is decided in more than one place again — which is how `create_order` "
        "came to omit `loyalty_discount` and the edit preview came to disagree "
        "with the edit it previewed. (A mention inside a comment or a "
        "docstring does not satisfy this -- it checks for an actual call.)"
    )


# ===========================================================================
# PART 2 -- THE GENERATIVE LINT: no NEW expression of the formula
# ===========================================================================
#
# The formula's SHAPE is an add/subtract mixing a BASIS term (a subtotal) with
# an ADJUSTMENT term (a discount or a delivery fee). When this file was
# written, an AST scan of the WHOLE `business_app/` tree for that shape
# returned the spec'd sites and NOTHING ELSE -- zero false positives across the
# entire package. That measured cleanliness is what makes the ledger below
# meaningful: an undeclared hit is a real eleventh expression, not noise.

_BASIS_TERMS = ("subtotal", "basis", "gross", "items_total", "line_total", "total_before")
_ADJUSTMENT_TERMS = ("discount", "delivery_fee", "_fee")
_ARITH_OPS = (ast.Add, ast.Sub)

# A basis term can be renamed away entirely (`gross = self.subtotal` catches
# THAT rename because "gross" is now in `_BASIS_TERMS`, but the next rename
# won't be on the list). The second, independent trigger below closes that:
# an add/sub of a discount/fee term that gets ASSIGNED to something
# total-shaped is formula-shaped no matter what its operands are called.
_TOTAL_ASSIGN_TERMS = ("total_amount", "grand_total", "final_total", "amount_due", "total_before")


# --- THE LEDGER -------------------------------------------------------------
#
# key   = (repo-relative path, enclosing def/class chain, ast.unparse(expr))
#         Line numbers are NOT part of the key -- code moves, and a pin that
#         breaks on an unrelated insertion gets deleted by the next person.
# value = a human's verdict, and WHY it is not an order total.
#
# Exactly two survive, and NEITHER computes a total.

_LEDGER: Dict[Tuple[str, str, str], str] = {
    (
        "business_app/services/order_service.py",
        "OrderService.create_order",
        "subtotal + delivery_fee",
    ): (
        "NOT A TOTAL. The MIN_ORDER_AMOUNT floor is gated on the GROSS basket, "
        "pre-discount, on purpose (design spec §4.4): a subscription discount is "
        "a merchant concession, not a smaller order, and gating on the net would "
        "hard-fail billing for any discounted subscription sitting just above "
        "the floor. Deliberately NOT moved to a net basis."
    ),
    (
        "business_app/services/cart_service.py",
        "CartService.calculate_cart_estimate",
        "items_subtotal + delivery_fee",
    ): (
        "NOT A TOTAL. `total_before_discount` is a published breakdown field the "
        "estimate response carries in its own right. The CHARGED figure in the "
        "same method, `final_total`, goes through `compute_order_total`."
    ),
    (
        "business_app/services/subscription_service.py",
        "SubscriptionService.create_subscription",
        "1 - discount_percentage / 100",
    ): (
        "NOT A TOTAL. This scales `Subscription.billing_amount` -- the "
        "subscription's own recurring PER-CYCLE price, stored on a Subscription "
        "row, not an Order. It carries no delivery fee, no loyalty discount and "
        "no tier discount; the shared formula's parameters do not describe this "
        "entity. The ORDER total for a subscription-generated order is decided "
        "separately, in `OrderService.create_order`: it reads "
        "`subscription.discount_percentage` off the row to derive its own "
        "`discount_amount` and calls `compute_order_total` directly -- the two "
        "expressions never read each other's output, so they cannot drift."
    ),
    (
        "business_app/services/loyalty_service.py",
        "clamp_tier_discount",
        "Decimal(str(subtotal or 0)) - Decimal(str(discount_amount or 0))",
    ): (
        "NOT A TOTAL. This is `headroom` -- an upper BOUND the tier discount "
        "may not exceed, computed so `ck_orders_tier_discount_nonneg` can never "
        "be violated. It never becomes `total_amount`; every caller still "
        "prices the order by passing the clamped `tier_discount` INTO "
        "`compute_order_total`."
    ),
    (
        "business_app/services/loyalty_service.py",
        "clamp_tier_discount",
        "Decimal(str(subtotal or 0)) - Decimal(str(discount_amount or 0)) - "
        "Decimal(str(loyalty_discount or 0))",
    ): (
        "NOT A TOTAL. Same `headroom` bound as above; the AST scanner also "
        "reports the outer subtraction of the chained expression as its own "
        "node. See the verdict directly above."
    ),
}


def _leaf_terms(node: ast.AST) -> List[str]:
    """Every identifier-ish token in an expression: names, attrs, string keys."""
    out: List[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.append(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.append(sub.attr)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return [t.lower() for t in out]


def _is_formula_shaped(node: ast.AST, assign_terms: Tuple[str, ...] = ()) -> bool:
    terms = _leaf_terms(node)
    has_basis = any(any(b in t for b in _BASIS_TERMS) for t in terms)
    has_adjustment = any(any(a in t for a in _ADJUSTMENT_TERMS) for t in terms)
    assigned_to_total = any(any(w in t for w in _TOTAL_ASSIGN_TERMS) for t in assign_terms)
    return (has_basis or assigned_to_total) and has_adjustment


class _FormulaScanner(ast.NodeVisitor):
    def __init__(self, relpath: str) -> None:
        self.relpath = relpath
        self._scope: List[str] = []
        # Stack of leaf terms from the target of the innermost enclosing
        # `Assign` this BinOp sits inside, e.g. `self.total_amount = <expr>`
        # pushes ("self", "total_amount"). Empty outside any assignment.
        self._assign_stack: List[Tuple[str, ...]] = []
        self.hits: List[Tuple[Tuple[str, str, str], int]] = []

    def _enter(self, node):
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter
    visit_ClassDef = _enter

    @staticmethod
    def _target_terms(targets: List[ast.AST]) -> Tuple[str, ...]:
        terms: List[str] = []
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    terms.append(sub.id.lower())
                elif isinstance(sub, ast.Attribute):
                    terms.append(sub.attr.lower())
        return tuple(terms)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assign_stack.append(self._target_terms(node.targets))
        self.generic_visit(node)
        self._assign_stack.pop()

    def visit_BinOp(self, node: ast.BinOp) -> None:
        assign_terms = self._assign_stack[-1] if self._assign_stack else ()
        if isinstance(node.op, _ARITH_OPS) and _is_formula_shaped(node, assign_terms):
            key = (self.relpath, ".".join(self._scope), ast.unparse(node))
            self.hits.append((key, node.lineno))
        self.generic_visit(node)


def _scan_backend() -> List[Tuple[Tuple[str, str, str], int]]:
    hits: List[Tuple[Tuple[str, str, str], int]] = []
    for path in sorted((REPO_ROOT / "business_app").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == _THE_IMPLEMENTATION:
            continue
        scanner = _FormulaScanner(rel)
        scanner.visit(ast.parse(path.read_text(encoding="utf-8")))
        hits.extend(scanner.hits)
    return hits


@pytest.mark.unit
def test_no_undeclared_order_total_arithmetic():
    """THE ELEVENTH-EXPRESSION DETECTOR.

    Every add/subtract in `business_app/` that mixes a subtotal with a discount
    or a delivery fee must carry a written verdict in `_LEDGER`. A new one --
    a new quote surface, a new "amount due" line, a total recomputed "just to be
    safe" -- fails here with the question attached: is this the ORDER TOTAL, and
    if so why is it not `compute_order_total`?
    """
    hits = _scan_backend()
    undeclared = sorted(
        {key for key, _ in hits if key not in _LEDGER},
        key=lambda k: (k[0], k[1], k[2]),
    )
    if undeclared:
        lines = []
        for relpath, scope, expr in undeclared:
            lineno = next(ln for key, ln in hits if key == (relpath, scope, expr))
            lines.append(f"  {relpath}:{lineno}  [{scope}]  {expr}")
        pytest.fail(
            "AN ORDER TOTAL IS BEING COMPUTED SOMEWHERE THAT IS NOT "
            "`business_app/utils/order_totals.py`.\n\n"
            + "\n".join(lines)
            + "\n\nBefore adding a line to `_LEDGER` in "
            f"{__file__}, answer:\n"
            "  1. Is this the amount a customer is CHARGED? Then call "
            "`compute_order_total` and delete the expression.\n"
            "  2. Is it a breakdown field or a gate that is deliberately NOT "
            "the total (the gross MIN_ORDER floor is one)? Then say so, and say "
            "which expression IS the total for that surface.\n"
            "A ledger entry that says 'looks fine' is how ten of these "
            "accumulated."
        )
    assert hits, "the scanner found nothing at all — it has stopped working"


@pytest.mark.unit
def test_the_ledger_does_not_rot():
    """A ledger entry for code that no longer exists stops meaning anything."""
    seen = {key for key, _ in _scan_backend()}
    stale = sorted(set(_LEDGER) - seen, key=lambda k: (k[0], k[1], k[2]))
    assert not stale, (
        "these _LEDGER entries no longer match any code — the expression was "
        "changed, moved or removed. Delete them (or re-key them) so the ledger "
        "keeps describing the repo that exists:\n"
        + "\n".join(f"  {p}  [{s}]  {e}" for p, s, e in stale)
    )


# ===========================================================================
# PART 3 -- deletions that must stay deleted
# ===========================================================================


@pytest.mark.unit
def test_validate_order_integrity_is_gone():
    """It computed `subtotal + delivery_fee - discount_amount` -- loyalty-blind
    -- and COMMITTED it back to `order.total_amount`. Its `and` short-circuit
    meant it ran to completion for any non-DELIVERED order; it raised
    AttributeError only on DELIVERED ones, via `order.delivered_at`, a column
    that lives on `Delivery`, not `Order`. No beat entry, no caller, no test:
    scheduling it once would have corrupted every rewarded order."""
    order_tasks = importlib.import_module("business_app.tasks.order_tasks")

    assert not hasattr(order_tasks, "validate_order_integrity"), (
        "`validate_order_integrity` is back. It writes a total that omits "
        "`loyalty_discount`. If order integrity needs validating, the check "
        "must compare against `compute_order_total`."
    )


@pytest.mark.unit
def test_the_hardcoded_tier_discount_table_is_gone():
    """`product_serializers` held a THIRD tier table -- {bronze:0, silver:2,
    gold:5, platinum:10} plus a 5% `is_vip` bump -- keyed on attributes `User`
    does not have, so both branches were inert. `calculate_product_price` IS
    wired (serialize_product:356), so the day anyone added a `loyalty_tier`
    attribute to `User` it would have fired silently at percentages that
    disagree with the database (design spec §2)."""
    src = inspect.getsource(
        importlib.import_module("business_app.serializers.product_serializers")
    )
    for token in ("tier_discounts", "is_vip", "loyalty_tier"):
        assert token not in src, (
            f"`{token}` is back in product_serializers.py. The ONLY source of a "
            "tier rate is `LoyaltyTierConfig.discount_percentage`, read live — "
            "never a literal in code, and never applied to a catalogue price."
        )
