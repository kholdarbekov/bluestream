import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from 'react-query';
import { message } from 'antd';

import DeliveryReports from '../../pages/DeliveryReports';
import staffService from '../../services/staffService';

jest.mock('../../services/staffService');
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: jest.fn(),
    post: jest.fn(),
    put: jest.fn(),
    delete: jest.fn(),
  },
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
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

const reconciliationPayload = {
  data: {
    data: {
      grand_total_cash: 300000,
      summary: {
        blocked_session_count: 1,
        mismatch_session_count: 1,
        overdue_session_count: 0,
      },
      report: [
        {
          driver_id: 55,
          driver_name: 'Driver One',
          phone: '+998901112233',
          total_cash_collected: 300000,
          blocked_session_count: 1,
          mismatch_session_count: 1,
          overdue_session_count: 0,
        },
      ],
      sessions: [
        {
          id: 101,
          driver_name: 'Driver One',
          business_date: '2026-03-06',
          status: 'submitted',
          expected_cash: 100000,
          expected_cash_on_hand: 100000,
          declared_cash: 100000,
          verified_cash: null,
          declared_variance: 0,
          verified_variance: null,
          blocked_from_cod: false,
        },
        {
          id: 102,
          driver_name: 'Driver Two',
          business_date: '2026-03-06',
          status: 'mismatch',
          expected_cash: 200000,
          expected_cash_on_hand: 200000,
          declared_cash: 150000,
          verified_cash: 150000,
          declared_variance: -50000,
          verified_variance: -50000,
          blocked_from_cod: true,
        },
      ],
    },
  },
};

describe('DeliveryReports page', () => {
  beforeEach(() => {
    jest.clearAllMocks();

    staffService.getCashReconciliation.mockResolvedValue(reconciliationPayload);
    staffService.getCashReconciliationSession.mockResolvedValue({
      data: {
        data: {
          id: 101,
          driver_name: 'Driver One',
          status: 'submitted',
          business_date: '2026-03-06',
          expected_cash: 100000,
          expected_cash_on_hand: 100000,
          declared_cash: 100000,
          verified_cash: null,
          declared_variance: 0,
          verified_variance: null,
          block_reason: null,
          blocked_from_cod: false,
          event_count: 0,
          events: [],
        },
      },
    });
    staffService.verifyCashReconciliationSession.mockResolvedValue({ data: { success: true } });
    staffService.resolveCashReconciliationSession.mockResolvedValue({ data: { success: true } });
    staffService.getCustomerCodStatement.mockResolvedValue({ data: { data: { items: [] } } });
    staffService.getOrderPaymentTimeline.mockResolvedValue({ data: { data: { timeline: [] } } });
  });

  it('renders explicit approve, reject, and resolve actions for reconciliation sessions', async () => {
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(staffService.getCashReconciliation).toHaveBeenCalled();
    });

    expect((await screen.findAllByText('Driver One')).length).toBeGreaterThan(0);
    expect((await screen.findAllByText('Approve')).length).toBeGreaterThan(0);
    expect((await screen.findAllByText('Reject')).length).toBeGreaterThan(0);
    expect((await screen.findAllByText('Resolve')).length).toBeGreaterThan(0);
  });

  it('submits reject flow through the verify endpoint with required notes', async () => {
    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(staffService.getCashReconciliation).toHaveBeenCalled();
    });

    const rejectButtons = await screen.findAllByText('Reject');
    await user.click(rejectButtons[0]);

    expect(await screen.findByText('Reject Reconciliation')).toBeInTheDocument();

    const amountInput = screen.getByRole('spinbutton');
    fireEvent.change(amountInput, { target: { value: '70000' } });

    await user.type(screen.getByRole('textbox'), 'Cash short by 30,000 UZS after count');
    await user.click(screen.getByRole('button', { name: 'OK' }));

    await waitFor(() => {
      expect(staffService.verifyCashReconciliationSession).toHaveBeenCalledTimes(1);
    });

    const [sessionId, payload] = staffService.verifyCashReconciliationSession.mock.calls[0];
    expect(sessionId).toBe(101);
    expect(Number(payload.verified_cash)).toBe(70000);
    expect(payload.reason_code).toBe('cash_count_short');
    expect(payload.notes).toBe('Cash short by 30,000 UZS after count');
    expect(message.success).toHaveBeenCalledWith('Reconciliation rejected and marked as mismatch');
  });
});
