import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';

import Tryouts from '../../pages/Tryouts';
import adminService from '../../services/adminService';
import tryoutService from '../../services/tryoutService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getProducts: vi.fn(),
    getDeliveryPersonnel: vi.fn(),
  },
}));

vi.mock('../../services/tryoutService', () => ({
  __esModule: true,
  default: {
    getTryouts: vi.fn(),
    exportTryouts: vi.fn(),
    convertTryout: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

describe('Tryouts page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getProducts.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getDeliveryPersonnel.mockResolvedValue({ data: { items: [], total: 0 } });
    tryoutService.getTryouts.mockResolvedValue({
      items: [{
        id: 1,
        tryout_number: 'TRY-1001',
        trial_contact: { full_name: 'Jane Doe', phone: '+998901234567' },
        status: 'active',
        outcome: 'pending',
        outstanding_bottles_total: 2,
        pickup_state: 'not_due',
        return_due_at: null,
        can_convert: true,
      }],
      total: 1,
      summary: {},
    });
  });

  const rowOf = async () => (await screen.findByText('TRY-1001')).closest('tr');

  it('draws Convert from the backend\'s can_convert and names the self-approval refusal', async () => {
    render(<Tryouts />, { wrapper: createWrapper() });
    const row = await rowOf();
    fireEvent.click(within(row).getByRole('button', { name: /Convert/ }));

    // The request names the one refusal the page explains itself, so the interceptor stays quiet.
    await waitFor(() => expect(tryoutService.convertTryout).toHaveBeenCalledWith(1, { handledErrorCodes: ['SALES_OUTLET_SELF_APPROVAL'] }));
  });

  it('draws no Convert when the viewer may not convert this try-out', async () => {
    tryoutService.getTryouts.mockResolvedValue({
      items: [{
        id: 1, tryout_number: 'TRY-1001', trial_contact: { full_name: 'Jane Doe', phone: '+998901234567' },
        status: 'active', outcome: 'pending', outstanding_bottles_total: 2, pickup_state: 'not_due', return_due_at: null,
        can_convert: false,
      }],
      total: 1,
      summary: {},
    });
    render(<Tryouts />, { wrapper: createWrapper() });
    const row = await rowOf();

    expect(within(row).queryByRole('button', { name: /Convert/ })).toBeNull();
  });

  it('shows a self-approval race refusal inline, once', async () => {
    tryoutService.convertTryout.mockRejectedValue({
      response: {
        status: 403,
        data: { success: false, message: 'You cannot approve an outlet you onboarded', error_code: 'SALES_OUTLET_SELF_APPROVAL', details: { outlet_id: 5 } },
      },
    });
    render(<Tryouts />, { wrapper: createWrapper() });
    const row = await rowOf();
    fireEvent.click(within(row).getByRole('button', { name: /Convert/ }));

    const alerts = await screen.findAllByTestId('convert-refusal');
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toHaveTextContent('You cannot approve an outlet you onboarded');
  });

  it('renders the page title and a try-out row using the translated (defaultValue) text', async () => {
    render(<Tryouts />, { wrapper: createWrapper() });
    expect(await screen.findByText('Try-outs')).toBeInTheDocument();
    expect(await screen.findByText('TRY-1001')).toBeInTheDocument();
    expect(screen.getAllByText('Actions').length).toBeGreaterThan(0);
  });

  it('a failed CSV export says so itself: a blob request gets no global toast', async () => {
    const errorSpy = vi.spyOn(message, 'error');
    tryoutService.exportTryouts.mockRejectedValue(Object.assign(new Error('Request failed with status code 500'), {
      config: { responseType: 'blob' },
      response: { status: 500 },
    }));
    render(<Tryouts />, { wrapper: createWrapper() });

    fireEvent.click(await screen.findByRole('button', { name: /Export CSV/ }));

    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith('An error occurred'));
    errorSpy.mockRestore();
  });
});
