import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';

import LinkedAccountsPanel from '../../components/LinkedAccountsPanel';
import PlaceGroupPanel from '../../components/PlaceGroupPanel';
import GroupedAddressesPanel from '../../components/GroupedAddressesPanel';
import adminService from '../../services/adminService';
import { useAuthStore } from '../../stores/authStore';

vi.mock('../../services/adminService');

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, fallback) => fallback || key }),
}));

vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
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

const wrap = (ui) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>
    </BrowserRouter>
  );
};

const USER = { id: 11, first_name: 'Alice', last_name: 'Tester', phone: '+998900000001' };

const suggestionMembers = [
  { address_id: 21, user_id: 31, first_name: 'A', last_name: 'B', phone: '+99879000001', title: 'T', full_address: 'X st' },
  { address_id: 22, user_id: 32, first_name: 'C', last_name: 'D', phone: '+99879000002', title: 'T', full_address: 'X st' },
];

const groupDetail = {
  notes: null,
  place_group_id: 7,
  label: 'Acme office',
  place_balance: 5,
  members: [
    {
      address_id: 1,
      address_title: 'Office',
      full_address: 'Office st 1',
      suggested_bottles_leaving: 2,
      owner: { id: 11, first_name: 'Alice', last_name: 'Tester', phone: '+998900000001' },
    },
  ],
  cod: { total_outstanding_amount: 0, active_cod_debt_count: 0, items: [] },
  removal_dissolves_group: false,
  events: [],
};

const setPermissions = (canManage) =>
  useAuthStore.setState({ permissions: canManage ? { can_manage_users: true } : {} });

beforeEach(() => {
  vi.clearAllMocks();
  adminService.getLinkedAccounts.mockResolvedValue({
    data: {
      canonical_customer_id: 5,
      primary_user_id: 11,
      members: [
        { id: 11, first_name: 'Alice', last_name: 'Tester', phone: '+998900000001' },
        { id: 12, first_name: 'Bob', last_name: 'Second', phone: '+998900000002' },
      ],
    },
  });
  adminService.getLinkSuggestions.mockResolvedValue({
    data: {
      suggestions: [
        { user_id: 21, first_name: 'Carl', last_name: 'Candidate', phone: '+99879000003', min_distance_km: 0.05, shared_geo_customer_count: 1, score: 0.9 },
      ],
    },
  });
  adminService.getUsers.mockResolvedValue({ data: { items: [] } });
  adminService.getUserAddresses.mockResolvedValue({
    data: { addresses: [{ id: 1, title: 'Office', full_address: 'Office st 1', address_group_id: 7 }] },
  });
  adminService.getPlaceGroup.mockResolvedValue({ data: groupDetail });
  adminService.getPlaceGroupSuggestions.mockResolvedValue({
    data: { suggestions: [{ address_ids: [21, 22], distinct_customer_count: 2, signal_fingerprint: 'f', members: suggestionMembers }] },
  });
  adminService.listPlaceGroups.mockResolvedValue({
    data: {
      items: [{ id: 12, label: 'Office A', member_count: 2, address_count: 2, place_open_cod_debt_total: 0, active_cod_debt_count: 0, bottle_exposure: 1, created_at: '2026-07-20T09:30:00+00:00' }],
      pagination: { page: 1, per_page: 20, total: 1, pages: 1 },
    },
  });
  adminService.getGlobalPlaceGroupSuggestions.mockResolvedValue({
    data: [{ address_ids: [21, 22], distinct_customer_count: 2, signal_fingerprint: 'f', members: suggestionMembers }],
  });
});

afterEach(() => setPermissions(false));

const names = {
  link: /^Link$/,
  unlink: /^Unlink$/,
  notSamePerson: /Not the same person/,
  newGroup: /New place group/,
  groupAs: /Group as same place/,
  notSamePlace: /Not the same place/,
  rename: /^Rename$/,
  add: /Add address/,
  remove: /^Remove$/,
  edit: /^Edit$/,
};

describe.each([
  [false, 0],
  [true, 1],
])('place-group write gating (can_manage_users=%s)', (granted) => {
  beforeEach(() => setPermissions(granted));

  const expectButtons = (keys) => {
    keys.forEach((key) => {
      const found = screen.queryAllByRole('button', { name: names[key] });
      if (granted) expect(found.length, key).toBeGreaterThan(0);
      else expect(found, key).toHaveLength(0);
    });
  };

  it('LinkedAccountsPanel: Link, Unlink, Not the same person', async () => {
    wrap(<LinkedAccountsPanel user={USER} />);
    expect(await screen.findByText(/Carl Candidate/)).toBeInTheDocument();
    expect(screen.getByText(/Bob Second/)).toBeInTheDocument();
    expectButtons(['link', 'unlink', 'notSamePerson']);
  });

  it('PlaceGroupPanel (+ card): New group, Group as, Not the same place, Rename, Add, Remove', async () => {
    const user = userEvent.setup();
    wrap(<PlaceGroupPanel user={USER} />);
    expect(await screen.findByText('Acme office')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Find possible same-place matches/ }));
    expect(await screen.findByText(/A B · C D/)).toBeInTheDocument();
    expectButtons(['newGroup', 'groupAs', 'notSamePlace', 'rename', 'add', 'remove']);
    // Reads still render.
    expect(screen.getByText(/Alice Tester/)).toBeInTheDocument();
  });

  it('GroupedAddressesPanel: Edit, Group as, Not the same place', async () => {
    wrap(<GroupedAddressesPanel />);
    expect(await screen.findByText('Office A')).toBeInTheDocument();
    await screen.findAllByText(/A B/);
    expectButtons(['edit', 'groupAs', 'notSamePlace']);
  });

  it('GroupedAddressesPanel: the row always opens the place card; a reader gets View and no write button', async () => {
    const user = userEvent.setup();
    wrap(<GroupedAddressesPanel />);
    await screen.findByText('Office A');

    const open = granted ? names.edit : /^View$/;
    expect(screen.queryAllByRole('button', { name: granted ? /^View$/ : names.edit })).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: open }));

    expect(await screen.findByText(granted ? 'Edit place group' : 'Place group')).toBeInTheDocument();
    // The card's reads render for both; its write buttons only for a writer.
    expect(await screen.findByText(/Alice Tester/)).toBeInTheDocument();
    await waitFor(() => expect(adminService.getPlaceGroup).toHaveBeenCalledWith(12));
    expectButtons(['rename', 'add', 'remove']);
  });
});
