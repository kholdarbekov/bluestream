"""THE one calculation behind a COD debtor's ROW figure and their COLLECT CEILING.

Owner ruling A6 (``.superpowers/sdd/2026-08-04-plan-e/OWNER-ANSWERS.md``), rule
R-B: *"'Collect all' must offer EXACTLY the row figure. Same calculation, not two
that happen to agree."*

WHY THIS MODULE EXISTS AT ALL. Before it, the debtor row was composed in
``StaffService.paginate_cod_debtors_for_staff`` as ``cluster total + the
coworkers' share of the place`` — a UNION — while the staff bot's ceiling was
``max(own, cluster, place)``. With Alice owing 10 000 at home and 15 000 at the
office and Bob owing 20 000 at that office, the row read 45 000 and the ceiling
read ``max(25k, 25k, 35k) = 35k``. A max is not a union; the two expressions
agreed on the narrow shapes anyone had tried and diverged the moment a coworker
owed anything. Both call sites now call :func:`collectible_cod_total`.

WHAT THE NUMBER MEANS. It is the size of the debt set ONE standalone collection
posted by this person can actually settle, i.e. the sum of the allocation
engine's two rings for a PLACE-scoped post
(``cash_collection_service.py:3503-3511``):

* **ring 1** — every open delivered COD debt at the grouped place, ANY owner;
* **ring 2** — every open delivered COD debt of the poster's own cluster, minus
  anything ring 1 already holds.

So it is ``cluster_total + (place debt whose owner is outside the cluster)``.
Adding the whole place total instead would double-count the member's own office
order, which is already inside ``cluster_total`` (it is their ``Payment.user_id``).

WHY IT IS ARITHMETIC OVER READERS AND NOT A QUERY. The allocation engine is
frozen for Plan E (plan C1: ``cash_collection_service.py``, ``allocation_scope.py``
and ``models/user.py`` must stay byte-identical). Every input here comes from an
existing PUBLIC reader — ``get_customer_cod_statement``,
``get_place_cod_statement``, ``list_users_with_open_cod_debts`` — so the rule is
expressible as a composition outside the engine. Do not "simplify" this by
moving it into the engine.
"""

from typing import Any, Dict, Iterable, Optional, Tuple

__all__ = ["collectible_cod_total", "place_widening_applies", "resolve_collect_scope"]


def place_widening_applies(cash_service: Any, customer_id: Any, delivery_address_id: Any) -> bool:
    """🔴 ONE decision: may a DISPLAY widen this person to their place at all?

    THE FOURTH INSTANCE OF THE SAME SHAPE, and the reason this is a CALL and not
    a rule. ``CashCollectionService.resolve_allocation_scope`` FORCES personal
    scope for a grocery account — its cash is mirrored onto a corporate contract
    and must never co-mingle with a household's (spec 5.8 layer 3,
    ``cash_collection_service.py:585-597``). Nothing on the display side knew
    that, so both display halves widened a grocery anyway:

    * :meth:`StaffService.paginate_cod_debtors_for_staff` widened the shop's
      debtor row to the whole place, and
    * :func:`resolve_collect_scope` handed back a PLACE scope carrying the
      shop's grouped address,

    while a real post settled only the shop's OWN debt. Measured on
    ``grocery_at_place``: every screen read **18 000**, the post settled
    **8 000**, and the coworker's 10 000 became the shop's prepaid credit. The
    receipt then said "10 000 still collectible", the next lap offered that
    10 000, settled **nothing**, and pushed the credit to 20 000 — a debt the
    screens name on every lap and no lap can ever pay.

    The engine's refusal is deliberate and correct, so the DISPLAY is what must
    change — and it must not change by MIRRORING the rule. A display-side
    ``user.is_grocery_store`` test would be a second expression of the engine's
    scope resolution, which is precisely the shape this whole effort exists to
    delete: two expressions that agree today desynchronise on the next edit, and
    that has already happened twice in this codebase. So the display ASKS the
    engine the question the engine will itself answer at post time, under the
    same ``STANDALONE_MEETING`` source the collect flows actually post with
    (the only member of ``_PLACE_SCOPE_SOURCES`` a standalone collection uses),
    and widens only when the answer is PLACE.

    Asking rather than mirroring also refuses, for free, every OTHER reason the
    engine declines a place — an ungrouped address, or a customer whose cluster
    owns no address in the group — and it will keep tracking the engine if a
    fifth reason is ever added there.

    Reading only: ``resolve_allocation_scope`` issues SELECTs and constructs a
    frozen :class:`AllocationScope`; it writes nothing and the engine stays
    byte-identical (plan C1).
    """
    if delivery_address_id is None:
        return False

    from business_app.services.allocation_scope import SCOPE_PLACE
    from shared.enums import CashCollectionSource

    scope = cash_service.resolve_allocation_scope(
        customer_id,
        delivery_address_id,
        CashCollectionSource.STANDALONE_MEETING,
    )
    return getattr(scope, "scope_type", None) == SCOPE_PLACE


