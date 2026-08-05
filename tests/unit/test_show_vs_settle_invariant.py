"""THE SHOW-VS-SETTLE SWEEP, AS A TEST THAT RUNS.

------------------------------------------------------------------------------
WHY THIS FILE EXISTS
------------------------------------------------------------------------------
A 4-lens review found FIVE instances of one defect, then a manual sweep of 34
surfaces (`.superpowers/sdd/2026-07-29-plan-e-cod-place-attribution/
SHOW-VS-SETTLE-SWEEP.md`) found three more. The defect is always the same shape:

    A MONETARY FIGURE SHOWN TO A HUMAN, AND THE AMOUNT OR SCOPE POSTED TO THE
    ENGINE, ARE DECIDED BY TWO INDEPENDENT EXPRESSIONS.

One of the eight was misallocating real money. **Not one was caught by a test,
in a suite of 8,000+**, because component tests assert return values and the
fixtures that reach them agree by construction.

A sweep is a document. A document does not run, so it cannot catch the ninth.
This file is that sweep re-expressed as executable pins.

------------------------------------------------------------------------------
THE SEARCH KEY (SHEET §6, verbatim)
------------------------------------------------------------------------------
    "A payload that does not publish the number the screen needs forces the
     client to compute it, and a second expression is born."

`Cart.to_dict()` published no total -> the customer bot summed its own.
`serialize_product` published no `price` -> the operator bot rendered `0`.
The reconciliation submit posted no amount -> the server recomputed the handoff.

That key is MECHANICALLY DETECTABLE, and detecting it is what
`test_no_undeclared_client_side_money_arithmetic` does: every arithmetic
expression over a money-named term, inside the two bot trees, must appear in
the LEDGER below with a human's verdict on it. A new one fails the suite.

------------------------------------------------------------------------------
THE STANDARD (what "one decision" means here)
------------------------------------------------------------------------------
`OrderCashEditService.apply_edit` re-enters `self.preview(...)`, and the preview
replays the event's FROZEN `AllocationScope.from_event`. The figure shown and
the set settled are THE SAME OBJECT BY CONSTRUCTION -- there is nothing that
could drift. `test_the_standard_is_still_the_standard` pins that, because every
other verdict in this file is graded against it.

Weaker but accepted grades, from the sweep:
  * ONE-DECISION (object)            -- both halves read one returned object.
  * ONE-DECISION (shared expression) -- one function, called twice.
  * ONE-DECISION (re-run)            -- one function, re-run on re-read state.
  * SPLIT                            -- two expressions. The defect.

------------------------------------------------------------------------------
WHAT THIS FILE DELIBERATELY DOES NOT DO
------------------------------------------------------------------------------
It does not re-assert the sweep's conclusions -- that would be decorative. Every
test here fails on a state the repo could reach TOMORROW, and any
`xfail(strict=True)` pin at the bottom fails on the state the repo is in TODAY
(a live defect, reported and NOT fixed in this file). When one is fixed, the pin
is un-xfailed in the SAME change -- `strict=True` guarantees it cannot be
forgotten -- and its `_LEDGER` verdict is rewritten or deleted with it. Sweep #7
(the cart screen) went through exactly that on 2026-08-05.
"""
from __future__ import annotations

import ast
import inspect
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ===========================================================================
# PART 1 -- THE GENERATIVE LINT: client-side money arithmetic in the bots
# ===========================================================================
#
# A bot handler is a SCREEN. Money arithmetic inside one is, by definition, a
# second expression for a figure the server already knows -- which is the whole
# defect family. Sometimes it is unavoidable (the server publishes no endpoint
# for it). That is a judgement a human makes ONCE, here, in writing.

# Terms that make an expression monetary. Broad on purpose: a false positive
# costs one LEDGER line, a false negative costs the customer money.
_MONEY_WORDS = (
    "price", "amount", "subtotal", "total", "discount", "fee", "cash",
    "outstanding", "collectible", "payable", "fine", "debt", "credit",
    "prepay", "prepaid", "money", "cost", "charge", "balance",
)

# Counters and pagination that happen to contain a money word. Each is a
# quantity of THINGS, never of currency.
_NOT_MONEY = (
    "total_attempts", "total_pages", "total_items", "total_deliveries",
    "total_referrals", "total_count", "total_qty", "total_quantity",
    "total_bottles", "total_returned", "total_orders", "total_seconds",
    "total_minutes", "total_records", "total_users", "sent_count",
    "balance_lines", "total_points",
)

_ARITH_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv)

# Bot handlers build screens by `text += ...`. An accumulator with one of these
# in its name is holding a MESSAGE, not a sum -- and the money words that trip
# the scanner are the kwargs of whatever renders into it.
_TEXT_ACCUMULATOR = re.compile(
    r"(?:^|_)(text|msg|message|caption|body|lines|content|header|footer|html|"
    r"summary|copy|label|brief)(?:$|_)",
    re.IGNORECASE,
)

# The trees a customer-facing or staff-facing SCREEN can live in. `business_app`
# is deliberately absent: money arithmetic there is the engine doing its job.
_SCANNED_TREES = ("telegram_bot", "staff_bot")


