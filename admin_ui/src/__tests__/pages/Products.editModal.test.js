import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Products from '../../pages/Products';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService');
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallbackOrOptions, maybeOptions) => {
      const fallback =
        typeof fallbackOrOptions === 'string'
          ? fallbackOrOptions
          : fallbackOrOptions?.defaultValue;
      const options =
        typeof fallbackOrOptions === 'string'
          ? maybeOptions
          : fallbackOrOptions;

      if (options?.count !== undefined && typeof fallback === 'string') {
        return fallback.replace('{{count}}', String(options.count));
      }
      return fallback || key;
    },
  }),
}));

// The row action menu never opens in jsdom, so flatten it into real buttons —
// otherwise "Edit Product" is unreachable and none of this can be driven.
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
            <button key={item.key} onClick={item.onClick} type="button" disabled={item.disabled}>
              {typeof item.label === 'string' ? item.label : item.key}
            </button>
          ))}
      </div>
    ),
  };
});

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

// Mirrors serialize_product_admin's payload for one row. Every key the edit
// modal needs must be present here, because the whole bug class is the modal
// failing to read a field the API already sends.
const productRow = (overrides = {}) => ({
  id: 44,
  name: 'Aqua Element 18.9L',
  sku: 'AQUA-19',
  category_id: 8,
  price: 18000,
  base_price: 18000,
  stock_quantity: 24,
  min_order_quantity: 1,
  volume: 18.9,
  status: 'active',
  is_featured: false,
  is_tryout_eligible: true,
  tracks_returnable_bottles: true,
  returnable_bottles_per_unit: 1,
  created_at: '2026-03-11T10:00:00+00:00',
  image_url: null,
  images: [],
  barcode: '4780011111111',
  spic: 'SPIC-19L',
  package_code: 'PACK-19L',
  units: 'pcs',
  vat_percent: 12,
  fiscalization_enabled: true,
  requires_marking_codes: false,
  marking_code_counts: { available: 3, reserved: 1, used: 4, archived: 0 },
  marking_codes_low_stock: false,
  marking_codes_low_stock_threshold: 10,
  missing_required_fields: [],
  ...overrides,
});

const mockProducts = (overrides) => {
  adminService.getProducts.mockResolvedValue({
    data: { items: [productRow(overrides)] },
    meta: { total: 1 },
  });
};

const openEditModal = async (user) => {
  render(<Products />, { wrapper: createWrapper() });

  await waitFor(() => {
    expect(adminService.getProducts).toHaveBeenCalled();
  });

  // The dropdown item and the detail-modal button share this label; the modal
  // title is "Edit Product - <name>", so an exact match hits only the buttons.
  const [editButton] = await screen.findAllByText('Edit Product');
  await user.click(editButton);

  return screen.findByRole('dialog');
};

describe('Products edit modal', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    adminService.getCategories.mockResolvedValue({
      data: { items: [{ id: 8, name: 'Water', is_active: true }] },
    });

    mockProducts();

    adminService.updateProduct.mockResolvedValue({
      success: true,
      data: { product: productRow() },
    });
  });

  it('pre-fills the SKU from the product row', async () => {
    const user = userEvent.setup();

    const dialog = await openEditModal(user);

    expect(within(dialog).getByLabelText('SKU')).toHaveValue('AQUA-19');
  });

  it('submits an unmodified stock change without the admin retyping any field', async () => {
    const user = userEvent.setup();

    const dialog = await openEditModal(user);
    const stockInput = within(dialog).getByLabelText('Stock Quantity');
    await user.clear(stockInput);
    await user.type(stockInput, '77');
    await user.click(within(dialog).getByRole('button', { name: 'Update Product' }));

    await waitFor(() => {
      expect(adminService.updateProduct).toHaveBeenCalledWith(
        44,
        expect.objectContaining({ sku: 'AQUA-19', stock_quantity: 77 }),
      );
    });
  });

  it('locks the stock field for a marking-code product, whose stock is derived', async () => {
    const user = userEvent.setup();
    mockProducts({ requires_marking_codes: true });

    const dialog = await openEditModal(user);

    expect(within(dialog).getByLabelText('Stock Quantity')).toBeDisabled();
    expect(within(dialog).getByLabelText('SKU')).toBeEnabled();
  });

  it('shows the derived code count as stock for a marking-code product', async () => {
    const user = userEvent.setup();
    // What serialize_product_admin now sends: stock_quantity IS the pool.
    mockProducts({ requires_marking_codes: true, stock_quantity: 2,
                   marking_code_counts: { available: 2, reserved: 0, used: 0, archived: 0 } });

    render(<Products />, { wrapper: createWrapper() });
    await waitFor(() => expect(adminService.getProducts).toHaveBeenCalled());

    const row = (await screen.findByText('Aqua Element 18.9L')).closest('tr');
    // The Stock column and the Fiscal column's "Available codes" figure both
    // read from the same derived number now -- that's the point of the
    // change, since those two columns previously showed contradictory
    // numbers. Scope to the row and accept either match rather than
    // asserting a unique occurrence.
    expect(within(row).getAllByText('2').length).toBeGreaterThan(0);

    const [editButton] = await screen.findAllByText('Edit Product');
    await user.click(editButton);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText('Stock Quantity')).toBeDisabled();
  });
});
