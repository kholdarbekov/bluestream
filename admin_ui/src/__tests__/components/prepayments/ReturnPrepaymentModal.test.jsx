import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ReturnPrepaymentModal from '../../../components/prepayments/ReturnPrepaymentModal';
import staffService from '../../../services/staffService';
import { PAYOUT_HANDLED_ERROR_CODES } from '../../../utils/cashScopeDisplay';
import { message } from 'antd';

const HANDLED = { handledErrorCodes: PAYOUT_HANDLED_ERROR_CODES };

vi.mock('../../../services/staffService');

// Interpolating mock: the "Cancel order {{order_number}}" line must render the number.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, options) => {
      const text = typeof fallback === 'string' ? fallback : key;
      return text.replace(/\{\{(\w+)\}\}/g, (_, name) => String(options?.[name] ?? ''));
    },
  }),
}));

vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    message: {
      success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn(),
    },
  };
});

const summary = (overrides = {}) => ({
  customer_id: 43,
  pool_user_ids: [43],
  free_balance: 50000,
  amount: 35000,
  target_balance: null,
  paid_by: 'driver_current_session',
  driver_user_id: 32,
  paid_at: '2026-11-10T09:00:00+05:00',
  drawn: [],
  card_click_amount: 12000,
  revenue_counted_amount: 0,
  warnings: { pending_orders: [], live_subscriptions: [], place_debts: [] },
  session_effect: null,
  contract_effect: [],
  blocking_reasons: [],
  is_applicable: true,
  ...overrides,
});

const DRIVERS = { data: { data: { items: [{ id: 2, user_id: 32, full_name: 'Aziz' }, { id: 3, user_id: 33, full_name: 'Bobur' }] } } };

const renderModal = (props = {}) => {
  const onClose = vi.fn();
  const onDone = vi.fn();
  render(<ReturnPrepaymentModal open customerId={43} freeBalance={50000} onClose={onClose} onDone={onDone} {...props} />);
  return { onClose, onDone };
};

const type = async (user, el, text) => {
  await user.clear(el);
  await user.type(el, text);
};

const pickDriver = async (user, name) => {
  await user.click(screen.getByRole('combobox'));
  await user.click(await screen.findByText(name));
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('key-1');
  staffService.getDeliveryPersons.mockResolvedValue(DRIVERS);
  staffService.previewPrepaymentPayout.mockResolvedValue({ data: { data: summary() } });
  staffService.returnPrepayment.mockResolvedValue({ data: { data: { payout: { id: 9 }, replayed: false } } });
});

