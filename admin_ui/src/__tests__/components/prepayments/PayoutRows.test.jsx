import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { message } from 'antd';

import PayoutRows from '../../../components/prepayments/PayoutRows';
import staffService from '../../../services/staffService';
import { PAYOUT_HANDLED_ERROR_CODES } from '../../../utils/cashScopeDisplay';

vi.mock('../../../services/staffService');

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback) => (typeof fallback === 'string' ? fallback : key),
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

const payout = {
  id: 9,
  paid_at: '2026-11-10T09:00:00+05:00',
  amount: 35000,
  paid_by: 'office',
  driver_name: null,
  reason: 'Customer moved away',
  voided_at: null,
  void_reason: null,
  can_undo: true,
  undo_block_code: null,
};

const openUndo = async (user, onChanged) => {
  render(<PayoutRows payouts={[payout]} onChanged={onChanged} />);
  await user.click(within(screen.getByTestId('payout-rows')).getByRole('button', { name: 'Undo' }));
  await user.type(await screen.findByLabelText('Reason'), 'Recorded twice');
  const dialog = screen.getByText('Undo this return').closest('.ant-modal-content');
  await user.click(within(dialog).getByRole('button', { name: 'Undo' }));
};

beforeEach(() => vi.clearAllMocks());

describe('PayoutRows undo failures', () => {
  it('a refused undo (the trip closed meanwhile) tells the admin once and refreshes the row', async () => {
    staffService.undoPrepaymentPayout.mockRejectedValue({
      response: {
        status: 400,
        data: { error_code: 'PAYOUT_UNDO_BLOCKED', errors: ['session_checked: the trip is closed'] },
      },
    });
    const onChanged = vi.fn();
    await openUndo(userEvent.setup(), onChanged);

    await waitFor(() => expect(staffService.undoPrepaymentPayout).toHaveBeenCalledWith(
      9, { reason: 'Recorded twice' }, { handledErrorCodes: PAYOUT_HANDLED_ERROR_CODES },
    ));
    await waitFor(() => expect(message.error).toHaveBeenCalledTimes(1));
    expect(message.error.mock.calls[0][0]).toBe("The driver's trip is closed, so this return can no longer be undone.");
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it('an unmapped failure adds no toast of its own and does not refresh', async () => {
    staffService.undoPrepaymentPayout.mockRejectedValue({ response: { status: 500, data: {} } });
    const onChanged = vi.fn();
    await openUndo(userEvent.setup(), onChanged);

    await waitFor(() => expect(staffService.undoPrepaymentPayout).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 30));
    expect(message.error).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
  });
});
