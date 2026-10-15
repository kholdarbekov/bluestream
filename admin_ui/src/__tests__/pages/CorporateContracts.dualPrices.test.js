/**
 * Contract prices: two optional inputs per product, an empty one is the live catalogue price
 * (docs/superpowers/specs/2026-10-09-corporate-contract-dual-pricing-design.md §6.2).
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import CorporateContracts from '../../pages/CorporateContracts';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService');
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, fallback) => (typeof fallback === 'string' ? fallback : key) }),
}));

const PRICE_ROW = {
  id: 5,
  contract_id: 41,
  product_id: 3,
  product_name: 'Water 19L',
  business_account_unit_price: 12000,
  cash_unit_price: null,
  catalogue_unit_price: 15000,
  is_prepayment_eligible: true,
  is_active: true,
  notes: '',
};
const CONTRACT = {
  id: 41,
  user_id: 77,
  contract_number: 'C-41',
  name: 'Office water',
  status: 'active',
  currency: 'UZS',
  is_active: true,
  tracking_mode: 'units',
  prices: [PRICE_ROW],
};

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

beforeEach(() => {
  vi.clearAllMocks();
  adminService.getCorporateContracts.mockResolvedValue({ data: { items: [CONTRACT] }, meta: { total: 1 } });
  adminService.getCorporateContract.mockResolvedValue({ data: { contract: CONTRACT } });
  adminService.getCorporateContractBalance.mockResolvedValue({ data: { balance: { products: [], summary: {} } } });
  adminService.getCorporateContractLedger.mockResolvedValue({ data: { items: [] } });
  adminService.getProducts.mockResolvedValue({ data: { items: [{ id: 3, name: 'Water 19L', catalogue_unit_price: 15000 }] } });
  adminService.getUsers.mockResolvedValue({ data: { items: [] } });
  adminService.updateCorporateContractPrices.mockResolvedValue({ success: true, data: { prices: [] } });
});

it('lists both prices and names the catalogue for an empty one', async () => {
  render(<CorporateContracts />, { wrapper: createWrapper() });

  expect(await screen.findByText('12,000 UZS')).toBeInTheDocument();
  expect(screen.getByText('Catalogue (15,000)')).toBeInTheDocument();
});

it('saves an emptied price as null, never as the catalogue figure', async () => {
  render(<CorporateContracts />, { wrapper: createWrapper() });

  fireEvent.click(await screen.findByRole('button', { name: 'Manage Prices' }));
  const drawer = (await screen.findByText('Manage Contract Prices')).closest('.ant-drawer-content');
  const cash = within(drawer).getByLabelText('Cash price');
  await waitFor(() => expect(cash).toHaveAttribute('placeholder', 'Catalogue (15,000)'));

  fireEvent.change(within(drawer).getByLabelText('Bank transfer price'), { target: { value: '' } });
  fireEvent.click(within(drawer).getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(adminService.updateCorporateContractPrices).toHaveBeenCalledWith(41, [
    {
      product_id: 3,
      business_account_unit_price: null,
      cash_unit_price: null,
      is_prepayment_eligible: true,
      is_active: true,
      notes: '',
    },
  ]));
});
