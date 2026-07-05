import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Subscriptions from '../../pages/Subscriptions';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getSubscriptions: vi.fn(),
    getSubscription: vi.fn(),
    createSubscription: vi.fn(),
    updateSubscription: vi.fn(),
    pauseSubscription: vi.fn(),
    resumeSubscription: vi.fn(),
    cancelSubscription: vi.fn(),
    processSubscriptionBilling: vi.fn(),
    addSubscriptionItem: vi.fn(),
    updateSubscriptionItem: vi.fn(),
    removeSubscriptionItem: vi.fn(),
    getUsers: vi.fn(),
    getUserAddresses: vi.fn(),
    getProducts: vi.fn(),
    getTimeSlots: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

describe('Subscriptions page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getSubscriptions.mockResolvedValue({
      items: [{
        id: 1, subscription_number: 'SUB-1', user_name: 'Test User', user_email: 't@e.com',
        status: 'active', billing_cycle: 'monthly', billing_amount: 30000,
        next_billing_date: '2026-08-01T09:00:00Z', items_count: 2,
      }],
      total: 1,
    });
    adminService.getUsers.mockResolvedValue({ items: [], total: 0 });
    adminService.getProducts.mockResolvedValue({ data: { items: [] } });
    adminService.getTimeSlots.mockResolvedValue({ data: { items: [] } });
    adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [{ id: 3, full_address: 'Amir Temur 1' }] } });
    adminService.getSubscription.mockResolvedValue({
      id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' },
      name: 'Existing Sub', description: 'd', billing_cycle: 'monthly', delivery_frequency: 'weekly',
      payment_method: 'cash', delivery_address_id: 3, auto_payment: true, auto_renew: true,
      discount_percentage: 0, status: 'active', items: [],
    });
    adminService.updateSubscription.mockResolvedValue({ id: 1 });
  });

  it('renders a subscription row from the list endpoint', async () => {
    render(<Subscriptions />, { wrapper: createWrapper() });
    expect(await screen.findByText('SUB-1')).toBeInTheDocument();
    expect(screen.getByText('Test User')).toBeInTheDocument();
  });

  it('opens the create modal with a Name field', async () => {
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');
    fireEvent.click(screen.getByRole('button', { name: /create subscription/i }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Name')).toBeInTheDocument();
  });

  it('prefills the edit modal and submits an update payload', async () => {
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');

    // Click the row's edit (pencil) action.
    const editIcon = document.querySelector('.anticon-edit');
    fireEvent.click(editIcon.closest('button'));

    // Prefilled name appears.
    expect(await screen.findByDisplayValue('Existing Sub')).toBeInTheDocument();

    // Submit the update.
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));

    await waitFor(() => {
      expect(adminService.updateSubscription).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ name: 'Existing Sub' }),
      );
    });
  });

  it('saves an edited item quantity from the drawer with the typed value', async () => {
    adminService.getSubscription.mockResolvedValue({
      id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' },
      name: 'Existing Sub', billing_cycle: 'monthly', delivery_frequency: 'weekly',
      payment_method: 'cash', delivery_address_id: 3, auto_payment: true, auto_renew: true,
      discount_percentage: 0, status: 'active',
      items: [{ id: 55, product_id: 2, product_name: 'Water', quantity: 2, unit_price: 15000 }],
    });
    adminService.updateSubscriptionItem.mockResolvedValue({ data: {} });
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');
    fireEvent.click(document.querySelector('.anticon-eye').closest('button'));
    expect(await screen.findByText('Water')).toBeInTheDocument();
    const qtyInput = document.querySelectorAll('.ant-drawer .ant-input-number-input')[0];
    fireEvent.change(qtyInput, { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => {
      expect(adminService.updateSubscriptionItem).toHaveBeenCalledWith(1, 55, { quantity: 7 });
    });
  });
});