# --- THE LEDGER -------------------------------------------------------------
#
# key   = (repo-relative path, enclosing def/class chain, ast.unparse(expr))
#         Line numbers are NOT part of the key -- code moves, and a pin that
#         breaks on an unrelated insertion gets deleted by the next person.
#         `ast.unparse` normalises whitespace and quoting, so reformatting is
#         free and a CHANGED CALCULATION is not.
# value = the verdict, in the sweep's own vocabulary, and WHY.
#
# ADDING A LINE HERE IS THE POINT. It is a human stating, on the record, that a
# screen computing its own money figure is safe -- and naming the reason. If you
# cannot write the reason, you have found the ninth instance.

_LEDGER: Dict[Tuple[str, str, str], str] = {
    # -- telegram_bot: checkout confirmation -------------------------------
    (
        "telegram_bot/handlers/orders.py",
        "OrderHandlers._show_order_confirmation",
        "cart_total_amount * discount_value / 100.0",
    ): (
        "ONE-DECISION (accepted mirror). Sweep #29. The server's "
        "LoyaltyService._compute_reward_discount is the same formula in Decimal/"
        "ROUND_HALF_UP; measured delta 0.0000 across four subtotals. It stays a "
        "second expression only because no order exists yet at confirm time, so "
        "there is nothing to preview against. Closing it needs a server-side "
        "order-preview endpoint (pricing-criticals-report.md, out-of-lane #1)."
    ),
    (
        "telegram_bot/handlers/orders.py",
        "OrderHandlers._show_order_confirmation",
        "cart_total_amount * discount_value",
    ): "ONE-DECISION (accepted mirror). Same expression as above, fixed-discount arm.",
    (
        "telegram_bot/handlers/orders.py",
        "OrderHandlers._show_order_confirmation",
        "float(cart_total_amount) - reward_discount",
    ): (
        "ONE-DECISION (accepted mirror). The grand total nets the mirrored "
        "discount off the SERVER's `cart['subtotal']` -- `cart_total_amount` is "
        "read, never summed, since the #7 fix. Do not reintroduce a fallback "
        "sum here; a second client calculation IS the defect."
    ),
    (
        "telegram_bot/handlers/orders.py",
        "OrderHandlers._show_order_confirmation",
        "float(cart_total_amount) - potential_applied",
    ): (
        "ONE-DECISION (fallback of a published figure). Sweep #30. Both operands "
        "are server figures; this only runs when the server omitted "
        "`estimated_payable_after_prepayment`. It is advisory copy, not an "
        "amount anyone posts."
    ),
    (
        "telegram_bot/handlers/orders.py",
        "OrderHandlers._show_order_confirmation",
        "MIN_ORDER_AMOUNT - cart_total_amount",
    ): (
        "ONE-DECISION (gate shortfall). A shortfall against a shared constant, "
        "computed off the SERVER's subtotal. Nothing is posted from it."
    ),
    (
        "telegram_bot/handlers/orders.py",
        "OrderHandlers._build_cod_prepayment_brief",
        "normalized_order_total - potential_applied",
    ): (
        "ONE-DECISION (read-only receipt). Post-order summary; `order_total` is "
        "the CREATED order's `total_amount`, i.e. the server's own charge. "
        "Nothing is posted from this screen."
    ),
    (
        "telegram_bot/handlers/orders.py",
        "OrderHandlers.checkout_choose_reward",
        "cost - balance",
    ): "NOT MONEY. Loyalty AquaCoins shortfall, not currency.",
    (
        "telegram_bot/handlers/orders.py",
        "OrderHandlers.checkout_choose_reward",
        "min_order - float(subtotal)",
    ): (
        "ONE-DECISION (gate shortfall). `subtotal` is `cart['subtotal']` since "
        "the #7 fix, so the 'add N UZS to unlock' figure and the reward gate the "
        "server enforces read the same number."
    ),
    # -- telegram_bot: cart + product screens ------------------------------
    # Sweep #7 (cart screen) is FIXED: `show_cart` reads `cart['subtotal']` and
    # per-line `total_price`, so `price * quantity` and its accumulator are gone
    # and their LEDGER entries went with them. What remains is the gate.
    (
        "telegram_bot/handlers/products.py",
        "min_order_shortfall",
        "float(MIN_ORDER_AMOUNT) - float(subtotal)",
    ): (
        "ONE-DECISION (gate shortfall). Sweep #7, post-fix. The cart screen's "
        "minimum-order gate and its 'add N UZS more' copy are now the SAME "
        "expression -- this one -- and its input is the SERVER's "
        "`cart['subtotal']`, not a client sum. `MIN_ORDER_AMOUNT` is a shared "
        "constant, not a server-published figure, so there is nothing to read "
        "instead; nothing is posted from the result. Mirrors the identical "
        "verdict on `OrderHandlers._show_order_confirmation`'s "
        "`MIN_ORDER_AMOUNT - cart_total_amount`."
    ),
    (
        "telegram_bot/handlers/products.py",
        "ProductHandlers._format_quantity_step_text",
        "unit_price * quantity",
    ): (
        "ONE-DECISION (single-line quote, SSOT unit price). `unit_price` comes "
        "from `_get_effective_unit_price`, the bot's one price-resolution point. "
        "No cart exists yet, so there is no server total to read; the figure is "
        "a per-product quote and nothing is posted from it."
    ),
    (
        "telegram_bot/handlers/subscriptions.py",
        "SubscriptionHandlers.manage_subscription_items",
        "price * quantity",
    ): (
        "ONE-DECISION (read-only line). `unit_price` is the SERVER's stored "
        "subscription-item price. The confirm screen that actually prices a "
        "subscription reads `preview['total_amount']` from "
        "`POST /subscriptions/preview` (sweep #32) -- this screen posts nothing."
    ),
    # -- staff_bot ----------------------------------------------------------
    (
        "staff_bot/handlers/delivery/cash_collection.py",
        "CashCollectionHandler.receive_collection_amount",
        "amount - total_outstanding",
    ): (
        "ONE-DECISION (object). The overpayment surplus, priced off "
        "`_scoped_ceiling(statement, flow['delivery_address_id'])` -- the same "
        "ceiling the collection is posted against (sweep #3, plan E4). Both "
        "operands are the driver's typed amount and that one ceiling."
    ),
    # Sweep #6 (`CreateOrderHandler._format_cart_summary`: `price * qty` and
    # `subtotal += item_total`) is GONE, not excused. The client-scoped quote the
    # entries called for exists -- `POST /staff/operator/users/<id>/
    # order-estimate` -> `StaffService.price_phone_order`, the same function
    # `create_phone_order` charges from -- and the cart screen now renders the
    # per-line `total_price` and `subtotal` off that response instead of
    # multiplying an OPERATOR-scoped catalogue price. `test_the_ledger_does_not_
    # rot` is what forced these two lines out with the code.
    # -- non-screen code that trips the money vocabulary --------------------
    (
        "staff_bot/webhook_server.py",
        "StaffWebhookServer.new_order_handler",
        "total - sent_count",
    ): "NOT MONEY. Broadcast fan-out counter.",
}


