/**
 * Orders "Edit Items" preview: the "Driver trip" row shows what the edit changes on the driver's trip.
 *
 * The backend decides what the apply books on the trip and whether it reopens it
 * (`cascade_summary.trip_effect`, the apply's own reader). The page only shows it: the trip's
 * units, the reopen warning when `reopens`, and, when the apply would refuse the reopen because
 * the driver is on another trip, the reason, with "Confirm and apply" disabled. The "Bottle
 * balance" row is the customer's ledger only and no longer reads `affected_session_id`.
 */
import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Orders from '../../pages/Orders';
import adminService from '../../services/adminService';
import api from '../../services/api';

vi.mock('../../services/adminService');
vi.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
  getCookie: vi.fn(),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback) => fallback || key,
  }),
}));
vi.mock('../../components/common/PermissionGuard', async () => {
  const actual = await vi.importActual('../../components/common/PermissionGuard');
  return {
    ...actual,
    usePermissions: vi.fn(() => ({
      isAdmin: () => true,
      isManager: () => false,
      isOperator: () => false,
      hasPermission: () => true,
      canManageOrders: () => true,
    })),
  };
});

// The row's action menu, flattened into buttons (the Orders.collectedCashEdit precedent).
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

vi.setConfig({ testTimeout: 15000 });

const WATER = { id: 7, name: '19 Litr suv', base_price: 15000, is_active: true };
const TEN_LITRE = { id: 9, name: '10 Litr suv', base_price: 9000, is_active: true };

const ORDER = {
  id: 640,
  order_number: 'WB_000640_26',
  user_id: 88,
  status: 'delivered',
  payment_method: 'cash',
  payment_status: 'completed',
  total_amount: 66000,
  customer_name: 'Bahor market',
  customer_email: 'shop@example.com',
  customer_phone: '+998901234567',
  created_at: '2026-10-04T10:00:00+05:00',
  items_summary: [],
  items_count: 2,
};

const ORDER_DETAIL = {
  ...ORDER,
  is_editable: true,
  edit_window_remaining_hours: 70,
  items: [
    { id: 501, product_id: WATER.id, product_name: WATER.name, quantity: 2, unit_price: 15000, total_price: 30000 },
    { id: 502, product_id: TEN_LITRE.id, product_name: TEN_LITRE.name, quantity: 4, unit_price: 9000, total_price: 36000 },
  ],
};

function createWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

function setupMocks(cascadeSummary) {
  vi.clearAllMocks();
  api.get.mockResolvedValue({ data: { data: { statuses: [{ value: 'delivered', label: 'Delivered' }] } } });
  adminService.getOrders.mockResolvedValue({ data: { items: [ORDER] }, meta: { total: 1 } });
  adminService.getOrderDetails.mockResolvedValue({ success: true, data: { order: ORDER_DETAIL } });
  adminService.getOrderEditHistory.mockResolvedValue({ success: true, data: { entries: [] } });
  adminService.getProducts.mockResolvedValue({ data: { items: [WATER, TEN_LITRE] } });
  adminService.previewOrderEdit.mockResolvedValue({
    data: {
      blocking_reasons: [],
      warnings: [],
      totals_before: {},
      totals_after: {},
      cascade_summary: cascadeSummary,
      is_post_delivery: true,
    },
  });
}

async function openThePreview(user) {
  render(<Orders />, { wrapper: createWrapper() });
  await user.click(await screen.findByText(/view_details|View Details/i));
  await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledWith(ORDER.id));
  await user.click(await screen.findByRole('button', { name: /Edit Items/i }));
  const dialog = (await screen.findByText(/0 removes a line/i)).closest('[role="dialog"]');
  await user.type(within(dialog).getByPlaceholderText(/customer asked for 2 extra bottles/i), 'took two more 10 L');
  await user.click(within(dialog).getByRole('button', { name: /Preview impacts/i }));
  await waitFor(() => expect(adminService.previewOrderEdit).toHaveBeenCalledTimes(1));
  return (await screen.findByText('Driver trip')).closest('[role="dialog"]');
}

