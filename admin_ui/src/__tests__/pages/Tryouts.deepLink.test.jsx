import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Tryouts from '../../pages/Tryouts';
import adminService from '../../services/adminService';
import tryoutService from '../../services/tryoutService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getProducts: vi.fn(),
    getDeliveryPersonnel: vi.fn(),
  },
}));

vi.mock('../../services/tryoutService', () => ({
  __esModule: true,
  default: {
    getTryouts: vi.fn(),
    getTryout: vi.fn(),
    exportTryouts: vi.fn(),
    convertTryout: vi.fn(),
  },
}));

vi.mock('../../components/AddressMapPicker', () => ({
  default: () => <div data-testid="address-map-picker" />,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

// No Router on purpose: Tryouts.js reads the link from window.location, so the
// page renders the same way its two existing test files render it.
const renderTryouts = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <Tryouts />
    </QueryClientProvider>
  );
};

const tryoutDetail = {
  id: 7,
  tryout_number: 'TRY_000007_26',
  status: 'active',
  outcome: 'pending',
  pickup_state: 'overdue',
  return_due_at: '2026-11-01T10:00:00Z',
  outstanding_bottles_total: 2,
  can_convert: false,
  trial_contact: { full_name: 'Trial Customer', phone: '+998790000031' },
  converted_user: { id: 11, full_name: 'Bobur Yangi', phone: '+998790000011' },
  address_snapshot: { full_address: 'Chilonzor 5, 12', latitude: 41.3101, longitude: 69.2801 },
  notes: null,
  items: [],
  tasks: [],
  ledger: [],
};

const drawerTitled = (title) => waitFor(() => {
  const titles = [...document.querySelectorAll('.ant-drawer-title')].filter((el) => el.textContent === title);
  expect(titles).toHaveLength(1);
  return titles[0].closest('.ant-drawer');
});

describe('Tryouts ?tryout_id= deep link', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getProducts.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getDeliveryPersonnel.mockResolvedValue({ data: { items: [], total: 0 } });
    tryoutService.getTryouts.mockResolvedValue({ items: [], total: 0, summary: {} });
    tryoutService.getTryout.mockResolvedValue(tryoutDetail);
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('opens the linked try-out\'s drawer on mount', async () => {
    window.history.replaceState(null, '', '/tryouts?tryout_id=7');
    renderTryouts();

    const drawer = await drawerTitled('TRY_000007_26');
    expect(tryoutService.getTryout.mock.calls).toEqual([[7]]);
    // The drawer is the one that carries the existing Adjust Bottles tool.
    expect(within(drawer).getByRole('button', { name: /Adjust Bottles/ })).toBeInTheDocument();
  });

  it('closing the drawer strips the param, keeping the rest of the URL', async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, '', '/tryouts?status=active&tryout_id=7#top');
    renderTryouts();

    const drawer = await drawerTitled('TRY_000007_26');
    await user.click(within(drawer).getByRole('button', { name: 'Close' }));

    await waitFor(() => expect(window.location.search).toBe('?status=active'));
    expect(window.location.pathname).toBe('/tryouts');
    expect(window.location.hash).toBe('#top');
  });

  it('without the param no try-out is read', async () => {
    window.history.replaceState(null, '', '/tryouts');
    renderTryouts();

    expect(await screen.findByText('Try-outs')).toBeInTheDocument();
    expect(tryoutService.getTryout).not.toHaveBeenCalled();
  });

  it('a malformed id is ignored', async () => {
    window.history.replaceState(null, '', '/tryouts?tryout_id=abc');
    renderTryouts();

    expect(await screen.findByText('Try-outs')).toBeInTheDocument();
    expect(tryoutService.getTryout).not.toHaveBeenCalled();
  });
});
