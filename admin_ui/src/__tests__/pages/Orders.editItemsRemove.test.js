/**
 * Orders "Edit Items": the minus button on an EXISTING line removes it.
 *
 * The backend reads an existing line that is missing from the payload as unchanged, so a row
 * dropped from the form must still be sent, as quantity 0. Without it, pressing only minus
 * previews "no changes", and minus plus another change silently keeps the "removed" line.
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

const WATER = { id: 7, name: 'Water 19L', base_price: 15000, is_active: true };
const LEMONADE = { id: 9, name: 'Lemonade 5L', base_price: 8000, is_active: true };

const ORDER = {
  id: 640,
  order_number: 'WB_000640_26',
  user_id: 88,
  status: 'confirmed',
  payment_method: 'cash',
  payment_status: 'pending',
  total_amount: 87000,
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
  items: [
    { id: 501, product_id: WATER.id, product_name: WATER.name, quantity: 4, unit_price: 15000, total_price: 60000 },
    { id: 502, product_id: LEMONADE.id, product_name: LEMONADE.name, quantity: 3, unit_price: 8000, total_price: 24000 },
  ],
};

function createWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

function setupMocks() {
  vi.clearAllMocks();
  api.get.mockResolvedValue({ data: { data: { statuses: [{ value: 'confirmed', label: 'Confirmed' }] } } });
  adminService.getOrders.mockResolvedValue({ data: { items: [ORDER] }, meta: { total: 1 } });
  adminService.getOrderDetails.mockResolvedValue({ success: true, data: { order: ORDER_DETAIL } });
  adminService.getOrderEditHistory.mockResolvedValue({ success: true, data: { entries: [] } });
  adminService.getProducts.mockResolvedValue({ data: { items: [WATER, LEMONADE] } });
  adminService.previewOrderEdit.mockResolvedValue({
    data: { blocking_reasons: [], warnings: [], totals_before: {}, totals_after: {}, item_changes: [] },
  });
}

async function openEditItems(user) {
  render(<Orders />, { wrapper: createWrapper() });
  await user.click(await screen.findByText(/view_details|View Details/i));
  await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledWith(ORDER.id));
  await user.click(await screen.findByRole('button', { name: /Edit Items/i }));
  const hint = await screen.findByText(/0 removes a line/i);
  return hint.closest('[role="dialog"]');
}

async function previewWithReason(user, dialog) {
  await user.type(within(dialog).getByPlaceholderText(/customer asked for 2 extra bottles/i), 'lemonade cancelled');
  await user.click(within(dialog).getByRole('button', { name: /Preview impacts/i }));
  await waitFor(() => expect(adminService.previewOrderEdit).toHaveBeenCalledTimes(1));
  return adminService.previewOrderEdit.mock.calls[0];
}

describe('Orders edit items: removing an existing line', () => {
  it('pressing minus on an existing row sends that line as quantity 0', async () => {
    setupMocks();
    const user = userEvent.setup();
    const dialog = await openEditItems(user);

    const minusButtons = within(dialog).getAllByRole('button', { name: /minus-circle/i });
    expect(minusButtons).toHaveLength(2);
    await user.click(minusButtons[1]);
    const [orderId, payload] = await previewWithReason(user, dialog);

    expect(orderId).toBe(ORDER.id);
    expect(payload).toEqual({
      items: [
        { orderItemId: 501, productId: WATER.id, quantity: 4 },
        { orderItemId: 502, productId: LEMONADE.id, quantity: 0 },
      ],
      reason: 'lemonade cancelled',
    });
  });

  it('minus on one row plus a change on another sends both', async () => {
    setupMocks();
    const user = userEvent.setup();
    const dialog = await openEditItems(user);

    await user.click(within(dialog).getAllByRole('button', { name: /minus-circle/i })[1]);
    const waterQuantity = within(dialog).getAllByRole('spinbutton')[0];
    await user.clear(waterQuantity);
    await user.type(waterQuantity, '6');
    const [, payload] = await previewWithReason(user, dialog);

    expect(payload.items).toEqual([
      { orderItemId: 501, productId: WATER.id, quantity: 6 },
      { orderItemId: 502, productId: LEMONADE.id, quantity: 0 },
    ]);
  });

  it('nothing removed: no quantity-0 line is invented', async () => {
    setupMocks();
    const user = userEvent.setup();
    const dialog = await openEditItems(user);

    const [, payload] = await previewWithReason(user, dialog);

    expect(payload.items).toEqual([
      { orderItemId: 501, productId: WATER.id, quantity: 4 },
      { orderItemId: 502, productId: LEMONADE.id, quantity: 3 },
    ]);
  });

  it('minus an existing line, then re-add the same product as a new row: one spec, no quantity-0 removal', async () => {
    // FR-1: M7 made minus append a {orderItemId, quantity: 0} removal for every dropped
    // original line. If the admin then re-adds that line's product via "Add Item" (a new
    // row with no orderItemId), the payload must bind the re-add to the original line — not
    // ALSO send the appended removal, which would give the backend two specs for one line.
    setupMocks();
    const user = userEvent.setup();
    const dialog = await openEditItems(user);

    await user.click(within(dialog).getAllByRole('button', { name: /minus-circle/i })[0]);
    await user.click(within(dialog).getByRole('button', { name: /Add Item/i }));

    const productSelects = within(dialog).getAllByRole('combobox');
    await user.click(productSelects[productSelects.length - 1]);
    await user.click(await screen.findByText(new RegExp(`^${WATER.name} -`)));

    const qtyInputs = within(dialog).getAllByRole('spinbutton');
    const newRowQuantity = qtyInputs[qtyInputs.length - 1];
    await user.clear(newRowQuantity);
    await user.type(newRowQuantity, '6');

    const [, payload] = await previewWithReason(user, dialog);

    expect(payload.items).toEqual([
      { orderItemId: 502, productId: LEMONADE.id, quantity: 3 },
      { orderItemId: null, productId: WATER.id, quantity: 6 },
    ]);
  });
});
