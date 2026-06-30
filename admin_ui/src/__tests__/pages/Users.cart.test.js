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

vi.mock('../../components/AddressMapPicker', () => ({
  default: () => <div data-testid="address-map-picker" />,
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
    t: (key, fallback) => fallback || key,
  }),
}));

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

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => (
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </BrowserRouter>
  );
};

const baseUser = {
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
};

const setupBaseMocks = () => {
  vi.clearAllMocks();

  adminService.getUsers.mockResolvedValue({
    data: { items: [baseUser] },
    meta: { total: 1, page: 1, per_page: 20 },
  });
  adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [] } });
  adminService.getUserNotificationSettings.mockResolvedValue({
    data: { notification_settings: { telegram_connected: true, bot_active: true, updated_at: null } },
  });
  adminService.getUserCart.mockResolvedValue({
    data: { cart_items: [], item_count: 0, subtotal: 0, estimated_total: 0, updated_at: null },
  });
  adminService.updateUserStatus.mockResolvedValue({});

  staffService.getCustomerCodStatement = vi.fn().mockResolvedValue({ data: { data: null } });
  staffService.getCustomerPrepaymentHistory = vi.fn().mockResolvedValue({ data: { data: { events: [] } } });
};

const openUserDetails = async (user) => {
  render(<Users />, { wrapper: createWrapper() });
  await waitFor(() => {
    expect(adminService.getUsers).toHaveBeenCalled();
  });
  await screen.findByText('alice@example.com');
  await user.click(screen.getByText('ui.users.view_details'));
};

describe('Users page cart section', () => {
  beforeEach(() => {
    setupBaseMocks();
  });

  it('renders cart items with unit price, line total, and cart totals', async () => {
    const user = userEvent.setup();

    adminService.getUserCart.mockResolvedValue({
      data: {
        cart_items: [
          { id: 1, product_id: 5, quantity: 2, unit_price: 12000, total_price: 24000, product: { id: 5, name: 'Aqua 1.5L' } },
          { id: 2, product_id: 6, quantity: 1, unit_price: 8000, total_price: 8000, product: { id: 6, name: 'Aqua 0.5L' } },
        ],
        item_count: 3,
        subtotal: 32000,
        estimated_total: 32000,
        updated_at: '2026-06-29T10:00:00+00:00',
      },
    });

    await openUserDetails(user);

    await waitFor(() => {
      expect(adminService.getUserCart).toHaveBeenCalledWith(11);
    });

    expect(await screen.findByText('Aqua 1.5L')).toBeInTheDocument();
    expect(screen.getByText('Aqua 0.5L')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Unit price' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Line total' })).toBeInTheDocument();

    const row = screen.getByRole('row', { name: /Aqua 1\.5L/ });
    const cells = within(row).getAllByRole('cell');
    // [0]=product, [1]=quantity, [2]=unit_price, [3]=line_total
    expect(cells[2]).toHaveTextContent('12,000 UZS');
    expect(cells[3]).toHaveTextContent('24,000 UZS');

    expect(screen.getByText('Subtotal:')).toBeInTheDocument();
    expect(screen.getByText('Estimated total:')).toBeInTheDocument();
  });

  it('shows an empty state when the cart has no items', async () => {
    const user = userEvent.setup();

    adminService.getUserCart.mockResolvedValue({
      data: { cart_items: [], item_count: 0, subtotal: 0, estimated_total: 0, updated_at: null },
    });

    await openUserDetails(user);

    await waitFor(() => {
      expect(adminService.getUserCart).toHaveBeenCalledWith(11);
    });
    expect(await screen.findByText('Cart is empty.')).toBeInTheDocument();
  });

  it('does not fetch or render the cart section for non-customer users', async () => {
    const user = userEvent.setup();
    const managerUser = { ...baseUser, id: 22, email: 'mgr@example.com', role: 'manager' };
    adminService.getUsers.mockResolvedValue({
      data: { items: [managerUser] },
      meta: { total: 1, page: 1, per_page: 20 },
    });

    render(<Users />, { wrapper: createWrapper() });
    await waitFor(() => expect(adminService.getUsers).toHaveBeenCalled());
    await screen.findByText('mgr@example.com');
    await user.click(screen.getByText('ui.users.view_details'));

    // Modal opened (addresses fetched for every role)
    await waitFor(() => expect(adminService.getUserAddresses).toHaveBeenCalledWith(22));
    expect(adminService.getUserCart).not.toHaveBeenCalled();
    expect(screen.queryByText('Cart is empty.')).not.toBeInTheDocument();
  });
});
