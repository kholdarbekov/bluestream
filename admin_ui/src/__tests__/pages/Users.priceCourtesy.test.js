import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';

import Users from '../../pages/Users';
import adminService from '../../services/adminService';
import staffService from '../../services/staffService';

vi.mock('../../services/adminService');
vi.mock('../../services/staffService');

// The row's action menu renders its items inline, so "view details" is a plain button.
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
  };
});

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
    t: (key, fallback, opts) => String(typeof fallback === 'string' ? fallback : key)
      .replace(/\{\{(\w+)\}\}/g, (_, name) => String(opts?.[name] ?? '')),
  }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => (
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </BrowserRouter>
  );
};

const userRow = (role) => ({
  id: 11,
  first_name: 'Alice',
  last_name: 'Tester',
  email: 'alice@example.com',
  phone: '+998901234567',
  status: 'active',
  role,
  user_type: 'individual',
  telegram_id: '998901234567',
  is_bot_active: true,
  created_at: '2026-03-01T10:00:00+00:00',
  last_login: '2026-03-02T10:00:00+00:00',
});

const seed = (role) => {
  adminService.getUsers.mockResolvedValue({ data: { items: [userRow(role)] }, meta: { total: 1, page: 1, per_page: 20 } });
  adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [] } });
  adminService.getUserNotificationSettings.mockResolvedValue({
    data: { notification_settings: { delivery_telegram_status_updates_enabled: true, telegram_connected: true, bot_active: true } },
  });
  adminService.getUserCart.mockResolvedValue({ data: null });
  adminService.getLinkedAccounts.mockResolvedValue({ data: { canonical_customer_id: 5, primary_user_id: 11, members: [] } });
  adminService.getPlaceGroupSuggestions.mockResolvedValue({ data: { suggestions: [] } });
  adminService.getCustomerPriceCourtesy.mockResolvedValue({
    courtesy_status: {
      state: 'available',
      expires_on: '2027-02-14',
      caps: [{ product_id: 3, product_name: 'Water 18.9 L', cap: 2 }],
      used_order_id: null,
      used_order_number: null,
    },
    allowed_actions: { grant: { allowed: false, block_reason: null } },
  });
  staffService.getCustomerCodStatement.mockResolvedValue({
    data: { data: { active_cod_debt_count: 0, total_outstanding_amount: 0, cod_restricted: false, items: [] } },
  });
  staffService.getCustomerPrepaymentHistory.mockResolvedValue({ data: { data: null } });
};

describe('Users price courtesy panel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("wires the panel into a customer's details", async () => {
    seed('customer');
    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });
    await screen.findByText('alice@example.com');
    await user.click(screen.getByText('ui.users.view_details'));

    await waitFor(() => expect(adminService.getCustomerPriceCourtesy).toHaveBeenCalledWith(11));
    expect(await screen.findByText(/Old price kept for this customer until Feb 14, 2027/)).toBeInTheDocument();
  });

  it('does not ask for a staff account', async () => {
    seed('operator');
    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });
    await screen.findByText('alice@example.com');
    await user.click(screen.getByText('ui.users.view_details'));

    // The details modal is open and its sibling panel has loaded.
    await waitFor(() => expect(adminService.getLinkedAccounts).toHaveBeenCalledWith(11));
    expect(adminService.getCustomerPriceCourtesy).not.toHaveBeenCalled();
    expect(screen.queryByText(/Old price kept for this customer/)).not.toBeInTheDocument();
  });

  it('never carries a typed reason over to another customer', async () => {
    seed('customer');
    adminService.getUsers.mockResolvedValue({
      data: { items: [userRow('customer'), { ...userRow('customer'), id: 12, first_name: 'Bob', email: 'bob@example.com' }] },
      meta: { total: 2, page: 1, per_page: 20 },
    });
    adminService.getCustomerPriceCourtesy.mockResolvedValue({
      courtesy_status: { state: 'none', expires_on: null, caps: [] },
      allowed_actions: { grant: { allowed: true, block_reason: null } },
    });
    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });
    await screen.findByText('alice@example.com');
    await user.click(screen.getAllByText('ui.users.view_details')[0]);
    await user.click(await screen.findByRole('button', { name: /Give the old-price order/ }));
    await user.type(await screen.findByPlaceholderText('Why this customer gets it'), 'Alice asked');
    // Close the grant dialog and the details, then open the other customer's details without a page reload.
    await user.click(screen.getByRole('button', { name: /Cancel/ }));
    await user.keyboard('{Escape}');
    await user.click(screen.getAllByText('ui.users.view_details')[1]);
    await waitFor(() => expect(adminService.getCustomerPriceCourtesy).toHaveBeenCalledWith(12));
    await user.click(await screen.findByRole('button', { name: /Give the old-price order/ }));

    expect(await screen.findByPlaceholderText('Why this customer gets it')).toHaveValue('');
  });
});
