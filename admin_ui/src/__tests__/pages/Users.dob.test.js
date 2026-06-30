import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';

import Users from '../../pages/Users';
import adminService from '../../services/adminService';
import staffService from '../../services/staffService';

vi.mock('../../services/adminService');
vi.mock('../../services/staffService');

vi.mock('../../components/AddressMapPicker', () => ({
  default: () => <div data-testid="address-map-picker" />,
}));

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
  },
}));

vi.mock('../../hooks/useResponsive', () => ({
  default: () => ({
    isMobileDevice: false,
    isTabletDevice: false,
    isTouchDevice: false,
    getFontSize: (mobile, _tablet, desktop) => desktop || mobile,
  }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, fallback) => fallback || key }),
}));

vi.mock('../../utils/exportUtils', () => ({
  __esModule: true,
  default: { exportUsers: vi.fn() },
}));

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
      success: vi.fn(),
      error: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
      loading: vi.fn(),
      destroy: vi.fn(),
      open: vi.fn(),
    },
  };
});

const baseUser = {
  id: 5,
  first_name: 'Ann',
  last_name: 'Lee',
  phone: '+998901112233',
  email: '',
  user_type: 'individual',
  status: 'active',
  role: 'customer',
  date_of_birth: '1990-05-17T00:00:00+05:00',
  created_at: '2026-01-01T00:00:00+00:00',
};

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => (
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </BrowserRouter>
  );
};

const setupMocks = () => {
  vi.clearAllMocks();
  adminService.getUsers.mockResolvedValue({
    data: { items: [baseUser] },
    meta: { total: 1, page: 1, per_page: 20 },
  });
  adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [] } });
  adminService.getUserNotificationSettings.mockResolvedValue({
    data: {
      notification_settings: {
        delivery_telegram_status_updates_enabled: true,
        delivery_telegram_status_updates_source: 'default',
        telegram_connected: false,
        bot_active: false,
        updated_at: null,
      },
    },
  });
  adminService.updateUser.mockResolvedValue({ data: { user: { id: 5 } } });
  adminService.createUser.mockResolvedValue({});
  adminService.updateUserStatus.mockResolvedValue({});
  adminService.unlockUserAccount.mockResolvedValue({});
  staffService.getCustomerCodStatement = vi.fn().mockResolvedValue({ data: { data: null } });
  staffService.getCustomerPrepaymentHistory = vi.fn().mockResolvedValue({
    data: { data: { events: [] } },
  });
};

describe('Users page — date_of_birth edit (Deliverable C10)', () => {
  beforeEach(() => {
    setupMocks();
  });

  it('submits date_of_birth as YYYY-MM-DD in the edit payload', async () => {
    const Wrapper = createWrapper();
    render(<Wrapper><Users /></Wrapper>);

    // Wait for the user row to appear (table renders "Ann Lee" as full name)
    await screen.findByText('Ann Lee');

    // Click the Dropdown-rendered "Edit User" menu item button
    const editBtn = await screen.findByRole('button', { name: 'Edit User' });
    editBtn.click();

    // Wait for the modal form to appear and click Save
    const saveBtn = await screen.findByRole('button', { name: 'Save' });
    saveBtn.click();

    await waitFor(() => expect(adminService.updateUser).toHaveBeenCalled());

    const [userId, payload] = adminService.updateUser.mock.calls[0];
    expect(userId).toBe(5);
    // date_of_birth must be serialized as YYYY-MM-DD (not dayjs object, not full ISO)
    expect(payload.date_of_birth).toBe('1990-05-17');
  });
});
