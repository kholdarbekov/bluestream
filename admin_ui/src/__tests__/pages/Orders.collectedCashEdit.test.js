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

// Mock PermissionGuard so we can control isAdmin() per test
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

vi.setConfig({ testTimeout: 10000 });

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

const DELIVERED_CASH_ORDER = {
  id: 620,
  order_number: 'TG_000183_26',
  user_id: 88,
  status: 'delivered',
  payment_method: 'cash',
  payment_status: 'completed',
  total_amount: 54000,
  amount_collected: 54000,
  outstanding_amount: 0,
  is_collected_cash_editable: true,
  collected_cash_edit_window_remaining_hours: 23.5,
  customer_name: 'Test Driver Cash',
  customer_email: 'test@example.com',
  customer_phone: '+998901234567',
  created_at: '2026-06-23T10:00:00+00:00',
  items_summary: [],
  items_count: 1,
};

const DELIVERED_CASH_ORDER_DETAIL = {
  ...DELIVERED_CASH_ORDER,
  items: [],
};

function setupBaseMocks() {
  vi.clearAllMocks();

  api.get.mockResolvedValue({
    data: {
      data: {
        statuses: [
          { value: 'pending', label: 'Pending' },
          { value: 'confirmed', label: 'Confirmed' },
          { value: 'delivered', label: 'Delivered' },
        ],
      },
    },
  });

  adminService.getOrders.mockResolvedValue({
    data: { items: [DELIVERED_CASH_ORDER] },
    meta: { total: 1 },
  });

  adminService.getOrderDetails.mockResolvedValue({
    success: true,
    data: { order: DELIVERED_CASH_ORDER_DETAIL },
  });

  adminService.getOrderEditHistory.mockResolvedValue({
    success: true,
    data: { entries: [] },
  });

  adminService.previewCollectedCashEdit.mockResolvedValue({
    data: {
      new_amount: 60000,
      surplus_or_shortfall: 6000,
      customer_credit_delta: 6000,
      session_will_reopen: false,
      warnings: [],
    },
  });

  adminService.editCollectedCash.mockResolvedValue({
    data: { order_id: 620, warnings: [] },
  });

  adminService.getProducts.mockResolvedValue({
    data: { items: [] },
  });
}

describe('Orders collected cash edit flow', () => {
  it('shows Edit collected cash button for admin + editable order', async () => {
    setupBaseMocks();

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    await user.click(await screen.findByText(/view_details|View Details/i));

    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(620);
    });

    expect(await screen.findByText(/Edit collected cash/i)).toBeInTheDocument();
  });

  it('opens modal and clicking Preview impact calls previewCollectedCashEdit, then Apply correction calls editCollectedCash', async () => {
    setupBaseMocks();

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    await user.click(await screen.findByText(/view_details|View Details/i));

    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(620);
    });

    // Open the modal
    fireEvent.click(await screen.findByText(/Edit collected cash/i));

    // Check modal opened (step 1 form is visible)
    expect(await screen.findByText(/Preview impact/i)).toBeInTheDocument();

    // Fill in required reason field before previewing (textarea for reason)
    const reasonInputs = screen.getAllByRole('textbox');
    const reasonInput = reasonInputs[reasonInputs.length - 1];
    await user.clear(reasonInput);
    await user.type(reasonInput, 'driver collected extra cash from customer');

    // Click preview
    fireEvent.click(screen.getByText(/Preview impact/i));

    await waitFor(() => {
      expect(adminService.previewCollectedCashEdit).toHaveBeenCalled();
    });

    // Confirm step 2 — Apply correction button should appear
    const applyBtn = await screen.findByText(/Apply correction/i);
    expect(applyBtn).toBeInTheDocument();

    // Click Apply correction
    fireEvent.click(applyBtn);

    await waitFor(() => {
      expect(adminService.editCollectedCash).toHaveBeenCalled();
    });
  });

  it('disables Apply correction and shows error Alert when preview returns blocking_reasons', async () => {
    setupBaseMocks();
    // Override preview to return a blocked response with blocking_reasons
    adminService.previewCollectedCashEdit.mockResolvedValueOnce({
      data: {
        new_amount: 60000,
        surplus_or_shortfall: 6000,
        customer_credit_delta: 6000,
        session_will_reopen: false,
        is_editable: false,
        blocking_reasons: ['cash_session_active_conflict: driver has another active session (id=42); submit & verify it first'],
        warnings: [],
      },
    });

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    await user.click(await screen.findByText(/view_details|View Details/i));

    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(620);
    });

    fireEvent.click(await screen.findByText(/Edit collected cash/i));

    // Fill reason then preview
    const reasonInputs = screen.getAllByRole('textbox');
    const reasonInput = reasonInputs[reasonInputs.length - 1];
    await user.clear(reasonInput);
    await user.type(reasonInput, 'driver collected extra cash from customer');

    fireEvent.click(screen.getByText(/Preview impact/i));

    await waitFor(() => {
      expect(adminService.previewCollectedCashEdit).toHaveBeenCalled();
    });

    // Blocking reason text must be visible
    expect(await screen.findByText(/cash_session_active_conflict/i)).toBeInTheDocument();

    // Apply correction button must be disabled
    const applyBtn = await screen.findByText(/Apply correction/i);
    expect(applyBtn.closest('button')).toBeDisabled();

    // editCollectedCash must NOT have been called
    expect(adminService.editCollectedCash).not.toHaveBeenCalled();
  });

  it('does NOT show Edit collected cash button when isAdmin() is false', async () => {
    // Override usePermissions mock to return isAdmin: () => false for this test
    const { usePermissions } = await import('../../components/common/PermissionGuard');
    usePermissions.mockImplementation(() => ({
      isAdmin: () => false,
      isManager: () => false,
      isOperator: () => false,
      hasPermission: () => true,
      canManageOrders: () => true,
    }));

    setupBaseMocks();

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    await user.click(await screen.findByText(/view_details|View Details/i));

    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(620);
    });

    // Wait a moment for the order details to render
    await screen.findByText(/Payment Summary/i);

    expect(screen.queryByText(/Edit collected cash/i)).toBeNull();

    // Restore for subsequent tests
    usePermissions.mockImplementation(() => ({
      isAdmin: () => true,
      isManager: () => false,
      isOperator: () => false,
      hasPermission: () => true,
      canManageOrders: () => true,
    }));
  });
});
