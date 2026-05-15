import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';

import DeliveryReports from '../../pages/DeliveryReports';
import staffService from '../../services/staffService';

vi.mock('../../services/staffService');
vi.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback) => fallback || key,
  }),
}));

vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  const MockSelect = ({
    children,
    value,
    defaultValue,
    onChange,
    disabled,
    id,
    name,
    className,
    style,
    'aria-label': ariaLabel,
  }) => (
    <select
      id={id}
      name={name}
      className={className}
      style={style}
      aria-label={ariaLabel}
      disabled={disabled}
      value={value ?? defaultValue ?? ''}
      onChange={(event) => onChange?.(event.target.value)}
    >
      {children}
    </select>
  );
  MockSelect.Option = ({ value: optionValue, children }) => (
    <option value={optionValue}>{children}</option>
  );

  return {
    ...actual,
    Select: MockSelect,
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
          session_started_at: '2026-03-06T08:00:00Z',
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
          session_started_at: '2026-03-06T09:00:00Z',
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
    vi.clearAllMocks();

    staffService.getCashReconciliation.mockResolvedValue(reconciliationPayload);
    staffService.getCashReconciliationSession.mockResolvedValue({
      data: {
        data: {
          id: 101,
          driver_name: 'Driver One',
          status: 'submitted',
          session_started_at: '2026-03-06T08:00:00Z',
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
    staffService.getCustomerCodStatement.mockResolvedValue({
      data: {
        data: {
          active_cod_debt_count: 0,
          total_outstanding_amount: 18000,
          items: [
            {
              order_id: 456,
              order_number: 'ORD-TEST-456',
              outstanding_amount: 18000,
            },
          ],
        },
      },
    });
    staffService.getOrderPaymentTimeline.mockResolvedValue({ data: { data: { timeline: [] } } });
    staffService.getCodCollectionUsersWithOpenDebts.mockResolvedValue({
      data: {
        data: {
          items: [
            {
              id: 77,
              first_name: 'Ali',
              last_name: 'Buyer',
              phone: '+998901234500',
              active_cod_debt_count: 0,
            },
          ],
        },
      },
    });
    staffService.searchCodCollectionUsers.mockResolvedValue({
      data: {
        data: {
          items: [
            {
              id: 77,
              first_name: 'Ali',
              last_name: 'Buyer',
              phone: '+998901234500',
              active_cod_debt_count: 0,
            },
          ],
        },
      },
    });
    staffService.getDeliveryPersons.mockResolvedValue({ data: { data: { items: [] } } });
    staffService.recordCashCollection.mockResolvedValue({ data: { success: true } });
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

  it('records personal card transfer with required target order', async () => {
    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(staffService.getCashReconciliation).toHaveBeenCalled();
    });

    const recordCollectionButton = (await screen.findAllByRole('button')).find((button) =>
      /Record Collection|staff:record_cash_collection/i.test(button.textContent || '')
    );
    expect(recordCollectionButton).toBeTruthy();
    await user.click(recordCollectionButton);

    await user.selectOptions(screen.getByLabelText('Customer'), '77');
    await user.selectOptions(screen.getByLabelText('Collection Type'), 'personal_card_transfer');
    await user.selectOptions(screen.getByLabelText('Target Order'), '456');

    const amountInput = screen.getByRole('spinbutton');
    fireEvent.change(amountInput, { target: { value: '12000' } });
    await user.type(screen.getByLabelText('Notes'), 'Customer paid to owner personal card');
    await user.click(screen.getByRole('button', { name: 'OK' }));

    await waitFor(() => {
      expect(staffService.recordCashCollection).toHaveBeenCalledTimes(1);
    });

    const payload = staffService.recordCashCollection.mock.calls[0][0];
    expect(payload.source).toBe('personal_card_transfer');
    expect(Number(payload.order_id)).toBe(456);
    expect(Number(payload.customer_id)).toBe(77);
    expect(payload.collector_user_id).toBeNull();
  });
});