def collectible_cod_total(
    *,
    cluster_total: Any,
    cluster_debt_count: Any,
    place_items: Iterable[Dict[str, Any]],
    cluster_user_ids: Iterable[Any],
) -> Tuple[float, int]:
    """``(amount, count)`` of open delivered COD debt this person can settle.

    :param cluster_total: the person's OWN delivered open COD debt, cluster-wide
        — ``cluster_delivered_outstanding_amount`` on a customer statement, or
        the collapsed ``total_outstanding_amount`` of a debtor row. The two are
        the same figure: both sum ``Payment.outstanding_amount`` over the
        cluster's CASH payments on DELIVERED orders
        (``cash_collection_service.py:1544-1561`` and ``:1935-1944``).
    :param cluster_debt_count: the matching debt count for that same set.
    :param place_items: rows from ``get_place_cod_statement(...)["items"]`` for
        the grouped place(s) this person belongs to. Pass ``()`` when no place
        resolves — the result is then just the cluster's own figure, which is
        exactly the un-widened row.
    :param cluster_user_ids: every account id of this one person.

    Pass ``cluster_total=0, cluster_debt_count=0`` to get the FOREIGN half on its
    own — that is how a debt-free coworker's synthesised row is built.

    Items owned by a cluster member are skipped: their own office order is
    already inside ``cluster_total``, and counting it here is the double-count
    that ring 2's ``if pid not in ring1_id_set`` exclusion also prevents in the
    engine. Items are additionally de-duplicated by ``payment_id`` so a caller
    may concatenate several places' items without needing to dedupe first.
    """
    cluster = {int(uid) for uid in cluster_user_ids}
    amount = float(cluster_total or 0)
    count = int(cluster_debt_count or 0)

    seen_payment_ids = set()
    for item in place_items or ():
        payment_id = item.get("payment_id")
        if payment_id is not None:
            if payment_id in seen_payment_ids:
                continue
            seen_payment_ids.add(payment_id)
        owner_user_id = item.get("owner_user_id")
        if owner_user_id is not None and int(owner_user_id) in cluster:
            continue
        amount += float(item.get("outstanding_amount") or 0)
        count += 1

    return amount, count


def resolve_collect_scope(statement: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """🔴 ONE decision: the figure a surface SHOWS **and** the scope it POSTS.

    THE DEFECT THIS EXISTS TO MAKE IMPOSSIBLE — three shipped instances of one
    shape: *a number is computed for a human, a scope is computed for the
    engine, and nothing forces them to describe the same set.*

    1. the debtor row (a UNION) vs the staff-bot ceiling (a ``max``);
    2. the degraded bot ceiling (cluster-only) vs a still-PLACE-scoped post;
    3. **the admin cash-collection modal** — it displayed the raw per-account
       ``total_outstanding_amount`` and posted ``places[0].address_id``. Measured
       on the A6 rows: shown 25 000, true ceiling 45 000; the admin collects the
       25 000 they were shown, Alice still owes 10 000 and **10 000 of BOB's
       debt was paid**. The advertised total named one person and settled
       another.
    4. **a grocery account at a shared place** — the engine FORCES personal
       scope for it, and this function handed back a PLACE scope with an address
       regardless. Closed at the source rather than here:
       :func:`place_widening_applies` now gates whether
       :meth:`StaffService.get_customer_cod_statement_for_staff` publishes a
       ceiling for that place at all, so a forced-personal account arrives here
       with none and travels the SAME degradation path as every other
       "no ceiling published" case below — address dropped together with the
       figure. No fourth branch was added; the existing one was made reachable.

    So this returns both halves from one call, and **every degradation drops the
    address together with the ceiling** — exactly the invariant
    ``CashCollectionHandler._scoped_ceiling`` enforces for the staff bot. A
    surface must never keep the place scope while falling back on the figure.

    ``statement`` is a ``get_customer_cod_statement`` payload, optionally
    enriched by :meth:`StaffService.get_customer_cod_statement_for_staff` with
    ``places[].place_collect_ceiling_amount``. Returns::

        {"scope_type": "place" | "cluster",
         "delivery_address_id": int | None,   # what to POST
         "amount": float, "debt_count": int,  # what to SHOW for that scope
         "cluster_amount": float,             # the un-widened fallback, published
         "cluster_debt_count": int}           # so no caller ever recomposes it

    PLACE applies only when the customer has EXACTLY ONE grouped place carrying
    a published ceiling. Two places is ambiguity (decision E7): guessing would
    settle the wrong workplace, so it degrades to cluster — mirroring the bot's
    ``_resolve_scope_address_id``. No ceiling published (gate off, or a backend
    older than the caller) also degrades, address included.

    DELIVERED-ONLY on both branches. The cluster fallback is
    ``cluster_delivered_outstanding_amount``, never the per-account
    ``total_outstanding_amount``: the engine's candidate rings select DELIVERED
    orders only (``cash_collection_service.py:183-196``, ``:245-259``), so cash
    offered against a PENDING order settles nothing. That difference is not
    academic — on the A6 rows with one pending order the admin modal displayed
    **95 000** where the collection could settle **45 000**.
    """
    payload = statement or {}
    cluster_amount = float(payload.get("cluster_delivered_outstanding_amount") or 0)
    cluster_count = int(payload.get("active_cod_debt_count") or 0)
    scope: Dict[str, Any] = {
        "scope_type": "cluster",
        "delivery_address_id": None,
        "amount": cluster_amount,
        "debt_count": cluster_count,
        "cluster_amount": cluster_amount,
        "cluster_debt_count": cluster_count,
    }

    places = payload.get("places") or []
    if len(places) != 1:
        return scope

    place = places[0]
    address_id = place.get("address_id")
    ceiling = place.get("place_collect_ceiling_amount")
    if address_id is None or ceiling is None:
        return scope
    try:
        amount = float(ceiling)
    except (TypeError, ValueError):
        return scope

    scope["scope_type"] = "place"
    scope["delivery_address_id"] = address_id
    scope["amount"] = amount
    scope["debt_count"] = int(place.get("place_collect_ceiling_debt_count") or 0)
    return scope
