import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';

import PlaceGroupPanel from '../../components/PlaceGroupPanel';
import GroupedAddressesPanel from '../../components/GroupedAddressesPanel';
import adminService from '../../services/adminService';
import { useAuthStore } from '../../stores/authStore';

vi.mock('../../services/adminService');

// The stub interpolates `{{x}}` the way i18next does, so the distance line is
// asserted as the customer-visible text, not as the template.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, options) =>
      String(fallback || key).replace(/\{\{(\w+)\}\}/g, (_, name) => options?.[name] ?? ''),
  }),
}));

vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn() },
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

// A member as `CustomerLinkService._member` publishes it: `display_label` is the backend's one display rule.
const member = (id, overrides = {}) => ({
  address_id: id,
  user_id: 11,
  first_name: 'Alice',
  last_name: 'Tester',
  phone: '+998900000001',
  title: null,
  full_address: `Street ${id}`,
  display_label: `Street ${id}`,
  ...overrides,
});

// The second member has no title and no written line: the backend names it by its street line, which the panels
// print as published (a `title || full_address` re-derivation prints nothing for it).
const duplicateRow = {
  kind: 'same_customer',
  address_ids: [21, 22],
  members: [
    member(21, { title: 'Home', display_label: 'Home' }),
    member(22, { title: null, full_address: '', display_label: 'Navoi 5' }),
  ],
  distance_m: 14,
  distinct_customer_count: 1,
  signal_fingerprint: 'fp-dup',
};

const sharedRow = {
  kind: 'shared_place',
  address_ids: [31, 32],
  members: [
    member(31, { user_id: 41, first_name: 'Carl', last_name: 'One', full_address: 'Office st 9', display_label: 'Office st 9' }),
    member(32, { user_id: 42, first_name: 'Dina', last_name: 'Two', full_address: 'Office st 9', display_label: 'Office st 9' }),
  ],
  distinct_customer_count: 2,
  signal_fingerprint: 'fp-shared',
};

const legacyRow = { ...sharedRow, kind: undefined };
delete legacyRow.kind;

beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.setState({ permissions: { can_manage_users: true } });
  adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [] } });
  adminService.getUsers.mockResolvedValue({ data: { items: [] } });
  adminService.listPlaceGroups.mockResolvedValue({
    data: { items: [], pagination: { page: 1, per_page: 20, total: 0, pages: 0 } },
  });
  adminService.getGlobalPlaceGroupSuggestions.mockResolvedValue({ data: [duplicateRow, sharedRow] });
  adminService.getPlaceGroupSuggestions.mockResolvedValue({ data: { suggestions: [duplicateRow, legacyRow] } });
  adminService.getPlaceGroupMergePreview.mockResolvedValue({ data: {} });
});

afterEach(() => useAuthStore.setState({ permissions: {} }));

describe('same_customer suggestion rows', () => {
  it('a same_customer row shows the kind, both display labels and “~14 m apart”, and no customer count', async () => {
    wrap(<GroupedAddressesPanel />);

    const row = (await screen.findByText('Possible duplicate address')).closest('tr');
    expect(within(row).getByText('Home · Navoi 5')).toBeInTheDocument();
    expect(within(row).getByText('~14 m apart')).toBeInTheDocument();
    // Both members are one customer, so neither the names nor the count appear.
    expect(within(row).queryByText(/Alice/)).toBeNull();
    expect(within(row).queryByText('1')).toBeNull();
  });

  it('the per-customer panel renders the same row the same way', async () => {
    const user = userEvent.setup();
    wrap(<PlaceGroupPanel userId={USER.id} user={USER} />);

    await user.click(await screen.findByRole('button', { name: /Find possible same-place matches/ }));

    expect(await screen.findByText('Possible duplicate address')).toBeInTheDocument();
    expect(screen.getByText('Home · Navoi 5')).toBeInTheDocument();
    expect(screen.getByText('~14 m apart')).toBeInTheDocument();
  });

  it('a row without kind renders exactly as before', async () => {
    const user = userEvent.setup();
    wrap(<PlaceGroupPanel userId={USER.id} user={USER} />);
    await user.click(await screen.findByRole('button', { name: /Find possible same-place matches/ }));

    expect(await screen.findByText('Carl One · Dina Two')).toBeInTheDocument();
    expect(screen.getByText(/Office st 9 · customers: 2/)).toBeInTheDocument();
    // Exactly one of the two rows is a duplicate row.
    expect(screen.getAllByText('Possible duplicate address')).toHaveLength(1);
  });

  it('Group as same place opens the confirm modal with the pair', async () => {
    const user = userEvent.setup();
    wrap(<GroupedAddressesPanel />);

    const row = (await screen.findByText('Possible duplicate address')).closest('tr');
    await user.click(within(row).getByRole('button', { name: /Group as same place/ }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/Street 21|Home/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Navoi 5/)).toBeInTheDocument();
  });

  it('the dialog prints each member’s display_label, never a title or street line of its own', async () => {
    const user = userEvent.setup();
    // display_label is the backend's answer; title and full_address here say something else, so a
    // re-derivation would print "Old title" / "Old street" instead.
    const row = {
      ...duplicateRow,
      members: [
        member(21, { title: 'Old title', full_address: 'Old street 1', display_label: 'Home' }),
        member(22, { title: null, full_address: '', display_label: 'Navoi 5' }),
      ],
    };
    adminService.getGlobalPlaceGroupSuggestions.mockResolvedValue({ data: [row] });
    wrap(<GroupedAddressesPanel />);

    const tableRow = (await screen.findByText('Possible duplicate address')).closest('tr');
    await user.click(within(tableRow).getByRole('button', { name: /Group as same place/ }));

    const dialog = await screen.findByRole('dialog');
    expect(
      [...dialog.querySelectorAll('.ant-select-selection-item-content')].map((el) => el.textContent)
    ).toEqual(['Alice Tester · +998900000001 — Home', 'Alice Tester · +998900000001 — Navoi 5']);
  });
});

describe('the global kind filter', () => {
  it('passes kind=same_customer and kind=shared_place, and “All” passes none', async () => {
    const user = userEvent.setup();
    wrap(<GroupedAddressesPanel />);

    await waitFor(() => expect(adminService.getGlobalPlaceGroupSuggestions).toHaveBeenCalledTimes(1));
    expect(adminService.getGlobalPlaceGroupSuggestions).toHaveBeenLastCalledWith({ limit: 20 });

    await user.click(screen.getByText('Possible duplicates'));
    await waitFor(() =>
      expect(adminService.getGlobalPlaceGroupSuggestions).toHaveBeenLastCalledWith({ limit: 20, kind: 'same_customer' })
    );

    await user.click(screen.getByText('Shared places'));
    await waitFor(() =>
      expect(adminService.getGlobalPlaceGroupSuggestions).toHaveBeenLastCalledWith({ limit: 20, kind: 'shared_place' })
    );

    await user.click(screen.getByText('All'));
    await waitFor(() =>
      expect(adminService.getGlobalPlaceGroupSuggestions).toHaveBeenLastCalledWith({ limit: 20 })
    );
  });
});
