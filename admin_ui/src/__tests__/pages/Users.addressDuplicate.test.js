import React from 'react';
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';

import Users from '../../pages/Users';
import adminService from '../../services/adminService';
import staffService from '../../services/staffService';
import api from '../../services/api';

vi.mock('../../services/adminService');
vi.mock('../../services/staffService');

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
  useTranslation: () => ({ t: (key, fallback) => (typeof fallback === 'string' ? fallback : key) }),
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
            <button key={item.key} disabled={item.disabled} onClick={item.onClick} type="button">
              {typeof item.label === 'string' ? item.label : item.key}
            </button>
          ))}
      </div>
    ),
    message: {
      success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn(),
    },
  };
});

const { message, Modal } = await import('antd');

const baseUser = {
  id: 7, first_name: 'Ann', last_name: 'Lee', phone: '+998901112233', email: '', user_type: 'individual',
  status: 'active', role: 'customer', created_at: '2026-01-01T00:00:00+00:00',
};
const SAVED = {
  id: 31, title: 'Home', full_address: 'Chilonzor 5', district: '', street_address: '', floor_number: '',
  apartment_number: '', landmark: '', delivery_instructions: '', latitude: 41.3111, longitude: 69.2797,
  is_default: true, is_business: false,
};
// A 409 candidate is the saved row (`UserAddress.to_dict`) plus `distance_m`.
const CANDIDATE = { ...SAVED, display_label: 'Home', distance_m: 18 };
// Untitled: its display_label already is its street line.
const UNTITLED = {
  ...SAVED, id: 32, title: null, display_label: 'Chilonzor 12-kv', full_address: 'Chilonzor 12-kv', is_default: false, distance_m: 14,
};

const duplicateError = (candidates = [CANDIDATE]) => Object.assign(new Error('Request failed with status code 409'), {
  response: {
    status: 409,
    data: {
      success: false, message: 'x', error_code: 'ADDRESS_POSSIBLE_DUPLICATE',
      details: { move_address_id: null, candidates },
    },
  },
});

const Wrapper = ({ children }) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </BrowserRouter>
  );
};

// antd prints a confirm's title twice under vitest (modal title + confirm title): the dialog is found by the confirm one.
const duplicateDialog = async () => (await screen.findByText('Possible duplicate address', { selector: '.ant-modal-confirm-title' })).closest('.ant-modal');

const openUser = async () => {
  render(<Wrapper><Users /></Wrapper>);
  await screen.findByText('Ann Lee');
  fireEvent.click(await screen.findByRole('button', { name: 'ui.users.view_details' }));
};

const openCreate = async () => {
  await openUser();
  fireEvent.click(await screen.findByRole('button', { name: /Add Address/ }));
  fireEvent.change(await screen.findByPlaceholderText('Enter full delivery address...'), {
    target: { value: 'Chilonzor 5' },
  });
  fireEvent.click(await screen.findByRole('button', { name: 'Add' }));
};

const openEdit = async () => {
  await openUser();
  const editIcon = await screen.findByRole('button', { name: 'edit' });
  fireEvent.click(editIcon);
  fireEvent.click(await screen.findByRole('button', { name: 'Save' }));
};

describe('Users page: possible-duplicate address dialog', () => {
  afterEach(() => {
    Modal.destroyAll();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    adminService.createUserAddress.mockReset();
    adminService.updateUserAddress.mockReset();
    adminService.getUsers.mockResolvedValue({ data: { items: [baseUser] }, meta: { total: 1, page: 1, per_page: 20 } });
    adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [SAVED] } });
    adminService.getUserNotificationSettings.mockResolvedValue({
      data: { notification_settings: { delivery_telegram_status_updates_enabled: true, delivery_telegram_status_updates_source: 'default', telegram_connected: false, bot_active: false, updated_at: null } },
    });
    api.get.mockResolvedValue({ data: { success: true, data: { districts: [] } } });
    staffService.getCustomerCodStatement = vi.fn().mockResolvedValue({ data: { data: null } });
    staffService.getCustomerPrepaymentHistory = vi.fn().mockResolvedValue({ data: { data: { events: [] } } });
  });

  it('a 409 on create opens the dialog with the candidate; OK posts the same body plus confirm_duplicate: true; no toast', async () => {
    adminService.createUserAddress.mockRejectedValueOnce(duplicateError()).mockResolvedValueOnce({});
    await openCreate();

    const dialog = await duplicateDialog();
    expect(within(dialog).getByText('This customer already has an address close to this pin:')).toBeInTheDocument();
    expect(within(dialog).getByText(/Home/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Chilonzor 5/)).toBeInTheDocument();
    expect(within(dialog).getByText(/~18 m/)).toBeInTheDocument();
    expect(message.error).not.toHaveBeenCalled();

    const firstBody = adminService.createUserAddress.mock.calls[0][1];
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create anyway' }));
    await waitFor(() => expect(adminService.createUserAddress).toHaveBeenCalledTimes(2));
    expect(adminService.createUserAddress).toHaveBeenLastCalledWith(7, { ...firstBody, confirm_duplicate: true });
    expect(firstBody.confirm_duplicate).toBeUndefined();
    expect(message.error).not.toHaveBeenCalled();
  });

  it('names each candidate once: the street line follows the label only when the address has a title', async () => {
    adminService.createUserAddress.mockRejectedValueOnce(duplicateError([UNTITLED, CANDIDATE]));
    await openCreate();

    const dialog = await duplicateDialog();
    expect(within(dialog).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Chilonzor 12-kv · ~14 m',
      'Home · Chilonzor 5 · ~18 m',
    ]);
  });

  it('a 409 on edit offers "Save anyway" and re-sends with confirm_duplicate: true', async () => {
    adminService.updateUserAddress.mockRejectedValueOnce(duplicateError()).mockResolvedValueOnce({});
    await openEdit();

    const dialog = await duplicateDialog();
    const firstBody = adminService.updateUserAddress.mock.calls[0][2];
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save anyway' }));
    await waitFor(() => expect(adminService.updateUserAddress).toHaveBeenCalledTimes(2));
    expect(adminService.updateUserAddress).toHaveBeenLastCalledWith(7, 31, { ...firstBody, confirm_duplicate: true });
    expect(message.error).not.toHaveBeenCalled();
  });

  it('Cancel keeps the address form open and sends nothing more', async () => {
    adminService.createUserAddress.mockRejectedValueOnce(duplicateError());
    await openCreate();

    const dialog = await duplicateDialog();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByText('Possible duplicate address', { selector: '.ant-modal-confirm-title' })).toBeNull());
    expect(screen.getByPlaceholderText('Enter full delivery address...')).toHaveValue('Chilonzor 5');
    expect(adminService.createUserAddress).toHaveBeenCalledTimes(1);
  });

  it('any other failure still shows the toast and no dialog', async () => {
    adminService.createUserAddress.mockRejectedValueOnce(Object.assign(new Error('boom'), {
      response: { status: 400, data: { message: 'Outside the zone', error_code: 'ADDRESS_OUT_OF_ZONE' } },
    }));
    await openCreate();
    await waitFor(() => expect(message.error).toHaveBeenCalledWith('Outside the zone'));
    expect(screen.queryByText('Possible duplicate address', { selector: '.ant-modal-confirm-title' })).toBeNull();
  });
});
