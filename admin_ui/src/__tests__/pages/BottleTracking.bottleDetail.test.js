import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import BottleTracking from '../../pages/BottleTracking';
import adminService from '../../services/adminService';

// The spy map is DERIVED from the real service — never hand-listed. See the
// twin block in BottleTracking.test.js: a literal method list is a snapshot of
// the service, so a renamed call site keeps resolving to a spy and the file
// stays green while the button 404s. This file demonstrated exactly that for a
// whole task, with `getBottleLedgerForAddress` / `getCustomerBottleBalances`
// still mocked after both had been deleted.
vi.mock('../../services/adminService', async () => {
  const actual = await vi.importActual('../../services/adminService');
  const real = actual.default;
  // AdminService is a class, so its methods live on the prototype.
  const names = new Set();
  for (let obj = real; obj && obj !== Object.prototype; obj = Object.getPrototypeOf(obj)) {
    for (const name of Object.getOwnPropertyNames(obj)) {
      if (name !== 'constructor' && typeof real[name] === 'function') {
        names.add(name);
      }
    }
  }
  return {
    __esModule: true,
    default: Object.fromEntries([...names].map((name) => [name, vi.fn()])),
  };
});

// Mirrors real i18next interpolation for defaultValue strings containing
// {{token}} placeholders, since there is no initialized i18next instance in
// this test.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => {
      let value = (opts && opts.defaultValue) || key;
      if (opts) {
        value = value.replace(/\{\{(\w+)\}\}/g, (_, token) => (
          // eslint-disable-next-line security/detect-object-injection
          opts[token] !== undefined ? String(opts[token]) : `{{${token}}}`
        ));
      }
      return value;
    },
  }),
}));

// Balance rows are PLACES. The exact key set the backend can emit lives in
// business_app/models/bottle.py (BottleBalance.to_dict) plus
// business_app/serializers/bottle_serializers.py (serialize_bottle_balance).
// Validating fixtures against it is what stops this file drifting back into a
// blind spot: it previously fabricated `user_id` / `customer_name` /
// `customer_phone` / `bottle_balance_id`, none of which the API ever sent.
const BALANCE_ROW_KEYS = new Set([
  'id',
  'address_group_id',
  'address_id',
  'balance',
  'last_delivery_at',
  'last_return_at',
  'notes',
  'created_at',
  'updated_at',
  'place_label',
  'member_names',
  'is_shared_place',
  'member_address_ids',
  'representative_address_id',
  'address_title',
  'full_address',
]);

const balanceRow = (row) => {
  const fabricated = Object.keys(row).filter((k) => !BALANCE_ROW_KEYS.has(k));
  if (fabricated.length) {
    throw new Error(
      `Balance-row fixture invents keys the backend never emits: ${fabricated.join(', ')}. ` +
      'See serialize_bottle_balance in business_app/serializers/bottle_serializers.py.'
    );
  }
  return row;
};

// `get_customer_summary` (bottle_tracking_service.py) — the fine modal's
// balance-context payload. Declared and validated for the same reason as the
// balance rows: this fixture used to fabricate `total_balance`,
// `cluster_total_balance`, `bottle_balance_id` and `group_union_balance`, none of
// which the API had ever sent. The sets themselves are pinned against the live
// payload by tests/unit/test_admin_ui_payload_fixture_contracts.py, so a backend
// rename cannot leave BOTH the fixture and the set stale and still go green.
const CUSTOMER_SUMMARY_KEYS = new Set([
  'user_id',
  'addresses',
  'active_fines_count',
  'total_fine_amount',
  'is_linked',
  'cluster_member_ids',
  'cluster_scopes',
]);

const CUSTOMER_SUMMARY_ADDRESS_KEYS = new Set([
  'address_id',
  'address_title',
  'full_address',
  'place_balance',
  'last_delivery_at',
  'last_return_at',
  'address_group_id',
  'is_grouped',
]);

const CUSTOMER_SUMMARY_SCOPE_KEYS = new Set([
  'address_group_id',
  'address_id',
  'balance',
  'is_shared',
]);

const assertKeysMatch = (obj, allowed, what) => {
  const fabricated = Object.keys(obj).filter((k) => !allowed.has(k));
  if (fabricated.length) {
    throw new Error(
      `${what} fixture invents keys the backend never emits: ${fabricated.join(', ')}.`
    );
  }
  // Missing keys matter as much as extra ones: a fixture that quietly drops a
  // field teaches the component nothing about what the API really sends.
  const missing = [...allowed].filter((k) => !(k in obj));
  if (missing.length) {
    throw new Error(`${what} fixture is missing keys the backend always emits: ${missing.join(', ')}.`);
  }
  return obj;
};

