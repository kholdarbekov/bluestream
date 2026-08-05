import { describe, it, expect } from 'vitest';

import { resolveCollectScope } from '../../utils/codCollectScope';

// The canonical A6 rows: Alice owes 10 000 at an ungrouped home + 15 000 at
// office G; Bob owes 20 000 at G. Her raw per-account headline is 25 000; the
// figure a place-scoped collection actually settles is 45 000.
const aliceStatement = (overrides = {}) => ({
  total_outstanding_amount: 25000,
  active_cod_debt_count: 2,
  places: [{ address_id: 2, place_group_id: 9, place_open_cod_debt_total: 35000 }],
  collect_scope: {
    scope_type: 'place',
    delivery_address_id: 2,
    amount: 45000,
    debt_count: 3,
    cluster_amount: 25000,
    cluster_debt_count: 2,
  },
  ...overrides,
});

describe('resolveCollectScope', () => {
  it('returns the published ceiling together with the address it belongs to', () => {
    // The defect: 25 000 was shown while address 2 was posted, so the admin's
    // 25 000 settled 10 000 of a coworker's debt.
    const scope = resolveCollectScope(aliceStatement(), 'standalone_meeting');

    expect(scope).toEqual({
      deliveryAddressId: 2,
      amount: 45000,
      debtCount: 3,
      scopeType: 'place',
    });
  });

  it('drops the address whenever it drops the ceiling', () => {
    // 🔴 The invariant, stated as one table: every degradation must move BOTH
    // halves. An address surviving a ceiling fallback is the P0-degraded defect.
    const degradations = [
      ['no collect_scope at all (older backend)', aliceStatement({ collect_scope: undefined })],
      ['backend resolved cluster scope (two places / gate off)', aliceStatement({
        collect_scope: {
          scope_type: 'cluster',
          delivery_address_id: null,
          amount: 25000,
          debt_count: 2,
          cluster_amount: 25000,
          cluster_debt_count: 2,
        },
      })],
      ['a scope_type of place with no address', aliceStatement({
        collect_scope: {
          scope_type: 'place',
          delivery_address_id: null,
          amount: 45000,
          debt_count: 3,
          cluster_amount: 25000,
          cluster_debt_count: 2,
        },
      })],
      ['a scope_type of place with no amount', aliceStatement({
        collect_scope: {
          scope_type: 'place',
          delivery_address_id: 2,
          amount: null,
          debt_count: 3,
          cluster_amount: 25000,
          cluster_debt_count: 2,
        },
      })],
    ];

    degradations.forEach(([why, statement]) => {
      const scope = resolveCollectScope(statement, 'standalone_meeting');
      expect(scope.deliveryAddressId, why).toBeNull();
      expect(scope.scopeType, why).toBe('cluster');
      expect(scope.amount, why).toBe(25000);
    });
  });

  it('never place-scopes a source the backend refuses place scope for', () => {
    // admin_adjustment / backfill / personal_card_transfer are book corrections
    // or the payer's own money — `_PLACE_SCOPE_SOURCES` excludes all three, so
    // sending the address would be a lie the backend silently ignores, and
    // showing the place figure would promise a settlement that cannot happen.
    ['admin_adjustment', 'backfill', 'personal_card_transfer', undefined].forEach((source) => {
      const scope = resolveCollectScope(aliceStatement(), source);
      expect(scope.deliveryAddressId, source).toBeNull();
      expect(scope.amount, source).toBe(25000);
      expect(scope.debtCount, source).toBe(2);
    });
  });

  it('falls back to the legacy figure with no address when there is no statement', () => {
    expect(resolveCollectScope(null, 'standalone_meeting')).toEqual({
      deliveryAddressId: null,
      amount: 0,
      debtCount: 0,
      scopeType: 'cluster',
    });
  });
});
