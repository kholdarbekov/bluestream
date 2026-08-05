import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';

import LinkedAccountsPanel from './LinkedAccountsPanel';
import adminService from '../services/adminService';

vi.mock('../services/adminService');

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, fallback) => fallback || key }),
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
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

const USER = { id: 11, first_name: 'Alice', last_name: 'Tester', phone: '+998900000001' };

describe('LinkedAccountsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    adminService.getLinkedAccounts.mockResolvedValue({
      data: {
        canonical_customer_id: 5,
        primary_user_id: 11,
        members: [
          { id: 11, first_name: 'Alice', last_name: 'Tester', phone: '+998900000001' },
          { id: 12, first_name: 'Bob', last_name: 'Second', phone: '+998900000002' },
        ],
      },
    });

    adminService.getLinkSuggestions.mockResolvedValue({
      data: {
        suggestions: [
          {
            user_id: 21,
            first_name: 'Carl',
            last_name: 'Candidate',
            phone: '+998900000099',
            min_distance_km: 0.05,
            shared_geo_customer_count: 1,
            score: 0.9,
          },
        ],
      },
    });

    adminService.getUsers.mockResolvedValue({ data: { items: [] }, meta: { total: 0 } });

    adminService.linkAccounts.mockResolvedValue({ data: { canonical_customer_id: 5 } });
    adminService.unlinkAccount.mockResolvedValue({ data: { non_terminal_orders: [] } });
    adminService.dismissCustomerLink.mockResolvedValue({ data: {} });
  });

  it('renders nothing meaningful without a user id', () => {
    const { container } = render(<LinkedAccountsPanel user={null} />, { wrapper: createWrapper() });
    expect(container.textContent).toBe('');
  });

  it('renders cluster members with the primary tagged', async () => {
    render(<LinkedAccountsPanel user={USER} />, { wrapper: createWrapper() });

    expect(await screen.findByText(/Bob Second/)).toBeInTheDocument();
    expect(screen.getByText(/Alice Tester/)).toBeInTheDocument();
    expect(screen.getByText('primary')).toBeInTheDocument();
  });

  it('links a suggestion using the typed reason (exact payload)', async () => {
    const user = userEvent.setup();
    render(<LinkedAccountsPanel user={USER} />, { wrapper: createWrapper() });

    expect(await screen.findByText(/Carl Candidate/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Link' }));

    const dialog = await screen.findByRole('dialog');
    const textarea = within(dialog).getByRole('textbox');
    await user.type(textarea, 'Confirmed same person via phone call');
    await user.click(within(dialog).getByRole('button', { name: /confirm/i }));

    await waitFor(() => {
      expect(adminService.linkAccounts).toHaveBeenCalledWith(
        11,
        21,
        'Confirmed same person via phone call'
      );
    });
  });

  it('dismisses a suggestion as not the same person (exact ids)', async () => {
    const user = userEvent.setup();
    render(<LinkedAccountsPanel user={USER} />, { wrapper: createWrapper() });

    expect(await screen.findByText(/Carl Candidate/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /not the same person/i }));

    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /confirm/i }));

    await waitFor(() => {
      expect(adminService.dismissCustomerLink).toHaveBeenCalledWith(11, 21);
    });
  });

  // "Same physical place" grouping moved out of this panel in Phase 2c: place
  // groups are ownerless and may span customers, so they are not a property of
  // an identity cluster. That lifecycle is covered by PlaceGroupPanel.test.jsx.
  it('does not offer place grouping (that is PlaceGroupPanel\'s job)', async () => {
    render(<LinkedAccountsPanel user={USER} />, { wrapper: createWrapper() });

    expect(await screen.findByText(/Bob Second/)).toBeInTheDocument();
    expect(screen.queryByText(/same place/i)).not.toBeInTheDocument();
    expect(adminService.getUserAddresses).not.toHaveBeenCalled();
  });

  it('unlink calls unlinkAccount with the member id and the typed reason', async () => {
    const user = userEvent.setup();
    adminService.unlinkAccount.mockResolvedValue({
      data: {
        canonical_customer_id: 5,
        remaining_member_ids: [11],
        new_primary_user_id: 11,
        non_terminal_orders: [],
      },
    });

    render(<LinkedAccountsPanel user={USER} />, { wrapper: createWrapper() });

    const bobRow = (await screen.findByText(/Bob Second/)).closest('li');
    await user.click(within(bobRow).getByRole('button', { name: 'Unlink' }));

    const dialog = await screen.findByRole('dialog');
    const textarea = within(dialog).getByRole('textbox');
    await user.type(textarea, 'Confirmed duplicate account via support call');
    await user.click(within(dialog).getByRole('button', { name: /confirm/i }));

    await waitFor(() => {
      expect(adminService.unlinkAccount).toHaveBeenCalledWith(
        12,
        'Confirmed duplicate account via support call'
      );
    });
  });

  it('unlink warns when there are in-flight orders', async () => {
    const user = userEvent.setup();
    adminService.unlinkAccount.mockResolvedValue({
      data: {
        canonical_customer_id: 5,
        remaining_member_ids: [11],
        new_primary_user_id: 11,
        non_terminal_orders: [
          { order_id: 900, order_number: 'ORD-900', user_id: 12, status: 'confirmed' },
        ],
      },
    });

    render(<LinkedAccountsPanel user={USER} />, { wrapper: createWrapper() });

    const bobRow = (await screen.findByText(/Bob Second/)).closest('li');
    await user.click(within(bobRow).getByRole('button', { name: 'Unlink' }));

    const dialog = await screen.findByRole('dialog');
    const textarea = within(dialog).getByRole('textbox');
    await user.type(textarea, 'Splitting accidental merge, has an active order');
    await user.click(within(dialog).getByRole('button', { name: /confirm/i }));

    await waitFor(() => {
      expect(adminService.unlinkAccount).toHaveBeenCalledWith(
        12,
        'Splitting accidental merge, has an active order'
      );
    });

    await waitFor(() => {
      expect(message.warning).toHaveBeenCalled();
    });
  });
});
