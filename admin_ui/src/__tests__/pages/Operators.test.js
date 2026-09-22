import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import Operators from '../../pages/Operators';
import staffService from '../../services/staffService';

vi.mock('../../services/staffService');
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (typeof opts === 'string' ? opts : opts?.defaultValue) || key }),
}));
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return { ...actual, message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn() } };
});

const OPERATOR = {
  id: 52, first_name: 'Dilnoza', last_name: 'Rashidova', phone: '+998901234590',
  email: 'dilnoza@example.com', status: 'active', staff_roles: ['operator'],
  telegram_linked: false, last_login: null, created_at: '2026-09-01T00:00:00Z',
};
const LIST = { data: { data: { items: [OPERATOR] }, meta: { total: 1, summary: {} } } };

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => (
    <MemoryRouter initialEntries={['/staff/operators']}>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </MemoryRouter>
  );
};

beforeEach(() => {
  vi.clearAllMocks();
  staffService.getOperators.mockResolvedValue(LIST);
  staffService.createOperator.mockResolvedValue({ data: { data: { operator: OPERATOR } } });
  staffService.updateOperator.mockResolvedValue({ data: { data: { operator: OPERATOR } } });
});

it('omits email when creating an operator with the field left blank', async () => {
  // The backend resolves an existing account BY PHONE and attaches the operator role to it, so a
  // blank input must not travel as `email: null` -- that asserts "clear it" and wiped the email of
  // whoever already owned the typed number. `exclude_unset` drops the omitted key instead.
  render(<Operators />, { wrapper: createWrapper() });
  await screen.findByRole('row', { name: /Dilnoza/ });
  fireEvent.click(screen.getByRole('button', { name: /staff:add_operator/i }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText('staff:first_name'), { target: { value: 'Kamola' } });
  fireEvent.change(within(dialog).getByLabelText('staff:phone'), { target: { value: '+998901234591' } });
  fireEvent.click(within(dialog).getByRole('button', { name: /common:save/i }));

  await waitFor(() => expect(staffService.createOperator).toHaveBeenCalledTimes(1));
  expect(staffService.createOperator.mock.calls[0][0]).not.toHaveProperty('email');
});

it('still clears the email when an operator is edited and the field is emptied', async () => {
  // The edit door names the operator by id, so emptying a value the admin can actually see keeps
  // meaning "clear it". This is the half of the rule the create fix must not break.
  render(<Operators />, { wrapper: createWrapper() });
  await screen.findByRole('row', { name: /Dilnoza/ });
  fireEvent.click(screen.getByRole('button', { name: /common:edit/i }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText('staff:email'), { target: { value: '' } });
  fireEvent.click(within(dialog).getByRole('button', { name: /common:save/i }));

  await waitFor(() => expect(staffService.updateOperator).toHaveBeenCalledTimes(1));
  expect(staffService.updateOperator.mock.calls[0][1].email).toBeNull();
});
