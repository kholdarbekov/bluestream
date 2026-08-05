import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import BottleTracking from '../../pages/BottleTracking';
import adminService from '../../services/adminService';

// Plan C Task 7: the admin bottle write modals name a PLACE, never a member.
//
// The owner's model is that one person places the order and there is no
// coworker selection or separation anywhere afterwards, so Adjust / Initial
// Balance / Fine must not ask "which customer?" — and must not send `user_id`.
// The backend derives the audit attribution from the place's representative
// address; `bottle_balances` has no `user_id` column at all, so nothing about a
// balance depends on the answer.
//
// Payloads are asserted EXACTLY (`toEqual`, not `objectContaining`): a stray
// `user_id: undefined` still serialises the member concept back into the
// request body, and an `objectContaining` assertion would not see it.

// Derived spy map — see BottleTracking.test.js for why a hand-written method
// list is the mechanism that hid a rename from the suite.
vi.mock('../../services/adminService', async () => {
  const actual = await vi.importActual('../../services/adminService');
  const real = actual.default;
  const names = new Set();
  for (let obj = real; obj && obj !== Object.prototype; obj = Object.getPrototypeOf(obj)) {
    for (const name of Object.getOwnPropertyNames(obj)) {
      // eslint-disable-next-line security/detect-object-injection
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

// Fixtures are validated against the backend's key sets, and those sets are
// themselves pinned against the live payloads by
// tests/unit/test_admin_ui_payload_fixture_contracts.py — otherwise a rename
// leaves the fixture and the hand-copied set stale TOGETHER and still green.
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

// CustomerLinkService.search_addresses() — one row PER ADDRESS.
const ADDRESS_SEARCH_HIT_KEYS = new Set([
  'address_id',
  'title',
  'full_address',
  'address_group_id',
  'owner',
]);

const ADDRESS_SEARCH_OWNER_KEYS = new Set(['id', 'first_name', 'last_name', 'phone']);

const validate = (obj, allowed, what) => {
  const fabricated = Object.keys(obj).filter((k) => !allowed.has(k));
  if (fabricated.length) {
    throw new Error(`${what} fixture invents keys the backend never emits: ${fabricated.join(', ')}`);
  }
  return obj;
};

const balanceRow = (row) => validate(row, BALANCE_ROW_KEYS, 'Balance-row');
const searchHit = (hit) => {
  validate(hit, ADDRESS_SEARCH_HIT_KEYS, 'Address-search-hit');
  validate(hit.owner || {}, ADDRESS_SEARCH_OWNER_KEYS, 'Address-search-owner');
  return hit;
};

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

// `GET /admin/addresses/search` shape (CustomerLinkService.search_addresses).
// A shared place returns ONE HIT PER MEMBER ADDRESS — the picker has to fold
// them back into a single place, or the admin is choosing a coworker again.
const SHARED_HITS = [
  searchHit({
    address_id: 45,
    title: 'work',
    full_address: '1 Office St',
    address_group_id: 9,
    owner: { id: 2, first_name: 'Co', last_name: 'Worker', phone: '+998901234570' },
  }),
  searchHit({
    address_id: 44,
    title: 'work',
    full_address: '1 Office St',
    address_group_id: 9,
    owner: { id: 1, first_name: 'Test', last_name: 'User', phone: '+998901234567' },
  }),
];

const SOLO_HIT = searchHit({
  address_id: 51,
  title: 'Home',
  full_address: 'Solo street 1',
  address_group_id: null,
  owner: { id: 3, first_name: 'Solo', last_name: 'Customer', phone: '+998901234599' },
});

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

const renderPage = ({ balances = [] } = {}) => {
  adminService.getBottleBalances.mockResolvedValue({
    data: { items: balances, total: balances.length },
  });
  return render(<BottleTracking />, { wrapper: createWrapper() });
};

const clickRowAction = async (user, label, rowText) => {
  await screen.findByText(rowText);
  const tbody = document.querySelector('.ant-table-tbody');
  await user.click(within(tbody).getByText(label));
};

const currentModal = () => {
  const modals = [...document.querySelectorAll('.ant-modal-wrap')].filter(
    (m) => m.style.display !== 'none'
  );
  return modals[modals.length - 1];
};

const pickPlace = async (user, labelPattern) => {
  const picker = within(currentModal()).getByRole('combobox');
  await user.click(picker);
  await user.type(picker, 'off');
  const dropdown = await waitFor(() => {
    const el = document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden)');
    expect(el).not.toBeNull();
    return el;
  });
  await user.click(await within(dropdown).findByText(labelPattern));
};

const typeInto = async (user, modal, labelText, value) => {
  const item = [...modal.querySelectorAll('.ant-form-item')].find((el) =>
    el.querySelector('label')?.textContent?.includes(labelText)
  );
  await user.type(within(item).getByRole(item.querySelector('textarea') ? 'textbox' : 'spinbutton'), value);
};

const submitModal = async (user) => {
  await user.click(within(currentModal()).getByText('OK').closest('button'));
};

beforeEach(() => {
  vi.clearAllMocks();
  adminService.getBottleDashboard.mockResolvedValue({ data: {} });
  adminService.getBottleLedger.mockResolvedValue({ data: { items: [], total: 0 } });
  adminService.getBottleFines.mockResolvedValue({ data: { items: [], total: 0 } });
  adminService.getBottleSessions.mockResolvedValue({ data: { items: [], total: 0 } });
  adminService.getBottleTransfers.mockResolvedValue({ data: { items: [], total: 0 } });
  adminService.getPlaceBottleLedger.mockResolvedValue({ data: { items: [] } });
  adminService.getCustomerPlaceSummary.mockResolvedValue({ data: { addresses: [] } });
  adminService.searchAddresses.mockResolvedValue({ data: { addresses: [] } });
  adminService.createBottleAdjustment.mockResolvedValue({ data: {} });
  adminService.setBottleInitialBalance.mockResolvedValue({ data: {} });
  adminService.createBottleFine.mockResolvedValue({ data: {} });
});

describe('bottle write modals ask for a place, not a member', () => {
  it.each([
    ['Set Initial Balance', 'Set Initial Bottle Balance'],
    ['Adjust Balance', 'Adjust Bottle Balance'],
  ])('%s renders no customer picker', async (button, title) => {
    const user = userEvent.setup();
    renderPage({ balances: [SHARED_PLACE_ROW] });

    await user.click(await screen.findByText(button));

    const modal = within(await screen.findByText(title).then(() => currentModal()));
    expect(modal.queryByText('Customer')).toBeNull();
    expect(modal.queryByText(/Search by phone, name, or company/)).toBeNull();
    expect(adminService.getUsers).not.toHaveBeenCalled();
    expect(adminService.getUserAddresses).not.toHaveBeenCalled();
  });

  it('Create Fine renders no customer picker', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByText('Fines'));
    await user.click(await screen.findByText('Create Fine'));
    await screen.findByText('Create Bottle Fine');

    const modal = within(currentModal());
    expect(modal.queryByText('Customer')).toBeNull();
    expect(adminService.getUsers).not.toHaveBeenCalled();
    expect(adminService.getUserAddresses).not.toHaveBeenCalled();
  });

  it('searches places including grouped addresses, folded to one option per place', async () => {
    const user = userEvent.setup();
    adminService.searchAddresses.mockResolvedValue({ data: { addresses: SHARED_HITS } });
    renderPage();

    await user.click(await screen.findByText('Set Initial Balance'));
    await screen.findByText('Set Initial Bottle Balance');
    const picker = within(currentModal()).getByRole('combobox');
    await user.click(picker);
    await user.type(picker, 'off');

    // exclude_grouped=false: a shared place's members ARE grouped addresses, so
    // the default would make every shared place unreachable from these modals.
    await waitFor(() =>
      expect(adminService.searchAddresses).toHaveBeenCalledWith('off', false)
    );
    const dropdown = await waitFor(() => {
      const el = document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden)');
      expect(el).not.toBeNull();
      return el;
    });
    // Two member hits, ONE place option — picking between coworkers is not a choice.
    expect(dropdown.querySelectorAll('.ant-select-item-option')).toHaveLength(1);
  });
});

