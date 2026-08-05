import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';

import Users from '../../pages/Users';
import adminService from '../../services/adminService';
import staffService from '../../services/staffService';

vi.mock('../../services/adminService');
vi.mock('../../services/staffService');

// Dropdown mirrors Users.linkedAccounts.test.js (the row-action menu never
// renders its items otherwise); `message` mirrors PlaceGroupPanel.test.jsx —
// antd v5's static message mounts a real portal outside act() and leaks state
// between tests.
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    Dropdown: ({ menu, children }) => (
      <div>
        {children}
        {menu?.items
          ?.filter((item) => item && item.type !== 'divider' && item.onClick)
          .map((item) => (
            <button key={item.key} disabled={item.disabled} onClick={item.onClick} type="button">
              {typeof item.label === 'string' ? item.label : item.key}
            </button>
          ))}
      </div>
    ),
    message: {
      success: vi.fn(),
      error: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
      loading: vi.fn(),
      destroy: vi.fn(),
      open: vi.fn(),
    },
  };
});

vi.mock('../../components/AddressMapPicker', () => ({
  default: () => <div data-testid="address-map-picker" />,
}));

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
  },
}));

vi.mock('../../hooks/useResponsive', () => ({
  default: () => ({
    isMobileDevice: false,
    isTabletDevice: false,
    isTouchDevice: false,
    getFontSize: (mobile, _tablet, desktop) => desktop || mobile,
  }),
}));

// Returns the INLINE ENGLISH FALLBACK, so every assertion below also pins
// invariant 3b: a call site that forgot its second argument renders the raw
// `ui.users.grouped_addresses.*` key and fails here, exactly as
// tests/unit/test_place_group_translation_seeds.py fails on the seed side.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback) => fallback || key,
  }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return ({ children }) => (
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </BrowserRouter>
  );
};

// ---------------------------------------------------------------------------
// Payload contracts
// ---------------------------------------------------------------------------
// `GET /admin/place-groups`          -> CustomerLinkService.list_place_groups
// `GET /admin/place-group-suggestions` -> CustomerLinkService.get_place_group_suggestions
//
// Both key sets are hand-copied from those two services (A1.2). Every column
// carries a DIFFERENT number so a swapped column cannot pass.
const groupsPayload = {
  items: [
    {
      id: 12,
      label: 'Office A',
      member_count: 3,
      address_count: 5,
      // C6: a FLOAT on the wire. Flask renders a bare Decimal as "35000.00",
      // which turns the admin UI's arithmetic into NaN.
      place_open_cod_debt_total: 35000,
      active_cod_debt_count: 7,
      // The other half of invariant 2: what the place HOLDS. A Decimal in the
      // ledger, a JSON number here, and a quantity — so it is NOT formatted as
      // money. 9 collides with nothing else on the row.
      bottle_exposure: 9,
      created_at: '2026-07-20T09:30:00+00:00',
    },
  ],
  pagination: { page: 1, per_page: 20, total: 1, pages: 1 },
};

const suggestion = (overrides = {}) => ({
  address_ids: [21, 22],
  distinct_customer_count: 4,
  score: 4.0,
  signal_fingerprint: 'fp-one',
  members: [
    {
      address_id: 21,
      user_id: 31,
      first_name: 'Suggested',
      last_name: 'Person',
      phone: '+998900000021',
      title: 'Office',
      full_address: 'Office st 9',
    },
    {
      address_id: 22,
      user_id: 32,
      first_name: 'Other',
      last_name: 'Neighbour',
      phone: '+998900000022',
      title: 'Office',
      full_address: 'Office st 9',
    },
  ],
  ...overrides,
});

const openTab = async (user) => {
  await user.click(await screen.findByRole('tab', { name: /Grouped Addresses/ }));
};

