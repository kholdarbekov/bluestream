import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import AgentPayTab from '../../../../components/sales/pay/AgentPayTab';
import salesPayService from '../../../../services/salesPayService';

vi.mock('../../../../services/salesPayService', async () => {
  const actual = await vi.importActual('../../../../services/salesPayService');
  const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(actual.default)).filter((name) => name !== 'constructor');
  return { ...actual, default: Object.fromEntries(methods.map((name) => [name, vi.fn()])) };
});
const mockSeeded = { 'sales_agents:pay.error.sales_pay_month_locked': 'No longer editable: {{month}}.' };
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
const mockAuth = { hasPermission: vi.fn(() => true) };
vi.mock('../../../../stores/authStore', () => ({ useAuthStore: () => mockAuth }));

const TERM_OCT = {
  id: 7, effective_month: '2026-10', base_salary: 3000000.0, plan: { id: 1, name: 'Standard' }, note: null,
  created_at: '2026-09-28T06:00:00+00:00', created_by: { id: 1, name: 'Admin User' },
};
const TERM_DEC = {
  id: 9, effective_month: '2026-12', base_salary: 3200000.0, plan: { id: 2, name: 'Senior' }, note: 'raise',
  created_at: '2026-11-02T06:00:00+00:00', created_by: { id: 1, name: 'Admin User' },
};
// A16 for a leaver who owes 50,000 from his November statement, netted in December (Q15, I-28).
// The terms arrive oldest first on purpose: the table must still read newest first.
const TERMS = {
  agent: { user_id: 41, name: 'Aziz K.', phone: '+998901234574', is_active: false },
  employment: { start: '2026-06-01', end: '2026-11-20' },
  owed_to_date: { amount: 50000.0, month: '2026-11', nets_in: '2026-12' },
  terms: [TERM_OCT, TERM_DEC],
  term_in_force: TERM_OCT,
  plans: [{ id: 1, name: 'Standard' }, { id: 2, name: 'Senior' }],
  editable_from_month: '2026-12',
};

const renderTab = ({ active = true } = {}) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
  render(
    <QueryClientProvider client={queryClient}>
      <AgentPayTab agentUserId={41} active={active} />
    </QueryClientProvider>,
  );
  return { invalidate };
};

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
  salesPayService.getAgentTerms.mockResolvedValue(TERMS);
  salesPayService.createAdjustment.mockResolvedValue({ adjustment: { id: 12 } });
});

it('shows the outstanding balance from the published amount and month', async () => {
  renderTab();

  expect(await screen.findByText('Outstanding balance owed by the agent: 50,000 (statement 11.2026)')).toBeInTheDocument();
  expect(salesPayService.getAgentTerms).toHaveBeenCalledWith(41);
});

it.each([
  ['nothing is owed', { amount: 0.0, month: null, nets_in: null }, false, false],
  ['no month nets it yet', { amount: 50000.0, month: '2026-11', nets_in: null }, true, false],
  ['a month nets it', { amount: 50000.0, month: '2026-11', nets_in: '2026-12' }, true, true],
])('when %s, the balance row and the repayment door follow the published fields', async (_label, owed, row, door) => {
  salesPayService.getAgentTerms.mockResolvedValue({ ...TERMS, owed_to_date: owed });
  renderTab();
  await screen.findByText('New terms');

  expect(Boolean(screen.queryByText(/Outstanding balance owed by the agent/))).toBe(row);
  expect(Boolean(screen.queryByRole('button', { name: 'Record repayment' }))).toBe(door);
});

it('records a repayment into the published nets_in month, once, and refreshes every pay query', async () => {
  const { invalidate } = renderTab();
  fireEvent.click(await screen.findByRole('button', { name: 'Record repayment' }));
  const dialog = await screen.findByRole('dialog');

  // A minus is refused before submit, and so is a missing reason.
  fireEvent.change(within(dialog).getByLabelText('Amount'), { target: { value: '-50 000' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
  expect(await within(dialog).findByText('Enter an amount')).toBeInTheDocument();
  expect(await within(dialog).findByText('Enter a reason')).toBeInTheDocument();
  expect(salesPayService.createAdjustment).not.toHaveBeenCalled();

  fireEvent.change(within(dialog).getByLabelText('Amount'), { target: { value: '50,000' } });
  fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: 'Repaid offline' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(salesPayService.createAdjustment).toHaveBeenCalledTimes(1));
  expect(salesPayService.createAdjustment).toHaveBeenCalledWith('2026-12', 41, { amount: 50000, reason: 'Repaid offline' });
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['salesPay'] }));
});

it('lists the terms newest first and tags the one in force', async () => {
  renderTab();
  // Scoped to the terms table: the employment `Descriptions` above it is a table too.
  const table = (await screen.findByText('12.2026')).closest('table');

  const rows = within(table).getAllByRole('row').slice(1);
  expect(rows[0]).toHaveTextContent('12.2026');
  expect(rows[0]).toHaveTextContent('3,200,000');
  expect(rows[0]).not.toHaveTextContent('In force');
  expect(rows[1]).toHaveTextContent('10.2026');
  expect(rows[1]).toHaveTextContent('In force');
});

it('asks for the employment start before any terms', async () => {
  salesPayService.getAgentTerms.mockResolvedValue({ ...TERMS, employment: { start: null, end: null }, terms: [], term_in_force: null });
  renderTab();

  expect(await screen.findByText('Set the employment start date before adding pay terms')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'New terms' })).toBeNull();
});

it('names every locked month of a refused employment change inline', async () => {
  salesPayService.setEmployment.mockRejectedValue({
    response: {
      status: 409,
      data: {
        success: false, message: 'Month locked', error_code: 'SALES_PAY_MONTH_LOCKED',
        details: { months: ['2026-10', '2026-11'], status: 'approved' },
      },
    },
  });
  renderTab();
  fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

  const alert = await within(dialog).findByTestId('pay-error');
  expect(alert).toHaveTextContent('No longer editable: 10.2026, 11.2026.');
  expect(salesPayService.setEmployment).toHaveBeenCalledWith(41, { start: '2026-06-01', end: '2026-11-20' });
});

it('fires no pay read while the tab is not the active one', async () => {
  renderTab({ active: false });

  await new Promise((resolve) => { setTimeout(resolve, 0); });
  expect(salesPayService.getAgentTerms).not.toHaveBeenCalled();
});