describe('Adjust modal writes to the place with no member', () => {
  it('sends only the shared place address id', async () => {
    const user = userEvent.setup();
    renderPage({ balances: [SHARED_PLACE_ROW] });

    await clickRowAction(user, 'Adjust', 'office');
    const modal = await screen.findByText('Adjust Bottle Balance').then(() => currentModal());
    await typeInto(user, modal, 'Adjustment', '3');
    await typeInto(user, modal, 'Notes', 'recount');
    await submitModal(user);

    await waitFor(() => expect(adminService.createBottleAdjustment).toHaveBeenCalled());
    expect(adminService.createBottleAdjustment.mock.calls[0][0]).toEqual({
      address_id: 44,
      adjustment: 3,
      notes: 'recount',
    });
  });

  it('sends only the solo place address id', async () => {
    const user = userEvent.setup();
    renderPage({ balances: [SOLO_PLACE_ROW] });

    await clickRowAction(user, 'Adjust', 'Home');
    const modal = await screen.findByText('Adjust Bottle Balance').then(() => currentModal());
    await typeInto(user, modal, 'Adjustment', '2');
    await typeInto(user, modal, 'Notes', 'recount');
    await submitModal(user);

    await waitFor(() => expect(adminService.createBottleAdjustment).toHaveBeenCalled());
    expect(adminService.createBottleAdjustment.mock.calls[0][0]).toEqual({
      address_id: 51,
      adjustment: 2,
      notes: 'recount',
    });
  });
});

