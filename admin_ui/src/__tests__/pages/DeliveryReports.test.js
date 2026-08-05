import React from 'react';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';

import DeliveryReports from '../../pages/DeliveryReports';
import staffService from '../../services/staffService';

vi.mock('../../services/staffService');
vi.mock('../../components/common/PermissionGuard', async () => {
  const actual = await vi.importActual('../../components/common/PermissionGuard');
  return {
    ...actual,
    usePermissions: vi.fn(() => ({
      getUserRole: () => 'admin',
      isAdmin: () => true,
      isManager: () => false,
      isOperator: () => false,
      hasPermission: () => true,
    })),
  };
});
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
    // Interpolates `{{var}}` like real i18next does. Without this the summary
    // alert renders its template literally and the FIGURE THE ADMIN SEES — the
    // half of the collect decision that was wrong — cannot be asserted at all.
    t: (key, fallback, vars) => {
      const template = fallback || key;
      if (!vars) return template;
      const values = new Map(Object.entries(vars));
      return String(template).replace(/{{\s*(\w+)\s*}}/g, (match, name) => (
        values.has(name) ? String(values.get(name)) : match
      ));
    },
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
          session_ended_at: '2026-03-06T11:30:00Z',
          expected_cash: 100000,
          expected_cash_on_hand: 100000,
          declared_cash: 100000,
          verified_cash: null,
          declared_variance: 0,
          verified_variance: null,
          block_reason: null,
          blocked_from_cod: false,
          event_count: 1,
          events: [
            {
              id: 9001,
              occurred_at: '2026-03-06T09:00:00Z',
              source: 'standalone_meeting',
              amount: 30000,
              notes: 'part',
              customer_id: 77,
              customer_name: 'Ali Buyer',
              customer_phone: '+998901234500',
              order_id: null,
              order_number: null,
              allocations: [
                {
                  order_id: 456,
                  order_number: 'ORD-TEST-456',
                  allocated_amount: 30000,
                  allocation_mode: 'auto',
                  reversed: false,
                  payment_status: 'partially_paid',
                  payment_outstanding_amount: 15000,
                  settlement: 'partial',
                },
              ],
            },
          ],
        },
      },
    });
    staffService.verifyCashReconciliationSession.mockResolvedValue({ data: { success: true } });
    staffService.resolveCashReconciliationSession.mockResolvedValue({ data: { success: true } });
    staffService.forceCloseCashReconciliationSession.mockResolvedValue({ data: { data: {} } });
    staffService.getCustomerCodStatement.mockResolvedValue({
      data: {
        data: {
          first_name: 'Ali',
          last_name: 'Buyer',
          phone: '+998901234500',
          active_cod_debt_count: 0,
          total_outstanding_amount: 18000,
          items: [
            { order_id: 456, order_number: 'ORD-TEST-456', outstanding_amount: 18000, payment_id: 5 },
          ],
        },
      },
    });
    staffService.getOrderPaymentTimeline.mockResolvedValue({
      data: {
        data: {
          order_number: 'ORD-TEST-456',
          status: 'partially_paid',
          amount: 30000,
          amount_collected: 15000,
          outstanding_amount: 15000,
          payment_id: 5,
          customer_id: 77,
          customer_name: 'Ali Buyer',
          customer_phone: '+998901234500',
          timeline: [],
        },
      },
    });
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

  it('force-closes a stuck active session with the exact payload', async () => {
    staffService.getCashReconciliation.mockResolvedValue({
      data: {
        data: {
          grand_total_cash: 1000,
          summary: { blocked_session_count: 0, mismatch_session_count: 0, overdue_session_count: 0 },
          report: [
            {
              driver_id: 55,
              driver_name: 'Stuck Driver',
              phone: '+998901112233',
              total_cash_collected: 1000,
              blocked_session_count: 0,
              mismatch_session_count: 0,
              overdue_session_count: 0,
            },
          ],
          sessions: [
            {
              id: 202,
              driver_name: 'Stuck Driver',
              session_started_at: '2026-06-23T08:00:00Z',
              status: 'partial',
              expected_cash: 0,
              expected_cash_on_hand: 0,
              declared_cash: 1000,
              verified_cash: null,
              declared_variance: 1000,
              verified_variance: null,
              blocked_from_cod: false,
            },
          ],
        },
      },
    });

    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(staffService.getCashReconciliation).toHaveBeenCalled();
    });

    await user.click(await screen.findByText('Force Close'));

    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByRole('textbox'), 'Driver left; closing session');
    await user.click(within(dialog).getByRole('button', { name: /force close/i }));

    await waitFor(() => {
      expect(staffService.forceCloseCashReconciliationSession).toHaveBeenCalledTimes(1);
    });
    expect(staffService.forceCloseCashReconciliationSession).toHaveBeenCalledWith(202, {
      reason: 'Driver left; closing session',
      verified_cash: undefined,
    });
  });

  it('renders session timestamps in Tashkent time (DD-MM-YYYY HH:mm:ss)', async () => {
    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => expect(staffService.getCashReconciliation).toHaveBeenCalled());

    const viewButtons = await screen.findAllByText('View');
    await user.click(viewButtons[0]);

    // Scoped to the modal: the session-list table (behind the modal) renders the
    // same session's session_started_at with the same formatter, so an unscoped
    // screen.findByText would match both the background table row and the modal.
    const dialog = await screen.findByRole('dialog');
    // 2026-03-06T08:00:00Z + 5h -> 06-03-2026 13:00:00
    expect(await within(dialog).findByText('06-03-2026 13:00:00')).toBeInTheDocument();
    // 2026-03-06T11:30:00Z + 5h -> 06-03-2026 16:30:00
    expect(within(dialog).getByText('06-03-2026 16:30:00')).toBeInTheDocument();
  });

  it('shows the customer on each collection event and a settlement breakdown on expand', async () => {
    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => expect(staffService.getCashReconciliation).toHaveBeenCalled());
    await user.click((await screen.findAllByText('View'))[0]);

    // Customer column shows name + phone
    expect(await screen.findByText('Ali Buyer')).toBeInTheDocument();
    expect(screen.getByText('+998901234500')).toBeInTheDocument();

    // Expand the event row -> settlement breakdown appears
    const expandButton = document.querySelector('.ant-table-row-expand-icon');
    expect(expandButton).toBeTruthy();
    await user.click(expandButton);

    expect(await screen.findByText('ORD-TEST-456')).toBeInTheDocument();
    expect(screen.getByText(/Partially paid/)).toBeInTheDocument();
    expect(screen.getByText('Payment Timeline')).toBeInTheDocument();
  });

  it('renders a Reversed tag for a reversed settlement allocation', async () => {
    staffService.getCashReconciliationSession.mockResolvedValue({
      data: {
        data: {
          id: 101,
          driver_name: 'Driver One',
          status: 'submitted',
          session_started_at: '2026-03-06T08:00:00Z',
          session_ended_at: '2026-03-06T11:30:00Z',
          expected_cash: 100000,
          expected_cash_on_hand: 100000,
          declared_cash: 100000,
          verified_cash: null,
          declared_variance: 0,
          verified_variance: null,
          block_reason: null,
          blocked_from_cod: false,
          event_count: 1,
          events: [
            {
              id: 9002,
              occurred_at: '2026-03-06T09:00:00Z',
              source: 'standalone_meeting',
              amount: 12000,
              notes: 'reversed part',
              customer_id: 77,
              customer_name: 'Ali Buyer',
              customer_phone: '+998901234500',
              order_id: null,
              order_number: null,
              allocations: [
                {
                  order_id: 999,
                  order_number: 'ORD-REV-999',
                  allocated_amount: 12000,
                  allocation_mode: 'auto',
                  reversed: true,
                  payment_status: 'partially_paid',
                  payment_outstanding_amount: 12000,
                  settlement: 'partial',
                },
              ],
            },
          ],
        },
      },
    });

    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => expect(staffService.getCashReconciliation).toHaveBeenCalled());
    await user.click((await screen.findAllByText('View'))[0]);

    // Expand the event row -> settlement breakdown appears
    const expandButton = document.querySelector('.ant-table-row-expand-icon');
    expect(expandButton).toBeTruthy();
    await user.click(expandButton);

    expect(await screen.findByText('ORD-REV-999')).toBeInTheDocument();
    expect(await screen.findByText('Reversed')).toBeInTheDocument();
  });

  it('labels a place-scoped collection event and shows the payer → beneficiary stamps', async () => {
    staffService.getCashReconciliationSession.mockResolvedValue({
      data: {
        data: {
          id: 101,
          driver_name: 'Driver One',
          status: 'submitted',
          session_started_at: '2026-03-06T08:00:00Z',
          session_ended_at: '2026-03-06T11:30:00Z',
          expected_cash: 100000,
          expected_cash_on_hand: 100000,
          declared_cash: 100000,
          verified_cash: null,
          declared_variance: 0,
          verified_variance: null,
          block_reason: null,
          blocked_from_cod: false,
          event_count: 1,
          events: [
            {
              id: 9003,
              occurred_at: '2026-03-06T09:00:00Z',
              source: 'delivery',
              amount: 60000,
              notes: null,
              customer_id: 7,
              customer_name: 'Ali Buyer',
              customer_phone: '+998901234500',
              order_id: null,
              order_number: null,
              scope_type: 'place',
              scope_group_id: 3,
              scope_group_label: 'Acme office',
              allocations: [
                {
                  order_id: 456,
                  order_number: 'ORD-PLACE-456',
                  allocated_amount: 60000,
                  allocation_mode: 'auto',
                  reversed: false,
                  payment_status: 'completed',
                  payment_outstanding_amount: 0,
                  settlement: 'fully',
                  source_customer_id: 7,
                  beneficiary_user_id: 9,
                },
              ],
            },
          ],
        },
      },
    });

    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => expect(staffService.getCashReconciliation).toHaveBeenCalled());
    await user.click((await screen.findAllByText('View'))[0]);

    // Event row carries the place label — a workplace collection must not read
    // as personal cash.
    expect(await screen.findByText(/Acme office/)).toBeInTheDocument();

    const expandButton = document.querySelector('.ant-table-row-expand-icon');
    await user.click(expandButton);

    expect(await screen.findByText('ORD-PLACE-456')).toBeInTheDocument();
    expect(screen.getByText(/#7 → #9/)).toBeInTheDocument();
  });

  it('leaves personal collection events unlabelled (unlinked baseline)', async () => {
    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => expect(staffService.getCashReconciliation).toHaveBeenCalled());
    await user.click((await screen.findAllByText('View'))[0]);

    await screen.findByText('Ali Buyer');
    expect(screen.queryByText(/Place collection/)).toBeNull();
    expect(screen.queryByText(/Linked-accounts collection/)).toBeNull();
    expect(screen.queryByText(/→ #/)).toBeNull();
  });

  it('shows customer identity in the Customer Statement and Payment Timeline modals', async () => {
    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => expect(staffService.getCashReconciliation).toHaveBeenCalled());
    await user.click((await screen.findAllByText('View'))[0]);

    // Customer Statement (event Actions button; event has customer_id 77)
    await user.click(await screen.findByText('Customer Statement'));
    await screen.findByText('Customer COD Statement');
    const statementDialog = screen
      .getAllByRole('dialog')
      .find((d) => within(d).queryByText('Customer COD Statement'));
    expect(statementDialog).toBeTruthy();
    expect(within(statementDialog).getByText(/\+998901234500/)).toBeInTheDocument();
    expect(within(statementDialog).getByText('Ali Buyer')).toBeInTheDocument();

    // Payment Timeline (expand the event row, click the per-order link)
    await user.click(document.querySelector('.ant-table-row-expand-icon'));
    await user.click(await screen.findByText('Payment Timeline'));
    await screen.findByText('Order Payment Timeline');
    const timelineDialog = screen
      .getAllByRole('dialog')
      .find((d) => within(d).queryByText('Order Payment Timeline'));
    expect(timelineDialog).toBeTruthy();
    expect(within(timelineDialog).getByText('Ali Buyer')).toBeInTheDocument();
    expect(within(timelineDialog).getByText(/\+998901234500/)).toBeInTheDocument();
  });

  // ── Plan E: the place address is DERIVED, and no per-person debt gate ──────
  //
  // A statement for a coworker who personally owes NOTHING but whose workplace
  // owes 40 000. `active_cod_debt_count: 0` is exactly the state the old
  // per-person submit guard refused, and exactly the person a driver or admin
  // most often collects the office's cash from.
  const statementWithPlaces = (places, collectScope) => ({
    data: {
      data: {
        first_name: 'Ali',
        last_name: 'Buyer',
        phone: '+998901234500',
        active_cod_debt_count: 0,
        account_active_cod_debt_count: 0,
        total_outstanding_amount: 0,
        cluster_member_count: 1,
        cluster_delivered_outstanding_amount: 0,
        places,
        items: [],
        collect_scope: collectScope,
      },
    },
  });

  // 🔴 `collect_scope` is the ONE object the modal both displays and posts —
  // `StaffService.get_customer_cod_statement_for_admin` resolves it server-side
  // with the same calculation the driver's debtor row and the staff bot's
  // collect ceiling use. Note how far the place figure is from the per-account
  // one on THIS statement: Ali owes 0 personally and his workplace owes 40 000.
  // The old modal rendered the 0 and posted the place address anyway.
  const PLACE_SCOPE = {
    scope_type: 'place',
    delivery_address_id: 44,
    amount: 40000,
    debt_count: 1,
    cluster_amount: 0,
    cluster_debt_count: 0,
  };

  const CLUSTER_SCOPE = {
    scope_type: 'cluster',
    delivery_address_id: null,
    amount: 0,
    debt_count: 0,
    cluster_amount: 0,
    cluster_debt_count: 0,
  };

  const ONE_PLACE = [
    {
      address_id: 44,
      place_group_id: 9,
      label: 'Acme office',
      place_open_cod_debt_total: 40000,
      place_active_cod_debt_count: 1,
    },
  ];

  const TWO_PLACES = [
    ...ONE_PLACE,
    {
      address_id: 45,
      place_group_id: 10,
      label: 'Beta office',
      place_open_cod_debt_total: 15000,
      place_active_cod_debt_count: 1,
    },
  ];

  const openRecordCollectionModal = async (user) => {
    const recordCollectionButton = (await screen.findAllByRole('button')).find((button) =>
      /Record Collection|staff:record_cash_collection/i.test(button.textContent || '')
    );
    expect(recordCollectionButton).toBeTruthy();
    await user.click(recordCollectionButton);
  };

  const fillAndSubmitCollection = async (user, source) => {
    await user.selectOptions(screen.getByLabelText('Customer'), '77');
    await user.selectOptions(screen.getByLabelText('Collection Type'), source);

    const amountInput = screen.getByRole('spinbutton');
    fireEvent.change(amountInput, { target: { value: '40000' } });
    await user.type(screen.getByLabelText('Notes'), 'Office cash');
    await user.click(screen.getByRole('button', { name: 'OK' }));
  };

  it('submits a standalone collection for a customer with no personal COD debt', async () => {
    // Plan E R1: a coworker holding the office's cash has active_cod_debt_count
    // 0. The old per-person submit guard blocked the admin before the request
    // was even built. Deleting it is the fix; this is its regression pin.
    staffService.getCustomerCodStatement.mockResolvedValue(
      statementWithPlaces(ONE_PLACE, PLACE_SCOPE)
    );

    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(staffService.getCashReconciliation).toHaveBeenCalled();
    });

    await openRecordCollectionModal(user);
    await fillAndSubmitCollection(user, 'standalone_meeting');

    // Assert the mutation fired at all — under the old guard it never did.
    await waitFor(() => {
      expect(staffService.recordCashCollection).toHaveBeenCalledTimes(1);
    });
  });

  it('sends the single grouped place address with a standalone collection', async () => {
    // Same stub as above. The derived address is the customer's ONE grouped
    // place; the admin is never asked to pick it (plan E9).
    staffService.getCustomerCodStatement.mockResolvedValue(
      statementWithPlaces(ONE_PLACE, PLACE_SCOPE)
    );

    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(staffService.getCashReconciliation).toHaveBeenCalled();
    });

    await openRecordCollectionModal(user);
    await fillAndSubmitCollection(user, 'standalone_meeting');

    await waitFor(() => {
      expect(staffService.recordCashCollection).toHaveBeenCalledTimes(1);
    });

    const payload = staffService.recordCashCollection.mock.calls[0][0];
    expect(Number(payload.customer_id)).toBe(77);
    expect(Number(payload.amount)).toBe(40000);
    expect(payload.source).toBe('standalone_meeting');
    expect(payload.delivery_address_id).toBe(44);
  });

  it('sends no place address when the customer belongs to two places', async () => {
    // Two entries in `places` => ambiguous. Guessing would spread the cash over
    // the wrong workplace, so we send null and the backend keeps cluster scope
    // (plan E7, mirroring the staff bot's _resolve_scope_address_id).
    // The backend resolves the ambiguity, not the UI: two places => it
    // publishes cluster scope with no address.
    staffService.getCustomerCodStatement.mockResolvedValue(
      statementWithPlaces(TWO_PLACES, CLUSTER_SCOPE)
    );

    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(staffService.getCashReconciliation).toHaveBeenCalled();
    });

    await openRecordCollectionModal(user);
    await fillAndSubmitCollection(user, 'standalone_meeting');

    await waitFor(() => {
      expect(staffService.recordCashCollection).toHaveBeenCalledTimes(1);
    });

    expect(staffService.recordCashCollection.mock.calls[0][0].delivery_address_id).toBeNull();
  });

  it('sends no place address for an admin correction', async () => {
    // C5.3: admin_adjustment / backfill / personal_card_transfer can never be
    // place-scoped, so the field must not be sent for them. The stub still
    // publishes a place scope, so a missing source check would show up as 44.
    staffService.getCustomerCodStatement.mockResolvedValue(
      statementWithPlaces(ONE_PLACE, PLACE_SCOPE)
    );

    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(staffService.getCashReconciliation).toHaveBeenCalled();
    });

    await openRecordCollectionModal(user);
    await fillAndSubmitCollection(user, 'admin_adjustment');

    await waitFor(() => {
      expect(staffService.recordCashCollection).toHaveBeenCalledTimes(1);
    });

    expect(staffService.recordCashCollection.mock.calls[0][0].delivery_address_id).toBeNull();
  });

  // ── 🔴 THE ADMIN SPLIT: the figure SHOWN and the scope POSTED are ONE ──────
  //
  // The modal posted `places[0].address_id` — PLACE scope, settling the whole
  // workplace — while rendering the raw per-account `total_outstanding_amount`.
  // Measured on real rows: shown 25 000, true ceiling 45 000; the admin
  // collected the 25 000 they were shown, the customer was still 10 000 down
  // and 10 000 of a COWORKER'S debt had been paid. Each test below asserts BOTH
  // halves in one act, so neither can be verified in isolation again.

  const selectCustomerAndSource = async (user, source) => {
    await user.selectOptions(screen.getByLabelText('Customer'), '77');
    await user.selectOptions(screen.getByLabelText('Collection Type'), source);
  };

  const submitCollection = async (user) => {
    const amountInput = screen.getByRole('spinbutton');
    fireEvent.change(amountInput, { target: { value: '40000' } });
    await user.type(screen.getByLabelText('Notes'), 'Office cash');
    await user.click(screen.getByRole('button', { name: 'OK' }));
  };

  it('shows the figure its own submit settles, not the raw account total', async () => {
    // Ali owes 0 personally; his workplace owes 40 000 and a place-scoped
    // collection from him settles all of it. The old alert rendered the 0.
    staffService.getCustomerCodStatement.mockResolvedValue(
      statementWithPlaces(ONE_PLACE, PLACE_SCOPE)
    );

    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(staffService.getCashReconciliation).toHaveBeenCalled();
    });

    await openRecordCollectionModal(user);
    await selectCustomerAndSource(user, 'standalone_meeting');

    expect(
      await screen.findByText('Active COD debts: 1 | Outstanding: 40,000 UZS')
    ).toBeInTheDocument();
    // …and the admin is told WHY that figure is bigger than this person's own.
    expect(screen.getByText(/scoped to the customer's workplace/)).toBeInTheDocument();

    // The other half of the same decision, in the same test.
    await submitCollection(user);
    await waitFor(() => {
      expect(staffService.recordCashCollection).toHaveBeenCalledTimes(1);
    });
    expect(staffService.recordCashCollection.mock.calls[0][0].delivery_address_id).toBe(44);
  });

  it('drops the place address when the backend publishes no collect scope', async () => {
    // 🔴 The degraded branch, on the admin surface. A business_app older than
    // this bundle serves no `collect_scope`, and the gate-off rollback serves
    // none either. Keeping the address while falling back on the figure is
    // precisely the shape that promised a surplus which did not exist and
    // cleared a coworker's debt instead — so the address goes with it.
    staffService.getCustomerCodStatement.mockResolvedValue(
      statementWithPlaces(ONE_PLACE, undefined)
    );

    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(staffService.getCashReconciliation).toHaveBeenCalled();
    });

    await openRecordCollectionModal(user);
    await selectCustomerAndSource(user, 'standalone_meeting');

    expect(
      await screen.findByText('Active COD debts: 0 | Outstanding: 0 UZS')
    ).toBeInTheDocument();
    expect(screen.queryByText(/scoped to the customer's workplace/)).not.toBeInTheDocument();

    await submitCollection(user);
    await waitFor(() => {
      expect(staffService.recordCashCollection).toHaveBeenCalledTimes(1);
    });
    expect(staffService.recordCashCollection.mock.calls[0][0].delivery_address_id).toBeNull();
  });

  it('shows the cluster figure for a source that can never be place-scoped', async () => {
    // A place ceiling IS published, but `admin_adjustment` is a book correction
    // — `_PLACE_SCOPE_SOURCES` refuses it place scope. Showing 40 000 here would
    // promise a settlement the backend will not perform.
    staffService.getCustomerCodStatement.mockResolvedValue(
      statementWithPlaces(ONE_PLACE, PLACE_SCOPE)
    );

    const user = userEvent.setup();
    render(<DeliveryReports />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(staffService.getCashReconciliation).toHaveBeenCalled();
    });

    await openRecordCollectionModal(user);
    await selectCustomerAndSource(user, 'admin_adjustment');

    expect(
      await screen.findByText('Active COD debts: 0 | Outstanding: 0 UZS')
    ).toBeInTheDocument();

    await submitCollection(user);
    await waitFor(() => {
      expect(staffService.recordCashCollection).toHaveBeenCalledTimes(1);
    });
    expect(staffService.recordCashCollection.mock.calls[0][0].delivery_address_id).toBeNull();
  });
});