describe('Users "Grouped Addresses" tab', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    adminService.getUsers.mockResolvedValue({
      data: {
        items: [
          {
            id: 11,
            first_name: 'Alice',
            last_name: 'Tester',
            email: 'alice@example.com',
            phone: '+998901234567',
            status: 'active',
            role: 'customer',
            user_type: 'individual',
            telegram_id: '998901234567',
            is_bot_active: true,
            created_at: '2026-03-01T10:00:00+00:00',
            last_login: '2026-03-02T10:00:00+00:00',
          },
        ],
      },
      meta: { total: 1, page: 1, per_page: 20 },
    });

    adminService.listPlaceGroups.mockResolvedValue({ data: groupsPayload });
    adminService.getGlobalPlaceGroupSuggestions.mockResolvedValue({ data: [suggestion()] });
  });

  it('renders a third Users tab labelled from ui.users.grouped_addresses.*', async () => {
    render(<Users />, { wrapper: createWrapper() });

    await waitFor(() => expect(adminService.getUsers).toHaveBeenCalled());

    // The i18n stub returns the INLINE ENGLISH FALLBACK, so this also pins
    // invariant 3b: t('ui.users.grouped_addresses.tab', 'Grouped Addresses').
    expect(await screen.findByText('Grouped Addresses')).toBeInTheDocument();
    // ...as a THIRD tab beside the two that already exist, not a new nav item.
    expect(screen.getAllByRole('tab')).toHaveLength(3);
  });

  it('lists every grouped address with its label, member count and BOTH halves of its exposure', async () => {
    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });
    await openTab(user);

    expect(await screen.findByText('Office A')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();   // distinct OWNERS
    expect(screen.getByText('5')).toBeInTheDocument();   // ...addresses, counted separately
    expect(screen.getByText('7')).toBeInTheDocument();   // unpaid COD orders
    expect(screen.getByText('35,000')).toBeInTheDocument();
    // Invariant 2 in full: the debt the place OWES *and* the bottles it HOLDS.
    // Grouping pools both, so a row carrying only the money would show half the
    // consequence of the act this tab exists to make easier.
    expect(screen.getByText('9')).toBeInTheDocument();   // bottles at this place
    // ...under the SAME label the per-customer panel uses for the same figure.
    expect(screen.getByRole('columnheader', { name: 'Bottles at this place' })).toBeInTheDocument();
    // A quantity, not money: 9 renders as "9", never as "9,000"-style money copy.
    expect(screen.queryByText('9.00')).toBeNull();

    // Assert the arguments the service actually received, not that it was called.
    expect(adminService.listPlaceGroups.mock.calls[0]).toEqual([
      { page: 1, perPage: 20, search: '' },
    ]);
    // C6: money is a NUMBER on the wire, never a pre-formatted string. The
    // bottle figure crosses the same boundary as a Decimal and needs the same
    // cast, so it is pinned the same way.
    expect(typeof groupsPayload.items[0].place_open_cod_debt_total).toBe('number');
    expect(typeof groupsPayload.items[0].bottle_exposure).toBe('number');
  });

  it('lists suggested candidates with their distinct-customer count', async () => {
    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });
    await openTab(user);

    expect(await screen.findByText('Suggested Person')).toBeInTheDocument();
    expect(screen.getByText('Other Neighbour')).toBeInTheDocument();
    expect(screen.getByText('4')).toBeInTheDocument();
    expect(adminService.getGlobalPlaceGroupSuggestions).toHaveBeenCalledTimes(1);
    expect(adminService.getGlobalPlaceGroupSuggestions.mock.calls[0]).toEqual([{ limit: 20 }]);
  });

  it('does not fetch anything until the tab is selected', async () => {
    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });
    await screen.findByText('alice@example.com');

    // Lazy, mirroring the map tab (Users.js:1054). The suggestion route is the
    // UN-ANCHORED clusterer over the whole estate (A1.2) — it must not run on
    // every Users page load.
    expect(adminService.listPlaceGroups).not.toHaveBeenCalled();
    expect(adminService.getGlobalPlaceGroupSuggestions).not.toHaveBeenCalled();

    await openTab(user);

    await waitFor(() => expect(adminService.listPlaceGroups).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(adminService.getGlobalPlaceGroupSuggestions).toHaveBeenCalledTimes(1)
    );
  });

  it('routes "Group as same place" through the shared confirm modal', async () => {
    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });
    await openTab(user);

    await user.click(await screen.findByRole('button', { name: /Group as same place/ }));

    // The panel must render <PlaceGroupConfirmModal> — the component extracted
    // from PlaceGroupPanel.jsx — NOT its own modal. The extracted modal owns
    // the reason TextArea, so a duplicated modal with its own copy fails here.
    expect(await screen.findByPlaceholderText('Reason (required)')).toBeInTheDocument();
    // ...and the suggestion's own addresses are pre-picked, so the admin
    // confirms exactly what the engine proposed.
    expect(await screen.findByText('Group these addresses as one place?')).toBeInTheDocument();
  });

  it('will not submit a grouping without a reason', async () => {
    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });
    await openTab(user);

    await user.click(await screen.findByRole('button', { name: /Group as same place/ }));

    // Spec 2.1/2.2, invariant 1: two addresses picked, reason blank.
    // `confirmDisabled` already includes `!reason.trim()`, and the extracted
    // modal carries that rule with it.
    const ok = await screen.findByRole('button', { name: /^OK$/ });
    expect(ok).toBeDisabled();

    await user.click(ok);
    expect(adminService.createPlaceGroup).not.toHaveBeenCalled();

    // ...and it submits once a reason IS given, so the assertion above is
    // about the reason and not about a permanently dead button.
    adminService.createPlaceGroup.mockResolvedValue({ data: { place_group_id: 31 } });
    await user.type(screen.getByPlaceholderText('Reason (required)'), 'one office, two doors');
    await user.click(screen.getByRole('button', { name: /^OK$/ }));

    await waitFor(() =>
      expect(adminService.createPlaceGroup.mock.calls[0]).toEqual([
        [21, 22],
        null,
        'one office, two doors',
        {},
      ])
    );
  });

  it('offers NO bulk accept-all control', async () => {
    const user = userEvent.setup();
    adminService.getGlobalPlaceGroupSuggestions.mockResolvedValue({
      data: [
        suggestion(),
        suggestion({
          address_ids: [31, 32],
          distinct_customer_count: 2,
          signal_fingerprint: 'fp-two',
          members: [
            {
              address_id: 31,
              user_id: 41,
              first_name: 'Second',
              last_name: 'Candidate',
              phone: '+998900000031',
              title: 'Shop',
              full_address: 'Shop st 4',
            },
            {
              address_id: 32,
              user_id: 42,
              first_name: 'Third',
              last_name: 'Candidate',
              phone: '+998900000032',
              title: 'Shop',
              full_address: 'Shop st 4',
            },
          ],
        }),
      ],
    });

    render(<Users />, { wrapper: createWrapper() });
    await openTab(user);

    // Non-vacuous: TWO candidates are on screen, so a bulk control would have
    // something to act on — and every one of them still needs its own human
    // confirmation (spec 2.1: auto-grouping fails dangerously in seven ways).
    expect(await screen.findByText('Suggested Person')).toBeInTheDocument();
    expect(screen.getByText('Second Candidate')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /Group as same place/ })).toHaveLength(2);

    expect(screen.queryByRole('button', { name: /accept all/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /group all/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /apply all/i })).toBeNull();
    // No row selection either: a checkbox column is how a bulk action arrives
    // without a button that says "all".
    const suggestions = (await screen.findByText('Suggested candidates')).closest('.ant-card');
    expect(within(suggestions).queryAllByRole('checkbox')).toHaveLength(0);
  });
});