describe('Initial Balance modal writes to the place with no member', () => {
  it.each([
    ['shared', SHARED_HITS, /shared place/, 44],
    ['solo', [SOLO_HIT], /Solo Customer/, 51],
  ])('sends only the %s place address id', async (_kind, hits, optionLabel, expectedId) => {
    const user = userEvent.setup();
    adminService.searchAddresses.mockResolvedValue({ data: { addresses: hits } });
    renderPage();

    await user.click(await screen.findByText('Set Initial Balance'));
    await screen.findByText('Set Initial Bottle Balance');
    await pickPlace(user, optionLabel);
    await typeInto(user, currentModal(), 'Bottle Quantity', '6');
    await submitModal(user);

    await waitFor(() => expect(adminService.setBottleInitialBalance).toHaveBeenCalled());
    expect(adminService.setBottleInitialBalance.mock.calls[0][0]).toEqual({
      address_id: expectedId,
      quantity: 6,
    });
  });
});

describe('Fine modal writes to the place with no member', () => {
  it.each([
    ['shared', SHARED_HITS, /shared place/, 44],
    ['solo', [SOLO_HIT], /Solo Customer/, 51],
  ])('sends only the %s place address id', async (_kind, hits, optionLabel, expectedId) => {
    const user = userEvent.setup();
    adminService.searchAddresses.mockResolvedValue({ data: { addresses: hits } });
    renderPage();

    await user.click(await screen.findByText('Fines'));
    await user.click(await screen.findByText('Create Fine'));
    await screen.findByText('Create Bottle Fine');
    await pickPlace(user, optionLabel);
    await typeInto(user, currentModal(), 'Bottles to Fine For', '1');
    await typeInto(user, currentModal(), 'Fine Amount', '20000');
    await submitModal(user);

    await waitFor(() => expect(adminService.createBottleFine).toHaveBeenCalled());
    expect(adminService.createBottleFine.mock.calls[0][0]).toEqual({
      address_id: expectedId,
      quantity: 1,
      fine_amount: 20000,
    });
  });
});