const customerSummary = (summary) => {
  assertKeysMatch(summary, CUSTOMER_SUMMARY_KEYS, 'Customer summary');
  summary.addresses.forEach((a) =>
    assertKeysMatch(a, CUSTOMER_SUMMARY_ADDRESS_KEYS, 'Customer summary address'));
  summary.cluster_scopes.forEach((s) =>
    assertKeysMatch(s, CUSTOMER_SUMMARY_SCOPE_KEYS, 'Customer summary cluster scope'));
  return summary;
};

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

describe('BottleTracking place detail drawer', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    adminService.getBottleDashboard.mockResolvedValue({ data: {} });
    adminService.getBottleBalances.mockResolvedValue({
      data: {
        items: [
          balanceRow({
            id: 501,
            address_group_id: 5,
            address_id: null,
            balance: 7,
            last_delivery_at: null,
            last_return_at: null,
            notes: null,
            created_at: null,
            updated_at: null,
            place_label: 'Acme office',
            member_names: ['Jane Doe', 'Bob Coworker'],
            is_shared_place: true,
            member_address_ids: [7, 9],
            representative_address_id: 7,
          }),
        ],
        total: 1,
      },
    });
    adminService.getBottleLedger.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getBottleFines.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getBottleSessions.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getBottleTransfers.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getPlaceBottleLedger.mockResolvedValue({ data: { items: [] } });
    adminService.getClusterBottleLedger.mockResolvedValue({ data: { items: [] } });
    adminService.getCustomerPlaceSummary.mockResolvedValue({ data: { addresses: [] } });
    adminService.searchAddresses.mockResolvedValue({ data: { addresses: [] } });
  });

  // The place label is printed in the row, in the ledger drawer AND in the
  // detail drawer, so row actions are located inside the table body only —
  // never by a document-wide text query.
  const openRowAction = async (label) => {
    const tbody = await waitFor(() => {
      const body = document.querySelector('.ant-table-tbody');
      expect(within(body).getByText('Acme office')).toBeInTheDocument();
      return body;
    });
    fireEvent.click(within(tbody).getByText(label));
  };

  const openPlaceDrawer = async () => {
    await openRowAction('Details');
    // Everything the drawer shows is also in the row behind it (label, members,
    // balance), so every assertion has to be scoped to this drawer's body.
    const title = await screen.findByText('Customer Bottle Detail');
    return title.closest('.ant-drawer-content').querySelector('.ant-drawer-body');
  };

  // A place row carries no `user_id` at all (BLOCKER-2), so nothing behind it
  // may drive a user-keyed fetch. Both of these are cluster/customer-scoped and
  // must stay dormant.
  it('drives no user-keyed fetch from a place row', async () => {
    render(<BottleTracking />, { wrapper: createWrapper() });

    await openPlaceDrawer();

    expect(adminService.getCustomerPlaceSummary).not.toHaveBeenCalled();
    expect(adminService.getClusterBottleLedger).not.toHaveBeenCalled();
  });

  it('does not call the customer detail endpoints before the drawer is opened', () => {
    render(<BottleTracking />, { wrapper: createWrapper() });

    expect(adminService.getCustomerPlaceSummary).not.toHaveBeenCalled();
    expect(adminService.getClusterBottleLedger).not.toHaveBeenCalled();
  });

  // THE headline defect: the drawer headed a CLUSTER-scoped table (the people
  // axis) with a PLACE total, so for two unlinked coworkers it printed
  // "7 at this place" above a ledger that summed to one person's 6.
  it('feeds the drawer ledger from the place, not the cluster', async () => {
    render(<BottleTracking />, { wrapper: createWrapper() });

    const drawer = await openPlaceDrawer();

    await waitFor(() => {
      expect(adminService.getPlaceBottleLedger).toHaveBeenCalledWith(
        7,
        expect.objectContaining({ per_page: 20 })
      );
    });
    expect(adminService.getClusterBottleLedger).not.toHaveBeenCalled();
    expect(within(drawer).getByText('Place Ledger')).toBeInTheDocument();
  });

  it('shows one pool for the place with its members, not two per-person totals', async () => {
    render(<BottleTracking />, { wrapper: createWrapper() });

    const drawer = await openPlaceDrawer();

    expect(within(drawer).getByText('Acme office')).toBeInTheDocument();
    expect(within(drawer).getByText('Jane Doe, Bob Coworker')).toBeInTheDocument();
    expect(within(drawer).getByText('Bottles at this place')).toBeInTheDocument();
    expect(within(drawer).getByText('7')).toBeInTheDocument();
    expect(
      within(drawer).getByText('Shared place — one pool across 2 accounts')
    ).toBeInTheDocument();
    // `total_balance` and `cluster_total_balance` were both deleted from the
    // backend; rendering either could only ever show a permanent 0.
    expect(screen.queryByText('This account only')).not.toBeInTheDocument();
    expect(screen.queryByText('Combined across linked accounts')).not.toBeInTheDocument();
  });

  // Both drawers read the SAME place through the SAME endpoint at different
  // page sizes. On one shared query key React Query dedupes the in-flight
  // fetch, so whichever drawer mounts second silently renders the other's page.
  it('gives the two place-ledger drawers their own cache entry', async () => {
    const pending = [];
    adminService.getPlaceBottleLedger.mockImplementation(
      () => new Promise((resolve) => pending.push(() => resolve({ data: { items: [] } })))
    );

    render(<BottleTracking />, { wrapper: createWrapper() });

    await openRowAction('Ledger');
    await openRowAction('Details');

    await waitFor(() => expect(adminService.getPlaceBottleLedger).toHaveBeenCalledTimes(2));
    expect(
      adminService.getPlaceBottleLedger.mock.calls.map(([addressId, params]) => [
        addressId,
        params.per_page,
      ])
    ).toEqual(expect.arrayContaining([[7, 50], [7, 20]]));

    await act(async () => {
      pending.forEach((resolve) => resolve());
    });
  });
});

