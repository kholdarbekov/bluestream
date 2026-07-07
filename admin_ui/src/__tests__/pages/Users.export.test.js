import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { message } from 'antd';

import Users from '../../pages/Users';
import adminService from '../../services/adminService';
import staffService from '../../services/staffService';
import exportUtils from '../../utils/exportUtils';

vi.mock('../../services/adminService');
vi.mock('../../services/staffService');
vi.mock('../../utils/exportUtils');

vi.mock('../../components/AddressMapPicker', () => ({
  default: () => <div data-testid="address-map-picker" />,
}));

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn() },
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
  useTranslation: () => ({
    t: (key, fallback) => fallback || key,
  }),
}));

vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
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

describe('Users page export button', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getUsers.mockResolvedValue({
      data: { items: [] },
      meta: { total: 0, page: 1, per_page: 20 },
    });
    exportUtils.exportUsers.mockResolvedValue({ success: true, message: 'Users exported successfully' });
  });

  it('calls exportUtils.exportUsers with the current search/status/registration filters', async () => {
    const user = userEvent.setup();
    render(<Users />, { wrapper: createWrapper() });

    await waitFor(() => expect(adminService.getUsers).toHaveBeenCalled());

    await user.click(screen.getByText('ui.users.export'));

    await waitFor(() => {
      expect(exportUtils.exportUsers).toHaveBeenCalledWith({
        search: '',
        status: '',
        registration_method: '',
      });
    });
    expect(exportUtils.exportUsers).toHaveBeenCalledTimes(1);
  });

  it('shows an error message when the export fails', async () => {
    const user = userEvent.setup();
    exportUtils.exportUsers.mockResolvedValue({ success: false, message: 'Failed to export users' });

    render(<Users />, { wrapper: createWrapper() });
    await waitFor(() => expect(adminService.getUsers).toHaveBeenCalled());

    await user.click(screen.getByText('ui.users.export'));

    await waitFor(() => {
      expect(message.error).toHaveBeenCalledWith('Failed to export users');
    });
  });
});
