/**
 * Products page: a refused bottle-trip setting reads as ONE message, in the admin's language.
 *
 * The page explains TRIP_UNIT_SETTING_BLOCKED (with the open trips) and TRIP_UNIT_ON_RETURNABLE itself
 * (productSaveErrorMessage). The REAL api.js interceptor and the REAL adminService run here, so this
 * proves the save names both codes and api.js does not toast the backend's English sentence beside the
 * page's message. Only the wire is stubbed: `api.defaults.adapter` stands in for the HTTP round trip.
 */
import React from 'react';
import axios, { AxiosError } from 'axios';
import toast from 'react-hot-toast';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: { error: vi.fn(), success: vi.fn() },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallbackOrOptions, maybeOptions) => {
      const fallback = typeof fallbackOrOptions === 'string' ? fallbackOrOptions : fallbackOrOptions?.defaultValue;
      const options = typeof fallbackOrOptions === 'string' ? maybeOptions : fallbackOrOptions;
      if (options && typeof fallback === 'string') {
        return fallback.replace(/{{(\w+)}}/g, (match, name) => (
          // eslint-disable-next-line security/detect-object-injection
          options[name] !== undefined ? String(options[name]) : match
        ));
      }
      return fallback || key;
    },
  }),
}));

// The row action menu never opens in jsdom: flatten it into real buttons (Products.editModal precedent).
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

let api;
let Products;

beforeAll(async () => {
  // api.js fetches a CSRF token with the bare axios client the moment it is imported; answer it here.
  vi.spyOn(axios, 'get').mockResolvedValue({ data: { csrf_token: 'test-csrf' } });
  api = (await import('../../services/api')).default;
  Products = (await import('../../pages/Products')).default;
});

// serialize_product_admin's row for a non-returnable product, not yet counted on the trip.
const PRODUCT = {
  id: 44,
  name: '10 Litr suv',
  sku: 'AQUA-10',
  category_id: 8,
  price: 9000,
  base_price: 9000,
  catalogue_unit_price: 9000,
  price_change_pending: null,
  stock_quantity: 30,
  min_order_quantity: 1,
  volume: 10,
  status: 'active',
  is_active: true,
  is_featured: false,
  is_tryout_eligible: true,
  in_sales_stock_check: false,
  in_agent_stock_list: false,
  tracks_returnable_bottles: false,
  returnable_bottles_per_unit: 0,
  counted_on_bottle_trip: false,
  created_at: '2026-03-11T10:00:00+00:00',
  image_url: null,
  images: [],
  requires_marking_codes: false,
  marking_code_counts: { available: 0, reserved: 0, used: 0, archived: 0 },
  marking_codes_low_stock: false,
  missing_required_fields: [],
};

const ok = (config, data) => Promise.resolve({ data, status: 200, statusText: 'OK', headers: {}, config });
const refuse = (config, status, data) => Promise.reject(
  new AxiosError(`Request failed with status code ${status}`, AxiosError.ERR_BAD_REQUEST, config, null, {
    status, statusText: '', data, headers: {}, config,
  }),
);

// The 18.9 L row: a returnable bottle. The page shows it the setting too; the backend refuses the tick.
const RETURNABLE = {
  ...PRODUCT,
  name: 'Aqua Element 18.9L',
  sku: 'AQUA-19',
  volume: 18.9,
  tracks_returnable_bottles: true,
  returnable_bottles_per_unit: 1,
};

// The admin routes the page calls, answered as the backend does; the save of `refusal.product` is refused.
const wire = (refusal) => {
  const saves = [];
  api.defaults.adapter = (config) => {
    const method = config.method.toLowerCase();
    if (method === 'get' && config.url === '/admin/categories') {
      return ok(config, { success: true, data: { items: [{ id: 8, name: 'Water', is_active: true }] } });
    }
    if (method === 'get' && config.url === '/admin/products') {
      return ok(config, { success: true, data: { items: [refusal.product] }, meta: { total: 1 } });
    }
    if (method === 'put' && config.url === `/admin/products/${refusal.product.id}`) {
      saves.push(JSON.parse(config.data));
      return refuse(config, refusal.status, refusal.body);
    }
    return ok(config, { success: true, data: { items: [], total: 0 } });
  };
  return saves;
};

const SETTING = "Count on the driver's bottle trip";

const REFUSALS = [
  {
    code: 'TRIP_UNIT_SETTING_BLOCKED',
    product: PRODUCT,
    status: 409,
    body: {
      success: false,
      error: 'CONFLICT',
      message: 'Close every open bottle trip before changing whether this product is counted on the trip.',
      error_code: 'TRIP_UNIT_SETTING_BLOCKED',
      details: { open_trips: [{ session_id: 9, session_ref: 'a1b2c3d4e5', driver_user_id: 3, driver_name: 'Aziz' }] },
    },
    localized: 'Close these open trips first: Aziz (#a1b2c3d4)',
  },
  {
    code: 'TRIP_UNIT_ON_RETURNABLE',
    product: RETURNABLE,
    status: 400,
    body: {
      success: false,
      error: 'VALIDATION_ERROR',
      message: 'A product counted on the bottle trip cannot be a returnable bottle.',
      error_code: 'TRIP_UNIT_ON_RETURNABLE',
    },
    localized: 'A product counted on the bottle trip cannot be returnable. Untick the setting first.',
  },
];

describe('Products page: a refused bottle-trip setting', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it.each(REFUSALS)('$code shows only the localized message, never a toast of the English one', async (refusal) => {
    const saves = wire(refusal);
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><Products /></QueryClientProvider>);

    const [editButton] = await screen.findAllByText('Edit Product');
    await user.click(editButton);
    const dialog = await screen.findByRole('dialog');
    // The modal opens with an animation, so wait for it to settle before reading visibility.
    await waitFor(() => expect(within(dialog).getByLabelText(SETTING)).toBeVisible());
    await user.click(within(dialog).getByLabelText(SETTING));
    await user.click(within(dialog).getByRole('button', { name: 'Update Product' }));

    expect(await screen.findByText(refusal.localized)).toBeInTheDocument();
    await waitFor(() => expect(saves).toHaveLength(1));
    expect(saves[0]).toMatchObject({
      counted_on_bottle_trip: true,
      tracks_returnable_bottles: refusal.product.tracks_returnable_bottles,
      returnable_bottles_per_unit: refusal.product.returnable_bottles_per_unit,
    });
    expect(toast.error).not.toHaveBeenCalled();
    expect(screen.queryByText(refusal.body.message)).toBeNull();
  });
});
