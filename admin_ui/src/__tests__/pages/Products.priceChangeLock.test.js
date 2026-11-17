import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Products from '../../pages/Products';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService');
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallbackOrOptions, maybeOptions) => {
      const fallback =
        typeof fallbackOrOptions === 'string' ? fallbackOrOptions : fallbackOrOptions?.defaultValue;
      const options = typeof fallbackOrOptions === 'string' ? maybeOptions : fallbackOrOptions;
      let text = fallback || key;
      Object.entries(options || {}).forEach(([name, value]) => {
        text = text.replace(`{{${name}}}`, String(value));
      });
      return text;
    },
  }),
}));

// The row action menu never opens in jsdom: flatten it into real buttons.
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

// Every key a fixture below carries; tests/integration/test_products_price_change_lock_ui_contract.py diffs this
// Set against the real admin product row, so a fixture cannot carry a field the backend does not send.
const PRODUCT_ROW_KEYS = new Set([
  'id',
  'name',
  'sku',
  'category_id',
  'price',
  'base_price',
  'catalogue_unit_price',
  'price_change_pending',
  'stock_quantity',
  'status',
  'is_active',
  'created_at',
  'image_url',
  'images',
  'min_order_quantity',
  'volume',
  'is_featured',
  'is_tryout_eligible',
  'in_sales_stock_check',
  'in_agent_stock_list',
  'tracks_returnable_bottles',
  'returnable_bottles_per_unit',
  'barcode',
  'spic',
  'package_code',
  'units',
  'vat_percent',
  'fiscalization_enabled',
  'requires_marking_codes',
  'marking_code_counts',
  'marking_codes_low_stock',
  'marking_codes_low_stock_threshold',
  'missing_required_fields',
]);

const productRow = (overrides = {}) => {
  const row = {
    id: 44,
    name: 'Aqua Element 18.9L',
    sku: 'AQUA-19',
    category_id: 8,
    price: 18000,
    base_price: 18000,
    catalogue_unit_price: 18000,
    price_change_pending: null,
    stock_quantity: 30,
    status: 'active',
    is_active: true,
    created_at: '2026-03-11T10:00:00+00:00',
    image_url: null,
    images: [],
    min_order_quantity: 1,
    volume: 18.9,
    is_featured: false,
    is_tryout_eligible: true,
    in_sales_stock_check: false,
    in_agent_stock_list: false,
    tracks_returnable_bottles: true,
    returnable_bottles_per_unit: 1,
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
  };
  Object.keys(row).forEach((key) => {
    if (!PRODUCT_ROW_KEYS.has(key)) throw new Error(`fixture key ${key} is not in PRODUCT_ROW_KEYS`);
  });
  return row;
};

const mockProducts = (overrides) => {
  adminService.getProducts.mockResolvedValue({
    data: { items: [productRow(overrides)] },
    meta: { total: 1 },
  });
};

const PENDING = { price_change_id: 5, change_on: '2027-02-01' };

const openEditModal = async (user) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <Products />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  await waitFor(() => expect(adminService.getProducts).toHaveBeenCalled());
  const [editButton] = await screen.findAllByText('Edit Product');
  await user.click(editButton);
  return screen.findByRole('dialog');
};

describe('Products price column and the price-change lock', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getCategories.mockResolvedValue({
      data: { items: [{ id: 8, name: 'Water', is_active: true }] },
    });
    mockProducts();
    adminService.updateProduct.mockResolvedValue({ success: true, data: { product: productRow() } });
  });

  it('lists the price customers pay, the discount price when one is set', async () => {
    mockProducts({ price: 18000, base_price: 18000, catalogue_unit_price: 16000 });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <Products />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    const row = (await screen.findByText('Aqua Element 18.9L')).closest('tr');
    expect(within(row).getByText('16,000 UZS')).toBeInTheDocument();
    expect(within(row).queryByText('18,000 UZS')).not.toBeInTheDocument();
  });

  it('locks the price while the product is in a pending price change and points to it', async () => {
    const user = userEvent.setup();
    mockProducts({ price_change_pending: PENDING });

    const dialog = await openEditModal(user);

    expect(within(dialog).getByLabelText('Price')).toBeDisabled();
    expect(
      within(dialog).getByText(
        'This price changes on Feb 01, 2027 through a scheduled price change. Change it there.',
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole('link', { name: 'Open price changes' })).toHaveAttribute(
      'href',
      '/price-changes',
    );
  });

  it('leaves the price editable when nothing is pending', async () => {
    const user = userEvent.setup();

    const dialog = await openEditModal(user);

    expect(within(dialog).getByLabelText('Price')).toBeEnabled();
    expect(within(dialog).queryByRole('link', { name: 'Open price changes' })).not.toBeInTheDocument();
  });

  it('still saves the other fields of a locked product', async () => {
    const user = userEvent.setup();
    mockProducts({ price_change_pending: PENDING });

    const dialog = await openEditModal(user);
    const stock = within(dialog).getByLabelText('Stock Quantity');
    await user.clear(stock);
    await user.type(stock, '30');
    await user.click(within(dialog).getByRole('button', { name: 'Update Product' }));

    await waitFor(() => {
      expect(adminService.updateProduct).toHaveBeenCalledWith(
        44,
        expect.objectContaining({ price: 18000, stock_quantity: 30 }),
      );
    });
  });
});
