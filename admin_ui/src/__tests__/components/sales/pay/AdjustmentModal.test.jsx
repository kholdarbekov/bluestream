import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import AdjustmentModal from '../../../../components/sales/pay/AdjustmentModal';
import salesPayService from '../../../../services/salesPayService';

vi.mock('../../../../services/salesPayService', async () => {
  const actual = await vi.importActual('../../../../services/salesPayService');
  const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(actual.default)).filter((name) => name !== 'constructor');
  return { ...actual, default: Object.fromEntries(methods.map((name) => [name, vi.fn()])) };
});
// Two seeded rows, so the refusal tests read the admin's sentence with `details.month` / `details.
// agents` interpolated rather than the backend's fallback message.
const mockSeeded = {
  'sales_agents:pay.error.sales_pay_month_locked': 'No longer editable: {{month}}.',
  'sales_agents:pay.error.sales_pay_terms_missing': 'Set pay terms first for: {{agents}}.',
};
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => {
      const text = mockSeeded[key] || (typeof opts === 'string' ? opts : opts?.defaultValue) || key;
      return opts && typeof opts === 'object'
        ? text.replace(/\{\{(\w+)\}\}/g, (_, name) => String(opts[name] ?? ''))
        : text;
    },
    i18n: { language: 'en' },
  }),
}));

const renderModal = (props) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <AdjustmentModal open month="2026-10" agentId={41} mode="signed" onClose={onClose} {...props} />
    </QueryClientProvider>,
  );
  return { invalidate, onClose };
};

const fill = (amount, reason) => {
  fireEvent.change(screen.getByLabelText('Amount'), { target: { value: amount } });
  if (reason !== undefined) fireEvent.change(screen.getByLabelText('Reason'), { target: { value: reason } });
};
const save = () => fireEvent.click(screen.getByRole('button', { name: 'Save' }));

beforeEach(() => {
  vi.clearAllMocks();
  salesPayService.createAdjustment.mockResolvedValue({ adjustment: { id: 5 } });
});

it.each([
  ['-50 000', -50000],
  ['−50,000', -50000],
  ['50 000.00', 50000],
  ['1.5e6', 1500000],
])('a signed adjustment typed as %j posts amount %d exactly', async (typed, amount) => {
  const { invalidate, onClose } = renderModal();
  expect(screen.getByText('Use − for a deduction. To undo an adjustment, add the opposite amount with a reason.')).toBeInTheDocument();

  fill(typed, 'Missed stock count');
  save();

  await waitFor(() => expect(salesPayService.createAdjustment).toHaveBeenCalledTimes(1));
  expect(salesPayService.createAdjustment).toHaveBeenCalledWith('2026-10', 41, { amount, reason: 'Missed stock count' });
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['salesPay'] }));
  expect(onClose).toHaveBeenCalled();
});

it('a recorded repayment names its published month and refuses a negative before submit', async () => {
  renderModal({ month: '2026-12', mode: 'repayment' });
  expect(screen.getByText('Record repayment')).toBeInTheDocument();
  expect(screen.getByText(
    'Money the agent repaid outside the system. It is added as an adjustment in 12.2026, which deducts it from the balance.',
  )).toBeInTheDocument();

  fill('-50 000', 'Repaid offline');
  save();

  expect(await screen.findByText('Enter an amount')).toBeInTheDocument();
  expect(salesPayService.createAdjustment).not.toHaveBeenCalled();
});

it('a recorded repayment needs a reason', async () => {
  renderModal({ month: '2026-12', mode: 'repayment' });

  fill('50 000');
  save();

  expect(await screen.findByText('Enter a reason')).toBeInTheDocument();
  expect(salesPayService.createAdjustment).not.toHaveBeenCalled();
});

it('shows a named refusal once, inline, with its month', async () => {
  salesPayService.createAdjustment.mockRejectedValue({
    response: {
      status: 409,
      data: {
        success: false, message: 'Month locked', error_code: 'SALES_PAY_MONTH_LOCKED',
        details: { month: '2026-12', status: 'approved' },
      },
    },
  });
  const { onClose } = renderModal({ month: '2026-12', mode: 'repayment' });

  fill('50000', 'Repaid offline');
  save();

  const alerts = await screen.findAllByTestId('pay-error');
  expect(alerts).toHaveLength(1);
  expect(alerts[0]).toHaveTextContent('No longer editable: 12.2026.');
  expect(onClose).not.toHaveBeenCalled();
});

// T12-R1: a closed-month re-freeze can also answer TERMS_MISSING (`details.agents` carries the
// id of the agent the write is for); the modal must name them from the `agentName` its caller
// already has, never print the raw id.
it('names the agent from its agentName prop, not their id, in a TERMS_MISSING refusal', async () => {
  salesPayService.createAdjustment.mockRejectedValue({
    response: {
      status: 409,
      data: {
        success: false, message: 'Set pay terms first', error_code: 'SALES_PAY_TERMS_MISSING',
        details: { agents: [41], month: '2026-10' },
      },
    },
  });
  renderModal({ agentName: 'Aziz K.' });

  fill('50000', 'Missed stock count');
  save();

  const alerts = await screen.findAllByTestId('pay-error');
  expect(alerts).toHaveLength(1);
  expect(alerts[0]).toHaveTextContent('Set pay terms first for: Aziz K.');
  expect(alerts[0]).not.toHaveTextContent('#41');
});