const REOPEN_WARNING = /Driver bottle session #\d+ will be reopened/;
const REFUSAL =
  'The driver has another open trip or rides on one: this edit will be refused until they close or leave it.';

describe('Orders edit items preview: the driver trip row', () => {
  it('shows the trip units and the reopen warning when the edit reopens the closed trip', async () => {
    setupMocks({
      bottle_balance: { changes: [] },
      trip_effect: {
        session_id: 42,
        bottles: 0,
        units: [{ product_id: TEN_LITRE.id, product_name: '10 Litr suv', delta: 2 }],
        reopens: true,
        reopen_refusal: null,
      },
    });
    const user = userEvent.setup();
    const dialog = await openThePreview(user);

    expect(within(dialog).getByText('10 Litr suv: +2')).toBeInTheDocument();
    expect(within(dialog).getByText('Driver bottle session #42 will be reopened')).toBeInTheDocument();
    expect(within(dialog).getByText('No bottle balance change')).toBeInTheDocument();
    expect(within(dialog).queryByText("No change on the driver's trip")).not.toBeInTheDocument();
    expect(within(dialog).queryByText(REFUSAL)).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /Confirm and apply/i })).toBeEnabled();
  });

  it('shows no reopen warning when the trip stays as it is, whatever the bottle balance row still carries', async () => {
    setupMocks({
      bottle_balance: { changes: [{ product_id: WATER.id, delta_bottles: 1 }], affected_session_id: 7 },
      trip_effect: { session_id: 7, bottles: 1, units: [], reopens: false, reopen_refusal: null },
    });
    const user = userEvent.setup();
    const dialog = await openThePreview(user);

    expect(within(dialog).getByText('+1 bottles')).toBeInTheDocument();
    expect(within(dialog).getByText('Product 7: +1 bottles')).toBeInTheDocument();
    expect(within(dialog).queryByText(REOPEN_WARNING)).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /Confirm and apply/i })).toBeEnabled();
  });

  // A reward line and a paid line of the same product are two plan changes, so the preview's ledger carries two
  // `changes` entries with one product_id (order_edit_service's bottle_changes).
  it('lists two bottle balance changes of one product as two rows, without a duplicate-key warning', async () => {
    setupMocks({
      bottle_balance: {
        changes: [
          { product_id: WATER.id, delta_bottles: 1 },
          { product_id: WATER.id, delta_bottles: 2 },
        ],
      },
      trip_effect: { session_id: 7, bottles: 3, units: [], reopens: false, reopen_refusal: null },
    });
    const consoleError = vi.spyOn(console, 'error');
    try {
      const user = userEvent.setup();
      const dialog = await openThePreview(user);

      expect(within(dialog).getByText('Product 7: +1 bottles')).toBeInTheDocument();
      expect(within(dialog).getByText('Product 7: +2 bottles')).toBeInTheDocument();
      const duplicateKeyWarnings = consoleError.mock.calls.filter((args) => args.some(
        (arg) => typeof arg === 'string' && arg.includes('Encountered two children with the same key'),
      ));
      expect(duplicateKeyWarnings).toEqual([]);
    } finally {
      consoleError.mockRestore();
    }
  });

  it('says why the apply will be refused and disables Confirm and apply', async () => {
    setupMocks({
      bottle_balance: { changes: [{ product_id: WATER.id, delta_bottles: 1 }] },
      trip_effect: {
        session_id: 7,
        bottles: 1,
        units: [],
        reopens: true,
        reopen_refusal: 'BOTTLE_SESSION_ACTIVE_CONFLICT',
      },
    });
    const user = userEvent.setup();
    const dialog = await openThePreview(user);

    expect(within(dialog).getByText(REFUSAL)).toBeInTheDocument();
    expect(within(dialog).getByText('Driver bottle session #7 will be reopened')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /Confirm and apply/i })).toBeDisabled();
  });
});
