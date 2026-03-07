import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from 'react-query';
import { BrowserRouter } from 'react-router-dom';
import { message } from 'antd';

import Users from '../../pages/Users';
import adminService from '../../services/adminService';
import staffService from '../../services/staffService';

jest.mock('../../services/adminService');
jest.mock('../../services/staffService');

jest.mock('../../components/AddressMapPicker', () => () => <div data-testid="address-map-picker" />);

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: jest.fn(),
  },
}));

jest.mock('../../hooks/useResponsive', () => () => ({
  isMobileDevice: false,
  isTabletDevice: false,
  isTouchDevice: false,
  getFontSize: (mobile, _tablet, desktop) => desktop || mobile,
}));

jest.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback) => fallback || key,
  }),
}));

jest.mock('antd', () => {
  const actual = jest.requireActual('antd');
  return {
    ...actual,
    Dropdown: ({ menu, children }) => (
      <div>
        {children}
        {menu?.items
          ?.filter((item) => item && item.type !== 'divider' && item.onClick)
          .map((item) => (
            <button
              key={item.key}
              disabled={item.disabled}
              onClick={item.onClick}
              type="button"
            >
              {typeof item.label === 'string' ? item.label : item.key}
            </button>
          ))}
      </div>
    ),
    message: {
      success: jest.fn(),
      error: jest.fn(),
      info: jest.fn(),
      warning: jest.fn(),
      loading: jest.fn(),
      destroy: jest.fn(),
      open: jest.fn(),
    },
  };
});

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return ({ children }) => (
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </BrowserRouter>
  );
};

describe('Users page notification settings modal flow', () => {
  beforeEach(() => {
    jest.clearAllMocks();

    adminService.getUsers.mockResolvedValue({
      data: {
        items: [
          {
            id: 11,
            first_name: 'Alice',
            last_name: 'Tester',
            email: 'alice@example.com',
            phone: '+998901234567',
            status: 'active',
            role: 'customer',
            user_type: 'individual',
            telegram_id: '998901234567',
            is_bot_active: true,
            created_at: '2026-03-01T10:00:00+00:00',
            last_login: '2026-03-02T10:00:00+00:00',
          },
        ],
      },
      meta: {
        total: 1,
        page: 1,
        per_page: 20,
      },
    });

    adminService.getUserAddresses.mockResolvedValue({
      data: {
        addresses: [],
      },
    });

    adminService.getUserNotificationSettings.mockResolvedValue({
      data: {
        notification_settings: {
          delivery_telegram_status_updates_enabled: true,
          delivery_telegram_status_updates_source: 'default',
          telegram_connected: true,
          bot_active: true,
          updated_at: null,
        },
      },
    });

    adminService.updateUserNotificationSettings.mockResolvedValue({
      data: {
        notification_settings: {
          delivery_telegram_status_updates_enabled: false,
          delivery_telegram_status_updates_source: 'explicit',
          telegram_connected: true,
          bot_active: true,
          updated_at: '2026-03-05T10:00:00+00:00',
        },
      },
      message: 'updated',
    });

    adminService.updateUserStatus.mockResolvedValue({});
    adminService.createUser.mockResolvedValue({});
    adminService.updateUser.mockResolvedValue({});
    adminService.createUserAddress.mockResolvedValue({});
    adminService.updateUserAddress.mockResolvedValue({});
    adminService.deleteUserAddress.mockResolvedValue({});
    adminService.unlockUserAccount.mockResolvedValue({});
    staffService.getCustomerCodStatement.mockResolvedValue({
      data: {
        data: {
          active_cod_debt_count: 0,
          total_outstanding_amount: 0,
          cod_restricted: false,
          items: [],
        },
      },
    });
  });

  it('requires reason before update and sends expected payload after reason is provided', async () => {
    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getUsers).toHaveBeenCalled();
    });

    await screen.findByText('alice@example.com');
    await user.click(screen.getByText('ui.users.view_details'));

    await waitFor(() => {
      expect(adminService.getUserNotificationSettings).toHaveBeenCalledWith(11);
    });

    const toggle = await screen.findByRole('switch');
    await user.click(toggle);

    const confirmButton = await screen.findByRole('button', { name: 'Confirm' });

    await user.click(confirmButton);

    expect(message.error).toHaveBeenCalledWith('Reason is required');
    expect(adminService.updateUserNotificationSettings).not.toHaveBeenCalled();

    await user.type(
      screen.getByPlaceholderText('Enter reason'),
      'Customer requested disable via phone'
    );

    await user.click(confirmButton);

    await waitFor(() => {
      expect(adminService.updateUserNotificationSettings).toHaveBeenCalledWith(11, {
        delivery_telegram_status_updates_enabled: false,
        reason: 'Customer requested disable via phone',
      });
    });
  });
});
