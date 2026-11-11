import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';

import Users from '../../pages/Users';
import adminService from '../../services/adminService';
import { useAuthStore } from '../../stores/authStore';

vi.mock('../../services/adminService');
vi.mock('../../services/staffService');

// `message` mirrors GroupedAddresses.test.jsx: antd v5's static message mounts a
// real portal outside act() and leaks state between tests.
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

vi.mock('../../components/AddressMapPicker', () => ({
  default: () => <div data-testid="address-map-picker" />,
}));

// The case screen's read-only pin is a Leaflet map; jsdom has no canvas.
vi.mock('../../components/support/LocationBubble', () => ({
  default: ({ message: pin }) => <div data-testid="pin">{`${pin.latitude},${pin.longitude}`}</div>,
}));

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn() },
}));

vi.mock('../../hooks/useResponsive', () => ({
  default: () => ({
    isMobileDevice: false,
    isTabletDevice: false,
    isTouchDevice: false,
    getFontSize: (mobile, _tablet, desktop) => desktop || mobile,
  }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, vars) => (typeof fallback === 'string'
      ? fallback.replace(/{{\s*(\w+)\s*}}/g, (_, name) => String(vars?.[name]))
      : key),
  }),
}));

const BOTH_FLAGS = { can_review_shared_places: true, can_manage_users: true };

const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
};

const renderUsers = (entry) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={queryClient}>
        <Users />
        <LocationProbe />
      </QueryClientProvider>
    </MemoryRouter>
  );
};

const location = () => screen.getByTestId('location').textContent;

// The list row (review.py _list_row), every key the backend emits.
const listRow = {
  id: 12,
  status: 'awaiting_admin',
  driver_status: 'answered',
  created_at: '2026-11-11T09:30:00+00:00',
  order_number: 'TG_000501_26',
  customer_first_name: 'Bobur',
  customer_last_name: 'Yangi',
  address_title: 'Home',
  returned_qty: 3,
  // No candidates were offered (the detail's `candidates: []`): the note-only screen's "none".
  answer_kind: 'no_candidates',
};

const listPayload = {
  cases: [listRow],
  open_count: 1,
  page: 1,
  per_page: 20,
  total: 1,
  statuses: ['awaiting_driver', 'awaiting_admin', 'resolved', 'closed'],
};

const caseDetail = {
  id: 12,
  status: 'awaiting_admin',
  driver_status: 'answered',
  created_at: '2026-11-11T09:30:00+00:00',
  order: { id: 501, order_number: 'TG_000501_26' },
  delivery_id: 301,
  customer: { id: 11, first_name: 'Bobur', last_name: 'Yangi', phone: '+998790000011' },
  address: {
    id: 17,
    title: 'Home',
    full_address: 'Chilonzor 5, 12',
    latitude: 41.3101,
    longitude: 69.2801,
    apartment_number: null,
    place_group: null,
    place_empties_now: -1,
  },
  driver: { id: 5, first_name: 'Dilshod' },
  delivered_qty: 2,
  returned_qty: 3,
  place_balance_before: 0,
  answer: {
    kind: 'no_candidates',
    picked_index: null,
    note: "says he ordered before on his wife's phone",
    answered_at: '2026-11-11T09:31:00+00:00',
  },
  candidates: [],
  empty_reason: 'none_within_radius',
  facts: { linked_accounts: [], referred_by: null },
  tryouts: [],
  move_sources: [],
  choices: {
    kinds: ['shared_place', 'same_home', 'other_place', 'own_bottles', 'wrong_count', 'close'],
    move_allowed: true,
    grouping_done: false,
  },
  claim: null,
  resolution: null,
};

const drawerTitled = (title) => waitFor(() => {
  const titles = [...document.querySelectorAll('.ant-drawer-title')].filter((el) => el.textContent === title);
  expect(titles).toHaveLength(1);
  return titles[0].closest('.ant-drawer');
});