describe('ReturnPrepaymentModal', () => {
  it('posts the exact preview and apply bodies for a current-trip return', async () => {
    const user = userEvent.setup();
    const { onDone } = renderModal();

    await type(user, screen.getByRole('spinbutton'), '35000');
    await user.click(screen.getByRole('radio', { name: "Driver, from his current trip's cash" }));
    await pickDriver(user, 'Aziz');
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));

    await waitFor(() => expect(staffService.previewPrepaymentPayout).toHaveBeenCalledWith(43, {
      amount: 35000, paid_by: 'driver_current_session', driver_user_id: 32,
    }, HANDLED));
    expect(staffService.getDeliveryPersons).toHaveBeenCalledWith({ status: 'active', per_page: 100 });

    await user.click(await screen.findByRole('button', { name: 'Return prepayment' }));
    await waitFor(() => expect(staffService.returnPrepayment).toHaveBeenCalledWith(43, {
      amount: 35000,
      paid_by: 'driver_current_session',
      driver_user_id: 32,
      reason: 'Customer moved away',
      idempotency_key: 'key-1',
    }, HANDLED));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it('sends paid_at only when the admin sets it', async () => {
    const user = userEvent.setup();
    renderModal();

    // untouched: no paid_at key at all
    await type(user, screen.getByRole('spinbutton'), '35000');
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    await waitFor(() => expect(staffService.previewPrepaymentPayout).toHaveBeenCalledTimes(1));
    expect(staffService.previewPrepaymentPayout.mock.calls[0][1]).toEqual({ amount: 35000, paid_by: 'office' });

    await user.click(await screen.findByRole('button', { name: 'Back' }));
    const picker = screen.getByPlaceholderText('Paid at (optional)');
    await user.type(picker, '2026-11-09 10:30{Enter}');
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    await waitFor(() => expect(staffService.previewPrepaymentPayout).toHaveBeenCalledTimes(2));
    expect(staffService.previewPrepaymentPayout.mock.calls[1][1]).toEqual({
      amount: 35000,
      paid_by: 'office',
      paid_at: new Date(2026, 10, 9, 10, 30).toISOString(),
    });
    await user.click(await screen.findByRole('button', { name: 'Return prepayment' }));
    await waitFor(() => expect(staffService.returnPrepayment).toHaveBeenCalledWith(43, {
      amount: 35000,
      paid_by: 'office',
      paid_at: new Date(2026, 10, 9, 10, 30).toISOString(),
      reason: 'Customer moved away',
      idempotency_key: 'key-1',
    }, HANDLED));
  });

  it('set-balance mode posts target_balance and never amount', async () => {
    staffService.previewPrepaymentPayout.mockResolvedValue({
      data: { data: summary({ amount: 35000, target_balance: 15000, paid_by: 'office', driver_user_id: null }) },
    });
    const user = userEvent.setup();
    renderModal();

    await user.click(screen.getByRole('radio', { name: 'Balance after return' }));
    // default target is 0; the free balance is not carried into the target field
    expect(screen.getByRole('spinbutton')).toHaveValue('0');
    await type(user, screen.getByRole('spinbutton'), '15000');
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));

    await waitFor(() => expect(staffService.previewPrepaymentPayout).toHaveBeenCalledWith(43, {
      target_balance: 15000, paid_by: 'office',
    }, HANDLED));
    const body = staffService.previewPrepaymentPayout.mock.calls[0][1];
    expect('amount' in body).toBe(false);
    // step 2 shows the amount the backend derived
    expect(await screen.findByText(/35,000/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Return prepayment' }));
    await waitFor(() => expect(staffService.returnPrepayment).toHaveBeenCalledWith(43, {
      target_balance: 15000, paid_by: 'office', reason: 'Customer moved away', idempotency_key: 'key-1',
    }, HANDLED));
    expect('amount' in staffService.returnPrepayment.mock.calls[0][1]).toBe(false);
  });

  it('labels the two modes', () => {
    renderModal();
    expect(screen.getByRole('radio', { name: 'Amount to return' })).toBeChecked();
    expect(screen.getByRole('radio', { name: 'Balance after return' })).not.toBeChecked();
    expect(screen.getByRole('spinbutton')).toHaveValue('50,000');
  });

  it('does not preview a driver mode until a driver is picked', async () => {
    const user = userEvent.setup();
    renderModal();
    await user.click(screen.getByRole('radio', { name: 'Driver, already settled' }));
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    // give a wrong call a chance to happen
    await new Promise((r) => setTimeout(r, 50));
    expect(staffService.previewPrepaymentPayout).not.toHaveBeenCalled();
  });

  it('disables Return and shows translated blocks, ignoring will_close', async () => {
    staffService.previewPrepaymentPayout.mockResolvedValue({
      data: {
        data: summary({
          blocking_reasons: ['amount_exceeds_driver_cash: asked 35000, driver holds 1000'],
          is_applicable: false,
          session_effect: {
            session_id: 30, status: 'overdue', on_hand_before: 1000, handed_in: 0, cap: 1000, on_hand_after: -34000, will_close: true,
          },
        }),
      },
    });
    const user = userEvent.setup();
    renderModal();
    await user.click(screen.getByRole('radio', { name: "Driver, from his current trip's cash" }));
    await pickDriver(user, 'Aziz');
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));

    expect(await screen.findByText("The driver's current trip does not hold that much cash.")).toBeInTheDocument();
    expect(screen.queryByText(/asked 35000/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Return prepayment' })).toBeDisabled();
    // the over-cap plan must not announce that the trip closes
    expect(screen.queryByText(/closes/i)).toBeNull();
    expect(staffService.returnPrepayment).not.toHaveBeenCalled();
  });

  it('lists a held order with the cancel-to-release line and the subscription and place warnings', async () => {
    staffService.previewPrepaymentPayout.mockResolvedValue({
      data: {
        data: summary({
          warnings: {
            pending_orders: [
              { order_id: 7, order_number: 'AD_1', status: 'pending', held_amount: 30000 },
              { order_id: 8, order_number: 'AD_2', status: 'pending', held_amount: 0 },
            ],
            live_subscriptions: [{ subscription_id: 5, status: 'active' }],
            place_debts: [{ place_group_id: 3, label: 'Cafe Roma', amount: 90000 }],
          },
        }),
      },
    });
    const user = userEvent.setup();
    renderModal();
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));

    expect(await screen.findByText('Cancel order AD_1 to release it')).toBeInTheDocument();
    expect(screen.queryByText('Cancel order AD_2 to release it')).toBeNull();
    expect(screen.getByText(/Cafe Roma/)).toBeInTheDocument();
    expect(screen.getByText(/#5/)).toBeInTheDocument();
    expect(screen.getByText('Of this, from card/Click')).toBeInTheDocument();
    expect(within(screen.getByTestId('payout-card-click')).getByText(/12,000/)).toBeInTheDocument();
  });

  it('makes one idempotency key per open', async () => {
    const user = userEvent.setup();
    renderModal();
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    await user.click(await screen.findByRole('button', { name: 'Back' }));
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    await screen.findByRole('button', { name: 'Return prepayment' });
    expect(crypto.randomUUID).toHaveBeenCalledTimes(1);
  });

  it('previews a fully held customer without an amount and shows the held order and the block', async () => {
    staffService.previewPrepaymentPayout.mockResolvedValue({
      data: {
        data: summary({
          free_balance: 0,
          amount: 0,
          blocking_reasons: ['no_free_balance: the customer has no free prepaid balance to return'],
          is_applicable: false,
          warnings: {
            pending_orders: [{ order_id: 7, order_number: 'AD_1', status: 'pending', held_amount: 30000 }],
            live_subscriptions: [],
            place_debts: [],
          },
        }),
      },
    });
    const user = userEvent.setup();
    renderModal({ freeBalance: 0 });
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));

    await waitFor(() => expect(staffService.previewPrepaymentPayout).toHaveBeenCalledWith(43, { paid_by: 'office' }, HANDLED));
    expect('amount' in staffService.previewPrepaymentPayout.mock.calls[0][1]).toBe(false);
    expect(await screen.findByText('Cancel order AD_1 to release it')).toBeInTheDocument();
    expect(screen.getByText(/There is no free prepayment to return/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Return prepayment' })).toBeDisabled();
  });

  it('refuses a typed 0 amount inline when there is a balance, and sends nothing', async () => {
    const user = userEvent.setup();
    renderModal();
    await type(user, screen.getByRole('spinbutton'), '0');
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    expect(await screen.findByText('Enter an amount above 0')).toBeInTheDocument();
    expect(staffService.previewPrepaymentPayout).not.toHaveBeenCalled();
  });

  it('starts every open from the balance of that open', async () => {
    const onClose = vi.fn();
    const { rerender, unmount } = render(
      <ReturnPrepaymentModal open customerId={43} freeBalance={50000} onClose={onClose} onDone={vi.fn()} />,
    );
    expect(screen.getByRole('spinbutton')).toHaveValue('50,000');
    // the pages mount the modal only while it is open, so a close unmounts it
    unmount();
    render(<ReturnPrepaymentModal open customerId={43} freeBalance={20000} onClose={onClose} onDone={vi.fn()} />);
    expect(screen.getByRole('spinbutton')).toHaveValue('20,000');
    expect(rerender).toBeDefined();
  });

  it('names the recorded amount of the response, not the typed one', async () => {
    staffService.returnPrepayment.mockResolvedValue({ data: { data: { payout: { id: 9, amount: 35000 }, replayed: false } } });
    staffService.previewPrepaymentPayout.mockResolvedValue({
      data: { data: summary({ amount: 30000, target_balance: 20000, paid_by: 'office', driver_user_id: null }) },
    });
    const user = userEvent.setup();
    renderModal();
    await user.click(screen.getByRole('radio', { name: 'Balance after return' }));
    await type(user, screen.getByRole('spinbutton'), '20000');
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    await user.click(await screen.findByRole('button', { name: 'Return prepayment' }));
    await waitFor(() => expect(message.success).toHaveBeenCalledTimes(1));
    expect(message.success.mock.calls[0][0]).toMatch(/35,000/);
    expect(message.success.mock.calls[0][0]).not.toMatch(/30,000/);
  });

  it('warns that a return which closes the trip cannot be undone', async () => {
    staffService.previewPrepaymentPayout.mockResolvedValue({
      data: {
        data: summary({
          session_effect: {
            session_id: 30, status: 'open', on_hand_before: 35000, handed_in: 0, cap: 35000, on_hand_after: 0, will_close: true,
          },
        }),
      },
    });
    const user = userEvent.setup();
    renderModal();
    await user.click(screen.getByRole('radio', { name: "Driver, from his current trip's cash" }));
    await pickDriver(user, 'Aziz');
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    expect(
      await screen.findByText("This return closes the driver's trip; it can't be undone afterwards."),
    ).toBeInTheDocument();
  });

  it('shows a hint instead of an empty list when the drivers fail to load', async () => {
    staffService.getDeliveryPersons.mockRejectedValue(new Error('boom'));
    const user = userEvent.setup();
    renderModal();
    await user.click(screen.getByRole('radio', { name: 'Driver, already settled' }));
    await user.click(screen.getByRole('combobox'));
    expect(await screen.findByText('Could not load the drivers. Close this window and try again.')).toBeInTheDocument();
  });

  it('explains a refusal once, in the admin language, never as the server sentence', async () => {
    const refusal = {
      response: {
        status: 400,
        data: {
          error_code: 'PAYOUT_BLOCKED',
          errors: ['no_free_balance: the customer has no free prepaid balance; amount_exceeds_free_balance: asked 9 of 5'],
        },
      },
    };
    staffService.returnPrepayment.mockRejectedValue(refusal);
    const user = userEvent.setup();
    renderModal();
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    await user.click(await screen.findByRole('button', { name: 'Return prepayment' }));
    await waitFor(() => expect(message.error).toHaveBeenCalledTimes(1));
    const text = message.error.mock.calls[0][0];
    expect(text).toContain('There is no free prepayment to return.');
    expect(text).toContain('That is more than the free prepayment balance.');
    expect(text).not.toMatch(/asked 9/);
  });

  it('never lets a future paid_at out: later hours today are not selectable, and a stale pick is refused', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date(2026, 10, 10, 10, 0));
    try {
      const user = userEvent.setup();
      renderModal();
      await type(user, screen.getByRole('spinbutton'), '35000');
      await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
      // 15:00 today is later than now: the picker refuses to take it, so no paid_at goes out
      await user.type(screen.getByPlaceholderText('Paid at (optional)'), '2026-11-10 15:00{Enter}');
      await user.click(screen.getByRole('button', { name: 'Preview' }));
      await waitFor(() => expect(staffService.previewPrepaymentPayout).toHaveBeenCalledTimes(1));
      expect('paid_at' in staffService.previewPrepaymentPayout.mock.calls[0][1]).toBe(false);

      // 09:30 is allowed at 10:00; the clock then runs back (a stale pick): the rule refuses it
      await user.click(await screen.findByRole('button', { name: 'Back' }));
      await user.type(screen.getByPlaceholderText('Paid at (optional)'), '2026-11-10 09:30{Enter}');
      vi.setSystemTime(new Date(2026, 10, 10, 9, 0));
      await user.click(screen.getByRole('button', { name: 'Preview' }));
      expect(await screen.findByText("Paid at can't be in the future")).toBeInTheDocument();
      expect(staffService.previewPrepaymentPayout).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('stale freeBalance 0 with credit now free: the preview plans it and the apply carries the preview amount', async () => {
    staffService.previewPrepaymentPayout.mockResolvedValue({
      data: { data: summary({ free_balance: 41000, amount: 41000, paid_by: 'office', driver_user_id: null }) },
    });
    const user = userEvent.setup();
    renderModal({ freeBalance: 0 });
    // nothing free as far as the page knew: the amount is locked, with a hint
    expect(screen.getByRole('spinbutton')).toBeDisabled();
    expect(screen.getByText('There is no free prepayment right now. Preview shows what is held.')).toBeInTheDocument();
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));

    await waitFor(() => expect(staffService.previewPrepaymentPayout).toHaveBeenCalledWith(43, { paid_by: 'office' }, HANDLED));
    await user.click(await screen.findByRole('button', { name: 'Return prepayment' }));
    await waitFor(() => expect(staffService.returnPrepayment).toHaveBeenCalledWith(43, {
      amount: 41000, paid_by: 'office', reason: 'Customer moved away', idempotency_key: 'key-1',
    }, HANDLED));
  });

  it('applies the amount the preview answered, not the typed one', async () => {
    staffService.previewPrepaymentPayout.mockResolvedValue({
      data: { data: summary({ amount: 27000, paid_by: 'office', driver_user_id: null }) },
    });
    const user = userEvent.setup();
    renderModal();
    await type(user, screen.getByRole('spinbutton'), '35000');
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    await user.click(await screen.findByRole('button', { name: 'Return prepayment' }));
    await waitFor(() => expect(staffService.returnPrepayment).toHaveBeenCalledWith(43, {
      amount: 27000, paid_by: 'office', reason: 'Customer moved away', idempotency_key: 'key-1',
    }, HANDLED));
  });

  it('adds no toast of its own to an unmapped failure (api.js toasts it once), on preview and on apply', async () => {
    const serverError = { response: { status: 500, data: {} } };
    staffService.previewPrepaymentPayout.mockRejectedValueOnce(serverError);
    const user = userEvent.setup();
    renderModal();
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    await waitFor(() => expect(staffService.previewPrepaymentPayout).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 30));
    expect(message.error).not.toHaveBeenCalled();

    staffService.returnPrepayment.mockRejectedValueOnce(serverError);
    await user.click(screen.getByRole('button', { name: 'Preview' }));
    await user.click(await screen.findByRole('button', { name: 'Return prepayment' }));
    await waitFor(() => expect(staffService.returnPrepayment).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 30));
    expect(message.error).not.toHaveBeenCalled();
  });

  it('renders translated statuses and a readable contract label in the warnings', async () => {
    staffService.previewPrepaymentPayout.mockResolvedValue({
      data: {
        data: summary({
          warnings: {
            pending_orders: [{ order_id: 7, order_number: 'AD_1', status: 'out_for_delivery', held_amount: 0 }],
            live_subscriptions: [{ subscription_id: 5, status: 'paused' }],
            place_debts: [],
          },
          contract_effect: [{ contract_id: 4, contract_number: 'C-2026-04', name: 'Korzinka', amount: 30000 }],
        }),
      },
    });
    const user = userEvent.setup();
    renderModal();
    await user.type(screen.getByLabelText('Reason'), 'Customer moved away');
    await user.click(screen.getByRole('button', { name: 'Preview' }));

    expect(await screen.findByText('AD_1 · Out for delivery · 0 UZS')).toBeInTheDocument();
    expect(screen.getByText('#5 · Paused')).toBeInTheDocument();
    expect(screen.getByText('C-2026-04 · Korzinka · 30,000 UZS')).toBeInTheDocument();
    expect(screen.queryByText(/out_for_delivery/)).toBeNull();
    expect(screen.queryByText(/#4/)).toBeNull();
  });
});
