import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// Namespace import (not a named import) on purpose: a missing named export is a
// hard ESM link error that would fail the whole file, hiding which behaviour
// regressed. This way each `it()` reports its own failure.
import BottleTracking, * as bottleTrackingModule from '../../pages/BottleTracking';
import adminService from '../../services/adminService';

// The spy map is DERIVED from the real service — never hand-listed.
//
// A hand-written method list is the mechanism that produced the blind spot: it
// is a snapshot of the service, so after `getBottleLedgerForAddress` was renamed
// to `getPlaceBottleLedger`, the stale name stayed on the mock, the page's stale
// call site kept resolving to a spy, and the suite stayed green while the button
// 404'd in production. Derived from `importActual`, a renamed method is simply
// absent from the mock, so the stale call site throws
// "adminService.getX is not a function" — loudly, at the first render.
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

// The methods this page depends on. Asserted against the REAL service below, so
// a backend-driven rename fails here with the missing name spelled out, instead
// of surfacing as an opaque render error.
const REQUIRED_SERVICE_METHODS = [
  'getBottleDashboard',
  'getBottleBalances',
  'getBottleLedger',
  'getBottleFines',
  'getBottleSessions',
  'getBottleTransfers',
  'getPlaceBottleLedger',
  'getCustomerPlaceSummary',
  'reconcileBottleBalance',
  // The write modals pick a PLACE, so the page searches addresses, not
  // customers — `getUsers` / `getUserDetails` / `getUserAddresses` left this
  // page with the member picker.
  'searchAddresses',
];

// Mirrors real i18next interpolation for defaultValue strings containing
// {{token}} placeholders, since there is no initialized i18next instance here.
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

