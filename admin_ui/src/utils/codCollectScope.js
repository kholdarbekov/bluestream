// 🔴 ONE decision: the figure the admin SEES and the scope the admin POSTS.
//
// THE DEFECT THIS MODULE EXISTS TO MAKE IMPOSSIBLE — three shipped instances of
// one shape: *a number is computed for a human, a scope is computed for the
// engine, and nothing forces them to describe the same set.*
//
//   1. the driver's debtor row (a UNION) vs the staff-bot ceiling (a `max`);
//   2. the degraded bot ceiling (cluster-only) vs a still-PLACE-scoped post;
//   3. THIS SCREEN. `DeliveryReports.js` posted `places[0].address_id` — PLACE
//      scope, settling the workplace's debts as well as the customer's — while
//      displaying the raw per-account `total_outstanding_amount`. Measured on
//      real rows: shown 25 000, true ceiling 45 000. The admin collects the
//      25 000 they were shown, the customer still owes 10 000, and 10 000 of a
//      COWORKER'S debt was paid instead. With a pending order in the mix the
//      displayed figure was 95 000 against the same 45 000 ceiling.
//
// The rule, in one line: **an address is posted ONLY together with that
// address's own published ceiling; every degradation drops the address too.**
// That is the invariant `CashCollectionHandler._scoped_ceiling` enforces in the
// staff bot, and `resolve_collect_scope` (business_app/services/
// cod_collect_ceiling.py) resolves server-side. Nothing here recomputes either
// half — every number below is READ off `statement.collect_scope`.

// Only cash physically handed over may resolve PLACE scope. The backend is the
// authority (`CashCollectionService._PLACE_SCOPE_SOURCES`); of the four sources
// this modal offers, `standalone_meeting` is the only member. Sending the
// address for the others would be a lie the backend silently ignores.
export const PLACE_SCOPED_COLLECTION_SOURCES = Object.freeze(['standalone_meeting']);

const asNumber = (value) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

/**
 * Resolve BOTH halves of an admin cash collection from one published object.
 *
 * @param {object|null} statement `GET .../customers/<id>/statement` payload
 * @param {string|undefined} source the selected collection type
 * @returns {{deliveryAddressId: number|null, amount: number, debtCount: number,
 *            scopeType: 'place'|'cluster'}}
 *   `amount` / `debtCount` describe EXACTLY the debts a submit carrying
 *   `deliveryAddressId` will settle. Display one and post the other and they
 *   cannot disagree.
 *
 * Degrades to cluster scope — address dropped — when: the source cannot be
 * place-scoped; the customer has no single unambiguous grouped place (two
 * places is ambiguity, and guessing settles the wrong workplace); or the
 * backend published no ceiling at all, which is what a business_app older than
 * this bundle serves and what the gate-off rollback serves.
 */
export function resolveCollectScope(statement, source) {
  const scope = statement && statement.collect_scope;

  // No `collect_scope` at all => an older backend. Post NO address (never a
  // place-scoped settlement behind an un-ceilinged figure) and show the only
  // figure that payload carries.
  if (!scope) {
    return {
      deliveryAddressId: null,
      amount: asNumber(statement && statement.total_outstanding_amount),
      debtCount: asNumber(statement && statement.active_cod_debt_count),
      scopeType: 'cluster',
    };
  }

  const cluster = {
    deliveryAddressId: null,
    amount: asNumber(scope.cluster_amount),
    debtCount: asNumber(scope.cluster_debt_count),
    scopeType: 'cluster',
  };

  if (!PLACE_SCOPED_COLLECTION_SOURCES.includes(source)) return cluster;
  if (scope.scope_type !== 'place') return cluster;
  if (scope.delivery_address_id == null || scope.amount == null) return cluster;

  return {
    deliveryAddressId: scope.delivery_address_id,
    amount: asNumber(scope.amount),
    debtCount: asNumber(scope.debt_count),
    scopeType: 'place',
  };
}

export default resolveCollectScope;