def _leaf_terms(node: ast.AST) -> List[str]:
    """Every identifier-ish token in an expression: names, attrs, dict keys."""
    out: List[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.append(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.append(sub.attr)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            # Covers `item['total_price']` and `x.get('subtotal')`.
            out.append(sub.value)
    return out


def _smells_monetary(node: ast.AST) -> bool:
    for term in _leaf_terms(node):
        low = term.lower()
        if any(stop in low for stop in _NOT_MONEY):
            continue
        if any(word in low for word in _MONEY_WORDS):
            return True
    return False


def _is_string_concat(node: ast.AST) -> bool:
    """`+` over text is not arithmetic, and bot handlers are mostly text."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.BinOp):
        return _is_string_concat(node.left) or _is_string_concat(node.right)
    if isinstance(node, ast.Call):
        func = node.func
        # `i18n.get(...)` returns a translated STRING.
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Name)
            and func.value.id == "i18n"
        )
    return False


class _MoneyArithmeticScanner(ast.NodeVisitor):
    def __init__(self, relpath: str) -> None:
        self.relpath = relpath
        self._scope: List[str] = []
        self.hits: List[Tuple[Tuple[str, str, str], int]] = []

    def _enter(self, node):
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter
    visit_ClassDef = _enter

    def _record(self, node: ast.AST, rendered: str) -> None:
        key = (self.relpath, ".".join(self._scope), rendered)
        self.hits.append((key, getattr(node, "lineno", 0)))

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if (
            isinstance(node.op, _ARITH_OPS)
            and not _is_string_concat(node)
            and _smells_monetary(node)
        ):
            self._record(node, ast.unparse(node))
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        target = ast.unparse(node.target)
        if (
            isinstance(node.op, _ARITH_OPS)
            and not _is_string_concat(node.value)
            and not (isinstance(node.op, ast.Add) and _TEXT_ACCUMULATOR.search(target))
            and (_smells_monetary(node.target) or _smells_monetary(node.value))
        ):
            self._record(
                node, f"{ast.unparse(node.target)} += {ast.unparse(node.value)}"
            )
        self.generic_visit(node)


def _scan_bot_trees(
    roots: Tuple[str, ...] = _SCANNED_TREES,
) -> List[Tuple[Tuple[str, str, str], int]]:
    hits: List[Tuple[Tuple[str, str, str], int]] = []
    for tree in roots:
        root = REPO_ROOT / tree
        if not root.is_dir():  # pragma: no cover - repo layout guard
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            scanner = _MoneyArithmeticScanner(rel)
            scanner.visit(ast.parse(path.read_text(encoding="utf-8")))
            hits.extend(scanner.hits)
    return hits


@pytest.mark.unit
def test_no_undeclared_client_side_money_arithmetic():
    """THE NINTH-INSTANCE DETECTOR.

    Every arithmetic expression over a money-named term in `telegram_bot/` and
    `staff_bot/` must carry a written verdict in `_LEDGER`. A new one -- a new
    screen that sums prices, a new "remaining" line, a fallback added "just in
    case the server field is missing" -- fails here, with the sweep's own
    question attached: *what does the engine do with this number, and is it the
    same expression?*

    This is not a style rule. All three instances the manual sweep found in
    2026-08 (operator cart, customer checkout, cart screen) are AST-identical to
    what this scanner flags, and all three shipped past 8,000 green tests.
    """
    hits = _scan_bot_trees()
    seen = {key for key, _ in hits}
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
            "A SCREEN IS COMPUTING ITS OWN MONEY FIGURE, AND NO HUMAN HAS RULED "
            "ON IT.\n\n"
            + "\n".join(lines)
            + "\n\n"
            "This is the exact shape of all eight known show-vs-settle defects "
            "(SHOW-VS-SETTLE-SWEEP.md). Before adding it to `_LEDGER` in "
            f"{__file__}, answer:\n"
            "  1. Which server expression decides the amount that is ACTUALLY "
            "charged / settled / handed off?\n"
            "  2. Can this screen READ that figure instead of recomputing it "
            "(the `_collect_offer` / `preview` pattern)?\n"
            "  3. If not, name the endpoint that does not exist yet -- that is "
            "the fix, and the LEDGER entry is the IOU.\n"
            "A ledger entry that says 'looks fine' is how the fifth instance "
            "survived four reviews."
        )
    assert seen  # the scanner must actually be finding things


@pytest.mark.unit
def test_the_ledger_does_not_rot():
    """A fixed defect must LEAVE the ledger, or the ledger stops meaning anything.

    Without this, `_LEDGER` accumulates entries for code that no longer exists
    and a future reader cannot tell which of its lines are live risk. It also
    means the two SPLIT entries below cannot be quietly "resolved" by deleting
    the handler and leaving the excuse behind.
    """
    seen = {key for key, _ in _scan_bot_trees()}
    stale = sorted(set(_LEDGER) - seen, key=lambda k: (k[0], k[1], k[2]))
    assert not stale, (
        "these LEDGER entries no longer match any code -- the expression was "
        "changed, moved or fixed. Delete them (or re-key them) so the ledger "
        f"keeps describing the repo that exists:\n"
        + "\n".join(f"  {p}  [{s}]  {e}" for p, s, e in stale)
    )


# ===========================================================================
# PART 2 -- THE SAME LINT FOR THE ADMIN UI
# ===========================================================================
#
# The sweep audited 13 admin-UI surfaces (#15-#27) and found NO split: every
# money-posting modal reads its figure and its scope off ONE server object
# (`recordCollectionScope`, `utils/codCollectScope.js`) or off a `preview*` call.
# The baseline is therefore EMPTY, which makes this the cheapest pin in the file
# and the one with the least room to argue: the first line of client-side money
# arithmetic in the admin UI is, on the evidence of this repo, a regression.

_JS_ROOTS = ("admin_ui/src/pages", "admin_ui/src/components", "admin_ui/src/utils")

# `<ident with a money word> <* or / or - or +> <ident or number>` and the mirror
# form. Restricted to identifier operands so that CSS-ish and index arithmetic on
# literals does not register.
_JS_MONEY_TERM = r"[A-Za-z_$][A-Za-z0-9_$]*(?:%s)[A-Za-z0-9_$]*" % "|".join(
    w.capitalize() + "|" + w for w in ("price", "amount", "subtotal", "outstanding", "collectible", "payable")
)
_JS_OPERAND = r"[A-Za-z_$][A-Za-z0-9_$.\[\]']*|[0-9][0-9_.]*"
_JS_ARITH = re.compile(
    r"(?<![A-Za-z0-9_$])(?:(%s)\s*([*/+-])\s*(%s)|(%s)\s*([*/+-])\s*(%s))(?![A-Za-z0-9_$])"
    % (_JS_MONEY_TERM, _JS_OPERAND, _JS_OPERAND, _JS_MONEY_TERM)
)

# Non-monetary counters/labels that contain a money word.
_JS_IGNORE = re.compile(r"amountOfTime|priceRuleCount|amountIndex")

# Known-and-ruled admin-UI money arithmetic. EMPTY is the correct state.
_JS_LEDGER: Set[Tuple[str, str]] = set()


def _strip_js_noise(source: str) -> str:
    """Blank out comments and string/template literals, preserving line count.

    Money words are everywhere in admin-UI COPY ("they will not be able to order
    cash-on-delivery until it is paid down"); matching inside a string would make
    this lint pure noise and it would be deleted within a week.
    """
    out: List[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
        elif ch == "/" and nxt == "*":
            while i < n and not (source[i] == "*" and source[i + 1 : i + 2] == "/"):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
        elif ch in "'\"`":
            quote = ch
            out.append(" ")
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\":
                    out.append(" ")
                    i += 1
                    if i < n:
                        out.append(" ")
                        i += 1
                    continue
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            out.append(" ")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@pytest.mark.unit
def test_no_client_side_money_arithmetic_in_the_admin_ui():
    """Admin UI shows server figures and posts typed amounts. Keep it that way.

    Sweep #15-#27: thirteen surfaces, thirteen ONE-DECISION verdicts. The
    strongest of them (`recordCollectionScope`) works precisely because the
    amount rendered in the modal and the `deliveryAddressId` posted with the
    collection are two fields of ONE object built by `codCollectScope.js`. The
    moment a page starts deriving a figure with `*` or `-`, that guarantee is
    gone and nothing else in the suite would notice.
    """
    findings: List[str] = []
    for rel_root in _JS_ROOTS:
        root = REPO_ROOT / rel_root
        if not root.is_dir():  # pragma: no cover - repo layout guard
            continue
        for path in sorted(list(root.rglob("*.js")) + list(root.rglob("*.jsx"))):
            if ".test." in path.name or "__tests__" in path.parts:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            code = _strip_js_noise(path.read_text(encoding="utf-8"))
            for lineno, line in enumerate(code.splitlines(), start=1):
                if _JS_IGNORE.search(line):
                    continue
                match = _JS_ARITH.search(line)
                if not match:
                    continue
                expr = match.group(0).strip()
                if (rel, expr) in _JS_LEDGER:
                    continue
                findings.append(f"  {rel}:{lineno}  {expr}")
    assert not findings, (
        "the admin UI has started computing money client-side:\n"
        + "\n".join(findings)
        + "\n\nEvery admin money surface in the sweep reads its figure off the "
        "server object it also posts from. If this genuinely cannot, add it to "
        "`_JS_LEDGER` with the reason -- and expect to justify it."
    )


# ===========================================================================
# PART 3 -- THE BACKEND HALF: every preview must be replayed, not re-derived
# ===========================================================================
#
# The client-side lint cannot see the other way this defect arrives: a service
# grows a `preview`/`simulate`/`estimate` that an admin screen renders, and an
# `apply` that recomputes the same thing a second time. That is exactly
# `OrderCashEditService` INVERTED, and it is what sweep #17 flags as a latent
# gap (`simulate_event_amount_change` exists; the modal that posts the change
# shows no projection at all).

# service-method -> the expression its WRITE half must go through.
# `None` means "declared read-only: no write half may exist".
_PREVIEW_SURFACES: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("business_app.services.order_cash_edit_service", "OrderCashEditService.preview"): (
        "apply_edit",
        "self.preview(",
    ),
    (
        "business_app.services.order_payment_method_edit_service",
        "OrderPaymentMethodEditService.preview",
    ): ("apply_edit", "self.preview("),
    ("business_app.services.order_edit_service", "OrderEditService.preview"): (
        "apply_edit",
        "self._build_plan(",
    ),
    # Sweep #6, CLOSED. ONE-DECISION (shared expression) rather than (object):
    # the operator's quote and the phone order both enter
    # `StaffService.price_phone_order`, but the write does NOT replay a frozen
    # quote -- there is no confirm-against-a-plan step here, the operator edits
    # the basket freely and the order is built from the basket. Freezing a quote
    # would need a quote token and would buy nothing, because the only thing
    # that could drift between the two calls is the CONTRACT itself, and an
    # order must be charged at the contract live at creation.
    ("business_app.services.staff_service", "StaffService.estimate_phone_order"): (
        "create_phone_order",
        "StaffService.price_phone_order(",
    ),
}


@pytest.mark.unit
def test_every_two_step_money_edit_replays_its_own_preview():
    """The admin types a number against a PROJECTION. The write must replay it.

    All three of these are two-step admin flows: `Orders.js` calls `preview*`,
    renders the plan, the human confirms, and a second call writes. If the write
    half re-derives the plan from scratch, the confirmation the human gave was
    against a different world -- and, on the cash-edit path specifically, against
    a different ALLOCATION SCOPE, which is how money reaches the wrong debt.
    """
    import importlib

    for (module_name, qualname), (write_method, required_call) in _PREVIEW_SURFACES.items():
        module = importlib.import_module(module_name)
        cls_name, _ = qualname.split(".")
        cls = getattr(module, cls_name)
        src = inspect.getsource(getattr(cls, write_method))
        assert required_call in src, (
            f"{cls_name}.{write_method} no longer goes through `{required_call}`. "
            "The figure the admin confirmed and the change actually written are "
            "now two expressions -- the defect this whole file exists for."
        )


@pytest.mark.unit
def test_new_preview_surfaces_must_declare_their_write_half():
    """A new `preview*` on a money service is a new show-vs-settle seam.

    This fails the day someone adds `preview_x` to a service without recording
    which write path replays it. That is the cheap moment to get it right; the
    expensive moment is when an admin has already confirmed against it.
    """
    import importlib
    import pkgutil

    import business_app.services as services_pkg

    # Services whose preview-ish methods are NOT part of a two-step money edit.
    # Each is exempt for a stated reason, not because it was inconvenient.
    exempt = {
        # Renders a notification body. No money.
        "NotificationService.preview_admin_notification_template",
        # Validation helper for contract date ranges. No money is posted from it.
        "CorporateContractService.preview_contract_price_overlaps",
        # Sweep #21: ONE-DECISION (re-run). The preview reuses the REAL allocator
        # (`_plan_allocation` + `resolve_allocation_scope`); `post_collection`
        # re-runs those same functions on the amount the admin typed. The
        # breakdown is advisory and the docstring says so.
        "CashCollectionService.preview_personal_card_transfer",
        # Sweep #17: projects the RESULT of an event-amount change. Its only
        # caller is OrderCashEditService, which is THE STANDARD.
        "CashCollectionService.simulate_event_amount_change",
        # Internal Decimal helper on the allocation path, not a rendered figure.
        "CashCollectionService.estimate_settleable_credit_for_order",
    }
    declared = {q for _, q in _PREVIEW_SURFACES}

    undeclared: List[str] = []
    for mod_info in pkgutil.iter_modules(services_pkg.__path__):
        module = importlib.import_module(f"business_app.services.{mod_info.name}")
        for cls_name, cls in vars(module).items():
            if not inspect.isclass(cls) or cls.__module__ != module.__name__:
                continue
            for attr, member in vars(cls).items():
                if not (inspect.isfunction(member) or isinstance(member, (classmethod, staticmethod))):
                    continue
                if not re.match(r"^(preview|simulate|estimate|quote)(_|$)", attr):
                    continue
                qual = f"{cls_name}.{attr}"
                if qual in exempt or qual in declared:
                    continue
                undeclared.append(f"business_app/services/{mod_info.name}.py :: {qual}")

    assert not undeclared, (
        "a new preview/simulate/estimate surface appeared and nothing records "
        "how the WRITE half reaches the same figure:\n"
        + "\n".join(f"  {u}" for u in sorted(undeclared))
        + "\n\nAdd it to `_PREVIEW_SURFACES` naming the write method and the "
        "call that replays the plan (the `OrderCashEditService` shape), or to "
        "`exempt` with the reason it shows no money a human acts on."
    )


# ===========================================================================
# PART 4 -- THE STANDARD ITSELF
# ===========================================================================


@pytest.mark.unit
def test_the_standard_is_still_the_standard():
    """`OrderCashEditService` is the north star every verdict above is graded on.

    Two properties, both load-bearing:

      1. `apply_edit` re-enters `self.preview(...)` -- it does not rebuild the
         plan. The number the admin confirmed IS the number applied.
      2. The scope is the event's FROZEN one (`AllocationScope.from_event`), so
         a correction settles the debts the ORIGINAL collection settled, not
         whatever is outstanding at correction time. This is the single property
         that made the fifth instance's fix expressible at all.

    If either degrades, every "ONE-DECISION" verdict in `_LEDGER` and
    `_PREVIEW_SURFACES` loses the thing it is measured against, and this file
    quietly becomes a style checker.
    """
    from business_app.services.order_cash_edit_service import OrderCashEditService

    apply_src = inspect.getsource(OrderCashEditService.apply_edit)
    assert "plan = self.preview(" in apply_src, (
        "apply_edit must REPLAY the preview, not rebuild the plan"
    )
    # ...and it must do so before any write decision reads the plan.
    assert apply_src.index("plan = self.preview(") < apply_src.index("atomic_transaction()"), (
        "the plan must be settled before the transaction opens, so the applied "
        "change cannot be a different one from the previewed change"
    )

    preview_src = inspect.getsource(OrderCashEditService.preview)
    scope_src = inspect.getsource(OrderCashEditService._resolve_event)
    combined = preview_src + scope_src
    assert "adjust_event_amount" in combined or "adjust_event_amount" in apply_src, (
        "the cash edit must go through the engine's event-adjustment path"
    )

    from business_app.services.cash_collection_service import CashCollectionService

    adjust_src = inspect.getsource(CashCollectionService.adjust_event_amount)
    assert "AllocationScope.from_event(" in adjust_src, (
        "a correction must replay the FROZEN scope stamped on the event. "
        "Re-resolving the scope at correction time is how a correction settles a "
        "debt the original collection never touched."
    )


@pytest.mark.unit
def test_the_cod_collect_offer_is_one_object_for_both_halves():
    """Sweep #2, the fifth instance's fix -- pinned so it cannot be unwound.

    `_format_statement` (what the driver READS) and `start_full_collection`
    (what the driver POSTS) must both take their address and their amount from a
    single `_collect_offer(statement)` call. Two `_collect_offer` calls would
    still be one *expression*, but the fix's whole point is that the ADDRESS and
    the AMOUNT travel together as one tuple -- so a place-scoped ceiling can
    never be posted against a personal-scoped address.
    """
    handler_file = "staff_bot/handlers/delivery/cash_collection.py"
    for method_name in ("_format_statement", "start_full_collection"):
        node = _function_node(handler_file, f"CashCollectionHandler.{method_name}")
        assert node is not None, (
            f"CashCollectionHandler.{method_name} no longer exists -- the fifth "
            "instance's fix lived in it; re-key this pin, do not delete it"
        )
        src = ast.unparse(node)
        assert "_collect_offer(" in src, (
            f"CashCollectionHandler.{method_name} no longer reads the offer; the "
            "shown figure and the posted scope are two decisions again"
        )
        offers = [
            sub
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "_collect_offer"
        ]
        assert len(offers) == 1, (
            f"CashCollectionHandler.{method_name} calls `_collect_offer` "
            f"{len(offers)} times. One call, one tuple, one decision -- a second "
            "call is a second decision waiting to disagree."
        )
        # The tuple must be unpacked whole: taking only the amount and sourcing
        # the address elsewhere is the fifth instance, restored.
        assert re.search(r"=\s*\w+\._collect_offer\(", src) is None or re.search(
            r"\w+\s*,\s*\w+\s*=\s*(cls|self)\._collect_offer\(", src
        ), (
            f"CashCollectionHandler.{method_name} must unpack BOTH halves of the "
            "offer (address, amount) from the one call"
        )


def _dedent(src: str) -> str:
    import textwrap

    return textwrap.dedent(src)


def _function_node(relpath: str, qualname: str):
    """Locate `Class.method` in a file WITHOUT importing it.

    The bot packages are only importable with their own directory on `sys.path`
    (`telegram_bot/handlers/__init__.py` does `from i18n import i18n`), and the
    repo has a documented cross-suite `sys.path` shadowing hazard. Parsing the
    file keeps these pins honest in any worker.
    """
    tree = ast.parse((REPO_ROOT / relpath).read_text(encoding="utf-8"))
    parts = qualname.split(".")
    scope: List[ast.AST] = tree.body
    node = None
    for part in parts:
        node = next(
            (
                child
                for child in scope
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == part
            ),
            None,
        )
        if node is None:
            return None
        scope = node.body
    return node


# ===========================================================================
# PART 5 -- THE (FORMERLY) LIVE SPLITS
# ===========================================================================
#
# These were production defects, found while building this file, reported in
# `.superpowers/sdd/2026-08-05-e2e-coverage/sweep-as-test-report.md`, and
# deliberately NOT fixed here (that was a test-writing lane). Each was pinned
# `xfail(strict=True)`, which meant the day someone fixed one, THAT TEST FAILED
# and forced the fixer to un-xfail it and remove the LEDGER entry above with it.
# Both went through exactly that on 2026-08-05 -- sweep #7 (the cart screen) and
# sweep #8 (the driver's cash-handoff button) -- and both are now REGRESSION
# pins. There are no `xfail` pins left in this file; a new live split gets a new
# one, and the same rule.


@pytest.mark.unit
def test_show_cart_does_not_price_the_cart_itself():
    """The cart screen must read the server's total, like the confirm screen does.

    `_show_order_confirmation` was fixed in exactly this way and carries a large
    comment forbidding the reintroduction of arithmetic. `show_cart` renders the
    SAME cart payload, one screen earlier, and got the same treatment on
    2026-08-05 (sweep #7, the cart-screen half): it now reads `cart['subtotal']`
    and per-line `total_price`, and its minimum-order gate is fed that same
    server figure through `min_order_shortfall`.

    This used to be an `xfail(strict=True)` pin over a live defect. It is now a
    REGRESSION pin: the arithmetic must not come back, in any form -- including
    a "fallback sum in case the server field is missing", which is how the
    second expression gets reborn.
    """
    # Read the SOURCE rather than importing. `telegram_bot/handlers/__init__.py`
    # does `from i18n import i18n`, which only resolves with `telegram_bot/` on
    # sys.path -- an import here would raise ModuleNotFound in a worker that has
    # not had that path inserted, and the pin would report on nothing. A pin that
    # cannot observe its own subject is worse than no pin.
    node = _function_node("telegram_bot/handlers/products.py", "ProductHandlers.show_cart")
    assert node is not None, (
        "ProductHandlers.show_cart no longer exists -- re-key or delete this pin"
    )
    arithmetic = sorted(
        {
            ast.unparse(sub)
            for sub in ast.walk(node)
            if isinstance(sub, (ast.BinOp, ast.AugAssign))
            and isinstance(sub.op, _ARITH_OPS)
            and not _is_string_concat(sub if isinstance(sub, ast.BinOp) else sub.value)
            and _smells_monetary(sub)
        }
    )
    assert not arithmetic, (
        f"show_cart computes money the server already published: {arithmetic}. "
        "GET /cart serves per-line `total_price` and cart `subtotal`, both "
        "composed by CartService.get_cart_summary -- the same one calculation "
        "the order is built from."
    )
    assert "subtotal" in ast.unparse(node), "show_cart should read cart['subtotal']"


@pytest.mark.unit
def test_the_reconciliation_button_hands_off_the_amount_it_displayed(
    app, db, delivery_driver, monkeypatch
):
    """THE JOURNEY: driver opens the screen, reads the button, taps it.

    Not a return-value assertion -- this draws the REAL screen through the real
    staff-bot handler, reads the figure off the rendered button, lands a real
    `CashCollectionEvent` in the gap (exactly what a COD completion does), taps
    THAT button, and asks THE ONLY QUESTION THAT MATTERS: *how much cash was
    recorded against this driver, and is it the number on the button?*

    This used to be an `xfail(strict=True)` pin over a live defect: the button
    posted `{}`, so `submit_session` re-derived the handoff from live
    `CashCollectionEvent`s at tap time (`expected - prior_declared`, UNCLAMPED)
    while the screen had shown `expected - declared` CLAMPED at 0 -- shown
    120,000, recorded 150,000. It is now a REGRESSION pin: the figure is frozen
    into the button's own callback and posted verbatim as `declared_cash`.

    Do not "simplify" this back into a direct `submit_session(declared_cash=
    None)` call. That models the tap instead of performing it, and the model is
    what went stale last time. The exhaustive shapes live in
    tests/unit/test_reconciliation_handoff_shows_what_it_records.py.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from business_app.models.payment import DriverCashHandoff
    from business_app.services.driver_reconciliation_service import (
        DriverReconciliationService,
    )
    from tests.unit._scope_money_helpers import make_user

    # Imported inside the test: this file otherwise keeps the bot packages out
    # of its import graph (see `_function_node`).
    from staff_bot import i18n as i18n_module
    from staff_bot.handlers.delivery import status_update as bot_module
    from staff_bot.handlers.delivery.status_update import StatusUpdateHandler

    service = DriverReconciliationService()

    class _Api:
        """The two reconciliation endpoints, over the real service."""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get_reconciliation_session(self, _token):
            session = service.get_open_session_for_driver(delivery_driver.id)
            return MagicMock(success=True, data=service.get_session_detail(session.id))

        async def submit_reconciliation_session(self, _token, payload):
            submitted = service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=payload.get("declared_cash"),
            )
            db.session.commit()
            return MagicMock(success=True, data=service.get_session_detail(submitted.id))

    # The button's amount only reaches a human through the translation template.
    monkeypatch.setattr(
        i18n_module.i18n,
        "translations",
        {"en": {"staff.delivery.handoff_remaining_cash": "Submit remaining {amount}"}},
    )
    handler = StatusUpdateHandler()
    monkeypatch.setattr(bot_module, "api_client", _Api())
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(bot_module.flow_state, "clear_and_drain", AsyncMock())
    context = MagicMock()
    context.user_data = {"language": "en", "authenticated": True,
                         "staff_roles": ["delivery_driver"]}

    def _tap(callback_data):
        upd = MagicMock()
        upd.effective_user = MagicMock(id=999)
        upd.message = None
        upd.callback_query = MagicMock()
        upd.callback_query.data = callback_data
        upd.callback_query.answer = AsyncMock()
        upd.callback_query.edit_message_text = AsyncMock()
        return upd

    customer = make_user(db)
    session = service.get_open_session_for_driver(delivery_driver.id)
    db.session.commit()
    _land_cash_collection(db, session, customer, delivery_driver, Decimal("120000.00"), "SVS-1")

    # --- THE SCREEN, as the driver receives it.
    screen = _tap("staff_reconcile_session")
    asyncio.run(handler.show_reconciliation_session(screen, context))
    markup = screen.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    button = next(
        b
        for row in markup.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("staff_reconcile_submit_all")
    )
    shown = Decimal(re.sub(r"[^\d.]", "", button.text))

    # --- THE GAP. A delivery completes; the engine stamps a COD collection
    # against the same open session. The driver's screen is already drawn.
    _land_cash_collection(db, session, customer, delivery_driver, Decimal("30000.00"), "SVS-2")

    # --- THE TAP. Exactly the callback Telegram delivers for that button.
    asyncio.run(handler.submit_reconciliation_all(_tap(button.callback_data), context))
    db.session.commit()

    recorded = (
        DriverCashHandoff.query.filter_by(driver_cash_session_id=session.id)
        .order_by(DriverCashHandoff.id.desc())
        .first()
    )

    assert shown == Decimal("120000"), f"fixture drifted; the button reads {button.text!r}"
    assert recorded is not None, "the tap must record a handoff"
    assert Decimal(str(recorded.amount)) == shown, (
        f"the button read {shown:,.0f} and {Decimal(str(recorded.amount)):,.0f} "
        "was written against the driver. The screen and the write must be ONE "
        "figure: the button freezes it and the tap posts it."
    )


def _land_cash_collection(db, session, customer, collector, amount: Decimal, event_id: str):
    """Insert a real COD collection against a driver's open session."""
    from business_app.models.payment import CashCollectionEvent
    from shared.enums import CashCollectionSource

    db.session.add(
        CashCollectionEvent(
            event_id=event_id,
            customer_id=customer.id,
            collector_user_id=collector.id,
            recorded_by_user_id=collector.id,
            driver_cash_session_id=session.id,
            amount=amount,
            currency="UZS",
            source=CashCollectionSource.DELIVERY_COMPLETION,
            occurred_at=datetime.now(UTC),
        )
    )
    db.session.commit()
