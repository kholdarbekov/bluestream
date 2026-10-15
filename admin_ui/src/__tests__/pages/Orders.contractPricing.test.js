/**
 * Create Order lists each product at the price of the payment method the form has picked
 * (docs/superpowers/specs/2026-10-09-corporate-contract-dual-pricing-design.md §4.3).
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Orders, { collectableOutstanding } from '../../pages/Orders';
import adminService from '../../services/adminService';
import api from '../../services/api';

vi.mock('../../services/adminService');
vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  getCookie: vi.fn(),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, fallback) => (typeof fallback === 'string' ? fallback : key) }),
}));

const CUSTOMER = { id: 77, first_name: 'Ali', last_name: 'Buyer', phone: '+998901234500' };
const ADDRESS = { id: 9, title: 'Office', full_address: 'Chilonzor 5', is_default: true };

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue({ data: { data: { statuses: [], transitions: {} } } });
  adminService.getOrders.mockResolvedValue({ data: { items: [] }, meta: { total: 0 } });
  adminService.getProducts.mockImplementation(async (params) => ({
    data: {
      items: [{
        id: 3,
        name: 'Water 19L',
        price: 15000,
        effective_unit_price: params.payment_method === 'cash' ? 13500 : 12000,
        pricing_source: params.payment_method === 'cash' ? 'contract_cash' : 'contract_business_account',
      }],
    },
  }));
  adminService.getUsers.mockResolvedValue({ data: { items: [CUSTOMER] }, meta: { has_next: false } });
  adminService.getUserDetails.mockResolvedValue({ data: { user: CUSTOMER } });
  adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [ADDRESS] } });
  adminService.getUserPaymentMethods.mockResolvedValue({
    data: {
      available_methods: [
        { method: 'business_account', name: 'Business Account', is_default: true },
        { method: 'cash', name: 'Cash' },
      ],
      payment_restrictions: null,
    },
  });
});

it('re-asks for product prices on the payment method the form picks', async () => {
  render(<Orders />, { wrapper: createWrapper() });
  fireEvent.click(screen.getAllByRole('button', { name: /create order/i })[0]);
  const dialog = await screen.findByRole('dialog', { name: 'Create Order' });

  const customer = within(dialog).getByText('Search customer by name or phone').closest('.ant-select');
  fireEvent.mouseDown(customer.querySelector('.ant-select-selector'));
  fireEvent.change(customer.querySelector('input'), { target: { value: 'Ali' } });
  fireEvent.click(await screen.findByTitle('Ali Buyer - +998901234500'));

  await waitFor(() => expect(adminService.getProducts).toHaveBeenCalledWith(
    expect.objectContaining({ pricing_user_id: 77, payment_method: 'business_account' }),
  ));

  const method = within(dialog).getByLabelText('Payment Method').closest('.ant-select');
  fireEvent.mouseDown(method.querySelector('.ant-select-selector'));
  fireEvent.click(await screen.findByTitle('Cash'));

  await waitFor(() => expect(adminService.getProducts).toHaveBeenLastCalledWith(
    expect.objectContaining({ pricing_user_id: 77, payment_method: 'cash' }),
  ));

  const product = within(dialog).getByText('Select product').closest('.ant-select');
  fireEvent.mouseDown(product.querySelector('.ant-select-selector'));
  expect(await screen.findByText(/^Water 19L - 13,500 UZS \(Contract\)$/)).toBeInTheDocument();
});

describe('collectableOutstanding', () => {
  it('prefers the server cash settlement figure for an unpaid electronic order', () => {
    expect(collectableOutstanding({ cash_settlement_amount: 27000, net_outstanding_amount: 30000 })).toBe(27000);
  });

  it('falls back to the net outstanding when there is no cash settlement figure', () => {
    expect(collectableOutstanding({ cash_settlement_amount: null, net_outstanding_amount: 30000 })).toBe(30000);
  });
});