describe('Users "Driver reports" tab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ permissions: BOTH_FLAGS });
    adminService.getUsers.mockResolvedValue({
      data: { items: [] },
      meta: { total: 0, page: 1, per_page: 20 },
    });
    adminService.listSharedPlaceCases.mockResolvedValue(listPayload);
    adminService.getSharedPlaceCase.mockResolvedValue({ case: caseDetail });
  });

  afterEach(() => useAuthStore.setState({ permissions: {} }));

  it('?tab=driver-reports with the flag opens the tab and reads the open cases', async () => {
    renderUsers('/users?tab=driver-reports');

    expect(await screen.findByText('TG_000501_26')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Driver reports/ })).toHaveAttribute('aria-selected', 'true');
    expect(adminService.listSharedPlaceCases.mock.calls).toEqual([[{ status: 'open', page: 1, perPage: 20 }]]);
    expect(screen.getAllByRole('tab')).toHaveLength(4);
  });

  it('without the flag there is no tab, no fetch, and ?tab=driver-reports lands on List', async () => {
    useAuthStore.setState({ permissions: { can_manage_users: true } });
    renderUsers('/users?tab=driver-reports');

    await waitFor(() => expect(adminService.getUsers).toHaveBeenCalled());
    expect(screen.queryByRole('tab', { name: /Driver reports/ })).toBeNull();
    expect(screen.getAllByRole('tab')).toHaveLength(3);
    expect(screen.getByRole('tab', { name: /List/ })).toHaveAttribute('aria-selected', 'true');
    expect(adminService.listSharedPlaceCases).not.toHaveBeenCalled();
  });

  it('the tab is lazy: nothing is read until it is opened', async () => {
    renderUsers('/users');

    await waitFor(() => expect(adminService.getUsers).toHaveBeenCalled());
    expect(screen.getByRole('tab', { name: /List/ })).toHaveAttribute('aria-selected', 'true');
    expect(adminService.listSharedPlaceCases).not.toHaveBeenCalled();
  });

  it('clicking the tab sets ?tab=driver-reports', async () => {
    const user = userEvent.setup();
    renderUsers('/users');

    await user.click(await screen.findByRole('tab', { name: /Driver reports/ }));

    expect(location()).toBe('/users?tab=driver-reports');
    expect(await screen.findByText('TG_000501_26')).toBeInTheDocument();
  });

  it('?tab=driver-reports&case=12 opens that case\'s drawer (the Slack link)', async () => {
    renderUsers('/users?tab=driver-reports&case=12');

    const drawer = await drawerTitled('Case #12');
    await waitFor(() => expect(adminService.getSharedPlaceCase.mock.calls).toEqual([[12]]));
    expect(
      await within(drawer).findByText('got 2 full, handed back 3 empties; this address had 0 on record before')
    ).toBeInTheDocument();
  });

  it('a row opens its drawer and puts the case in the URL; closing removes it', async () => {
    const user = userEvent.setup();
    renderUsers('/users?tab=driver-reports');

    await user.click(await screen.findByText('TG_000501_26'));
    const drawer = await drawerTitled('Case #12');
    expect(location()).toBe('/users?tab=driver-reports&case=12');
    await waitFor(() => expect(adminService.getSharedPlaceCase.mock.calls).toEqual([[12]]));

    // Exact: the case's own "Close, no action" choice sits in the same drawer.
    await user.click(within(drawer).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(location()).toBe('/users?tab=driver-reports'));
  });

  it('a case with no nearby places reads "No nearby places" in the list and on the case, with the note', async () => {
    const user = userEvent.setup();
    renderUsers('/users?tab=driver-reports');

    const row = (await screen.findByText('TG_000501_26')).closest('tr');
    expect(within(row).getByText('No nearby places')).toBeInTheDocument();
    await user.click(row);
    const drawer = await drawerTitled('Case #12');
    expect(await within(drawer).findByText('No nearby places')).toBeInTheDocument();
    expect(within(drawer).getByText("says he ordered before on his wife's phone")).toBeInTheDocument();
    expect(screen.queryByText('None of these')).toBeNull();
  });

  it('a "none" over offered places still reads "None of these" in the list', async () => {
    adminService.listSharedPlaceCases.mockResolvedValue({
      ...listPayload,
      cases: [{ ...listRow, answer_kind: 'none' }],
    });
    renderUsers('/users?tab=driver-reports');

    const row = (await screen.findByText('TG_000501_26')).closest('tr');
    expect(within(row).getByText('None of these')).toBeInTheDocument();
    expect(within(row).queryByText('No nearby places')).toBeNull();
  });

  it('the status filter reads resolved cases', async () => {
    const user = userEvent.setup();
    renderUsers('/users?tab=driver-reports');
    await screen.findByText('TG_000501_26');

    // antd's button-style radio hides its <input>; the admin clicks the label.
    await user.click(screen.getByRole('radio', { name: 'Resolved' }).closest('label'));

    await waitFor(() => expect(adminService.listSharedPlaceCases).toHaveBeenLastCalledWith({
      status: 'resolved',
      page: 1,
      perPage: 20,
    }));
  });

  it('the case\'s customer opens the existing user modal', async () => {
    const user = userEvent.setup();
    // A full user row: the existing modal renders every one of these fields.
    adminService.getUserDetails.mockResolvedValue({
      data: {
        user: {
          id: 11,
          first_name: 'Bobur',
          last_name: 'Yangi',
          email: 'bobur@example.com',
          phone: '+998790000011',
          status: 'active',
          role: 'customer',
          user_type: 'individual',
          telegram_id: '998790000011',
          is_bot_active: true,
          created_at: '2026-11-01T10:00:00+00:00',
          last_login: '2026-11-10T10:00:00+00:00',
        },
      },
    });
    renderUsers('/users?tab=driver-reports&case=12');

    const drawer = await drawerTitled('Case #12');
    await user.click(await within(drawer).findByRole('button', { name: 'Bobur Yangi' }));

    await waitFor(() => expect(adminService.getUserDetails.mock.calls).toEqual([[11]]));
    // The page's own user modal, the one the Map tab opens too.
    expect(await screen.findByText('bobur@example.com')).toBeInTheDocument();
  });
});