describe('BottleTracking create fine modal — balance context', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    adminService.getBottleDashboard.mockResolvedValue({ data: {} });
    adminService.getBottleBalances.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getBottleLedger.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getBottleFines.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getBottleSessions.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getBottleTransfers.mockResolvedValue({ data: { items: [], total: 0 } });

    // The fine modal picks a PLACE — `GET /admin/addresses/search`, one hit per
    // member address, folded to one option per place by the picker.
    adminService.searchAddresses.mockResolvedValue({
      data: {
        addresses: [
          {
            address_id: 7,
            title: 'Home',
            full_address: '123 Main St',
            address_group_id: 5,
            owner: { id: 42, first_name: 'Jane', last_name: 'Doe', phone: '+998901234567' },
          },
        ],
      },
    });
    // `get_customer_summary` shape (bottle_tracking_service.py:724-735): the
    // address row carries ONE number, `place_balance`, which is the whole
    // place's pool. There is no per-(user,address) "pair balance" any more, so
    // there is nothing for it to disagree with.
    adminService.getCustomerPlaceSummary.mockResolvedValue({
      data: customerSummary({
        user_id: 42,
        addresses: [
          {
            address_id: 7,
            address_title: 'Home',
            full_address: '123 Main St',
            place_balance: 8,
            last_delivery_at: null,
            last_return_at: null,
            address_group_id: 5,
            is_grouped: true,
          },
        ],
        active_fines_count: 0,
        total_fine_amount: 0,
        is_linked: true,
        cluster_member_ids: [42, 43],
        cluster_scopes: [{ address_group_id: 5, address_id: null, balance: 8, is_shared: true }],
      }),
    });
  });

  // ONE control, and it names a place. There is no customer step any more:
  // the admin fines the place, and the backend derives the audit attribution.
  const selectPlace = async () => {
    fireEvent.click(screen.getByText('Fines'));
    fireEvent.click(await screen.findByText('Create Fine'));

    await screen.findByText('Create Bottle Fine');
    const placeInput = document.getElementById('address_id');
    fireEvent.mouseDown(placeInput);
    fireEvent.change(placeInput, { target: { value: 'Home' } });

    fireEvent.click(await screen.findByText('Home, 123 Main St — shared place'));
  };

  it('shows the place balance for a grouped address', async () => {
    render(<BottleTracking />, { wrapper: createWrapper() });

    await selectPlace();

    await waitFor(() => {
      expect(adminService.getCustomerPlaceSummary).toHaveBeenCalledWith(42);
    });

    expect(await screen.findByText('Balance: 8')).toBeInTheDocument();
    expect(screen.getByText('grouped')).toBeInTheDocument();
    // The old "pair balance vs place union" duality is gone: `place_balance`
    // IS the combined figure, so the whole second line must be absent — not
    // merely carrying a different number. Matching the label (rather than
    // "…: 8") is what makes this fail while the block still renders its raw
    // `{{value}}` template against the deleted `group_union_balance`.
    expect(screen.queryByText(/Place union balance/)).not.toBeInTheDocument();
  });

  it('shows no balance context before a place is chosen', async () => {
    render(<BottleTracking />, { wrapper: createWrapper() });

    fireEvent.click(screen.getByText('Fines'));
    fireEvent.click(await screen.findByText('Create Fine'));

    expect(screen.queryByText(/^Balance: /)).not.toBeInTheDocument();
    expect(adminService.getCustomerPlaceSummary).not.toHaveBeenCalled();
  });
});