// ---------------------------------------------------------------------------
// Payload contract
// ---------------------------------------------------------------------------
// The exact key set a balances row can carry, taken from the backend:
//   business_app/models/bottle.py           BottleBalance.to_dict()
//   business_app/serializers/bottle_serializers.py serialize_bottle_balance()
//
// Fixtures are validated against it so a fabricated key (this is how the dead
// `bottle_balance_id` / `user_name` / `customer_phone` fields survived the last
// re-key and kept the suite green) fails loudly instead of teaching the table
// to read a field the API never sends.
const BALANCE_ROW_KEYS = new Set([
  // BottleBalance.to_dict()
  'id',
  'address_group_id',
  'address_id',
  'balance',
  'last_delivery_at',
  'last_return_at',
  'notes',
  'created_at',
  'updated_at',
  // serialize_bottle_balance() additions
  'place_label',
  'member_names',
  'is_shared_place',
  'member_address_ids',
  'representative_address_id',
  // solo rows only — the serializer guards these with `if balance.address:`
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

// A shared place: `ck_bottle_balance_scope` forces `address_id IS NULL`, so the
// only id the row actions can send is `representative_address_id`.
const SHARED_PLACE_ROW = balanceRow({
  id: 16,
  address_group_id: 9,
  address_id: null,
  balance: 7,
  last_delivery_at: null,
  last_return_at: null,
  notes: null,
  created_at: null,
  updated_at: null,
  place_label: 'office',
  member_names: ['Test User', 'Co Worker'],
  is_shared_place: true,
  member_address_ids: [44, 45],
  representative_address_id: 44,
});

// A solo place: `address_id` is set and equals the representative id.
const SOLO_PLACE_ROW = balanceRow({
  id: 17,
  address_group_id: null,
  address_id: 51,
  balance: 2,
  last_delivery_at: null,
  last_return_at: null,
  notes: null,
  created_at: null,
  updated_at: null,
  place_label: 'Home',
  member_names: ['Solo Customer'],
  is_shared_place: false,
  member_address_ids: [51],
  representative_address_id: 51,
  address_title: 'Home',
  full_address: 'Solo street 1',
});

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

// "Ledger" is both a tab label and a row-action label, and the tab renders
// before the balances query resolves — so row actions must be looked up inside
// the table body, after the row is on screen.
const clickRowAction = async (label, rowText) => {
  await screen.findByText(rowText);
  const tbody = document.querySelector('.ant-table-tbody');
  fireEvent.click(within(tbody).getByText(label));
};

const renderBottleTracking = ({ balances = [], stats = {} } = {}) => {
  adminService.getBottleDashboard.mockResolvedValue({ data: stats });
  adminService.getBottleBalances.mockResolvedValue({
    data: { items: balances, total: balances.length },
  });
  return render(<BottleTracking />, { wrapper: createWrapper() });
};

describe('adminService contract', () => {
  it('exports every method this page calls', async () => {
    const actual = await vi.importActual('../../services/adminService');
    const missing = REQUIRED_SERVICE_METHODS.filter(
      (name) => typeof actual.default[name] !== 'function'
    );
    expect(missing).toEqual([]);
  });

  it('mocks the real method names, not a hand-written list', async () => {
    const actual = await vi.importActual('../../services/adminService');
    // Every spy corresponds to a real method — the mock cannot invent one, which
    // is what let a deleted method keep answering calls from the page.
    const invented = Object.keys(adminService).filter(
      (name) => typeof actual.default[name] !== 'function'
    );
    expect(invented).toEqual([]);
  });
});

describe('BottleTracking balances table — place-keyed rows', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getBottleLedger.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getBottleFines.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getBottleSessions.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getBottleTransfers.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getPlaceBottleLedger.mockResolvedValue({ data: { items: [] } });
    adminService.getCustomerPlaceSummary.mockResolvedValue({ data: { addresses: [] } });
    adminService.getClusterBottleLedger.mockResolvedValue({ data: { items: [] } });
    adminService.reconcileBottleBalance.mockResolvedValue({ data: { discrepancy: 0 } });
    adminService.searchAddresses.mockResolvedValue({ data: { addresses: [] } });
  });

  it('shows a place label and its members, not a user id', async () => {
    renderBottleTracking({ balances: [SHARED_PLACE_ROW] });

    expect(await screen.findByText('office')).toBeInTheDocument();
    expect(screen.getByText(/Co Worker/)).toBeInTheDocument();
    expect(screen.queryByText(/User #/)).not.toBeInTheDocument();
  });

  it('badges a shared place so its balance does not read as one person\'s', async () => {
    renderBottleTracking({ balances: [SHARED_PLACE_ROW, SOLO_PLACE_ROW] });

    expect(await screen.findByText('office')).toBeInTheDocument();
    // Exactly one of the two rows is shared.
    expect(screen.getAllByText('shared place')).toHaveLength(1);
  });

  it('never renders "Address #null" for a shared place', async () => {
    renderBottleTracking({ balances: [SHARED_PLACE_ROW] });

    expect(await screen.findByText('office')).toBeInTheDocument();
    expect(screen.queryByText(/Address #/)).not.toBeInTheDocument();
  });

  it('counts PLACES with a balance in the dashboard KPI', async () => {
    renderBottleTracking({
      balances: [],
      stats: {
        total_bottles_out: 30,
        places_with_balance: 12,
        active_fines: 4,
        total_fine_amount: 99000,
      },
    });

    expect(await screen.findByText('Places with Balance')).toBeInTheDocument();
    expect(await screen.findByText('12')).toBeInTheDocument();
    expect(screen.queryByText('Customers with Balance')).not.toBeInTheDocument();
  });

  it('opens the ledger drawer using the representative address id', async () => {
    renderBottleTracking({ balances: [SHARED_PLACE_ROW] });

    await clickRowAction('Ledger', 'office');

    await waitFor(() => {
      expect(adminService.getPlaceBottleLedger).toHaveBeenCalledWith(
        44,
        expect.objectContaining({ per_page: 50 })
      );
    });
  });

  // The bug this pins: `reconcileBottleBalance(record.user_id, record.address_id)`
  // did NOT throw after the service was re-signatured to one argument — it
  // silently posted the (now always undefined) user id into the address slot,
  // i.e. reconciled whatever place that id happened to name. Arity is asserted
  // exactly so a re-introduced two-arg call cannot pass.
  it('reconciles a shared place by its representative address id, with one argument', async () => {
    renderBottleTracking({ balances: [SHARED_PLACE_ROW] });

    await clickRowAction('Reconcile', 'office');

    await waitFor(() => {
      expect(adminService.reconcileBottleBalance).toHaveBeenCalled();
    });
    expect(adminService.reconcileBottleBalance.mock.calls[0]).toEqual([44]);
  });

  it('reconciles a solo place by its own address id, with one argument', async () => {
    renderBottleTracking({ balances: [SOLO_PLACE_ROW] });

    await clickRowAction('Reconcile', 'Home');

    await waitFor(() => {
      expect(adminService.reconcileBottleBalance).toHaveBeenCalled();
    });
    expect(adminService.reconcileBottleBalance.mock.calls[0]).toEqual([51]);
  });

  it('opens the adjust modal with the place address prefilled', async () => {
    renderBottleTracking({ balances: [SHARED_PLACE_ROW] });

    await clickRowAction('Adjust', 'office');

    await screen.findByText('Adjust Bottle Balance');
    await waitFor(() => {
      const selected = document.querySelector('.ant-modal .ant-select-selection-item');
      expect(selected?.textContent).toBe('44');
    });
  });
});

describe('BottleTracking place write-modal prefill (D3, post-Task-7)', () => {
  // A place write names an address and nothing else. The prefill used to write
  // `user_id: undefined` FIRST, because the customer picker wiped `address_id`
  // on every user change (hazard H8) — that picker is gone, and asserting the
  // exact call list is what stops the member concept creeping back in through
  // the prefill.
  const recordingForm = () => {
    const calls = [];
    return { calls, setFieldValue: (name, value) => calls.push([name, value]) };
  };

  it('prefills the address alone for a shared place', () => {
    const form = recordingForm();

    bottleTrackingModule.prefillPlaceWriteForm(form, {
      address_id: null,
      representative_address_id: 44,
    });

    expect(form.calls).toEqual([['address_id', 44]]);
  });

  it('prefills the address alone for a solo place', () => {
    const form = recordingForm();

    bottleTrackingModule.prefillPlaceWriteForm(form, {
      address_id: 51,
      representative_address_id: 51,
    });

    expect(form.calls).toEqual([['address_id', 51]]);
  });
});

describe('BottleTracking place search folding', () => {
  // A shared place answers the address search once per member; two options for
  // one place would be the coworker choice all over again — with no effect,
  // since every member id resolves to the same place and the same derived
  // attribution.
  it('folds a shared place\'s member hits into one option at the lowest member id', () => {
    const folded = bottleTrackingModule.foldSearchHitsToPlaces([
      { address_id: 45, address_group_id: 9 },
      { address_id: 44, address_group_id: 9 },
      { address_id: 51, address_group_id: null },
    ]);

    expect(folded.map((p) => p.address_id)).toEqual([44, 51]);
  });

  it('is order-independent, so the same place always yields the same id', () => {
    const hits = [
      { address_id: 44, address_group_id: 9 },
      { address_id: 45, address_group_id: 9 },
    ];
    expect(bottleTrackingModule.foldSearchHitsToPlaces(hits)[0].address_id).toBe(44);
    expect(bottleTrackingModule.foldSearchHitsToPlaces([...hits].reverse())[0].address_id).toBe(44);
  });
});
