import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
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

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

// A business_account order whose customer sits AT the COD active-debt cap:
// OrderPaymentMethodEditService.get_edit_metadata previews every target with
// bypass_cod_check=False, so `cash` is missing from allowed_target_methods even
// though business_account -> cash is an allowed transition.
const CAPPED_BA_ORDER = {
  id: 771,
  order_number: 'TG_000771_26',
  user_id: 91,
  status: 'delivered',
  payment_method: 'business_account',
  payment_status: 'completed',
  total_amount: 48000,
  is_payment_method_editable: true,
  allowed_target_methods: ['card', 'click'],
  customer_name: 'Capped Corporate Customer',
  customer_phone: '+998901112233',
  created_at: '2026-07-20T10:00:00+00:00',
  items_summary: [],
  items_count: 1,
};

function setupBaseMocks() {
  vi.clearAllMocks();

  api.get.mockResolvedValue({
    data: { data: { statuses: [{ value: 'delivered', label: 'Delivered' }] } },
  });

  adminService.getOrders.mockResolvedValue({
    data: { items: [CAPPED_BA_ORDER] },
    meta: { total: 1 },
  });
  adminService.getOrderDetails.mockResolvedValue({
    success: true,
    data: { order: { ...CAPPED_BA_ORDER, items: [] } },
  });
  adminService.getOrderEditHistory.mockResolvedValue({ success: true, data: { entries: [] } });
  adminService.getProducts.mockResolvedValue({ data: { items: [] } });

  adminService.previewOrderPaymentMethod.mockResolvedValue({
    data: {
      order_id: 771,
      current_method: 'business_account',
      new_method: 'cash',
      is_delivered: true,
      blocking_reasons: [],
      warnings: [],
    },
  });
  adminService.submitOrderPaymentMethod.mockResolvedValue({
    data: { order_id: 771, new_method: 'cash', warnings: [] },
  });
}

async function openPaymentMethodModal(user) {
  render(<Orders />, { wrapper: createWrapper() });

  await waitFor(() => expect(adminService.getOrders).toHaveBeenCalled());
  await user.click(await screen.findByText(/view_details|View Details/i));
  await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledWith(771));

  fireEvent.click(await screen.findByText('Change'));
  return screen.findByText(/Preview impacts/i);
}

function paymentMethodDialog() {
  const dialogs = document.querySelectorAll('.ant-modal-content');
  return dialogs[dialogs.length - 1];
}

// antd renders each option as `.ant-select-item-option[title="<value>"]`, so the
// title attribute is the unambiguous handle (matching on text finds both the
// option wrapper and its `-content` child).
function openMethodDropdown() {
  fireEvent.mouseDown(paymentMethodDialog().querySelector('.ant-select-selector'));
}

function methodOption(value) {
  return document.querySelector(`.ant-select-dropdown .ant-select-item-option[title="${value}"]`);
}

async function pickNewMethod(value) {
  openMethodDropdown();
  await waitFor(() => expect(methodOption(value)).toBeTruthy());
  fireEvent.click(methodOption(value));
}

describe('Orders payment-method edit — COD cap override', () => {
  it('does not offer cash until the override checkbox is ticked', async () => {
    setupBaseMocks();
    const user = userEvent.setup();
    await openPaymentMethodModal(user);

    openMethodDropdown();
    await waitFor(() => expect(methodOption('click')).toBeTruthy());
    expect(methodOption('cash')).toBeNull();

    // Tick the override; `cash` becomes selectable.
    fireEvent.click(screen.getByRole('checkbox', { name: /Override the cash-on-delivery debt cap/i }));

    await waitFor(() => expect(methodOption('cash')).toBeTruthy());
  });

  it('sends bypass_cod_check: true to BOTH the preview and the apply call', async () => {
    setupBaseMocks();
    const user = userEvent.setup();
    await openPaymentMethodModal(user);

    fireEvent.click(screen.getByRole('checkbox', { name: /Override the cash-on-delivery debt cap/i }));
    await pickNewMethod('cash');

    const dialog = paymentMethodDialog();
    const reason = dialog.querySelector('textarea');
    await user.clear(reason);
    await user.type(reason, 'card payment failed, driver collected cash at the door');

    fireEvent.click(screen.getByText(/Preview impacts/i));

    await waitFor(() => {
      expect(adminService.previewOrderPaymentMethod).toHaveBeenCalledWith(771, {
        new_method: 'cash',
        bypass_cod_check: true,
      });
    });

    fireEvent.click(await screen.findByText(/Confirm and apply/i));

    await waitFor(() => {
      expect(adminService.submitOrderPaymentMethod).toHaveBeenCalledWith(771, {
        new_method: 'cash',
        reason: 'card payment failed, driver collected cash at the door',
        bypass_cod_check: true,
      });
    });
  });

  it('sends bypass_cod_check: false when the override is left untouched', async () => {
    setupBaseMocks();
    const user = userEvent.setup();
    await openPaymentMethodModal(user);

    const dialog = paymentMethodDialog();
    const reason = dialog.querySelector('textarea');
    await user.clear(reason);
    await user.type(reason, 'customer switched to online card payment');

    fireEvent.click(screen.getByText(/Preview impacts/i));

    await waitFor(() => {
      expect(adminService.previewOrderPaymentMethod).toHaveBeenCalledWith(771, {
        new_method: 'card',
        bypass_cod_check: false,
      });
    });
  });
});
