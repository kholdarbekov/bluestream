import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import SalesAgents from '../../pages/SalesAgents';
import staffService from '../../services/staffService';
import api from '../../services/api';

vi.mock('../../services/staffService');
vi.mock('../../services/api', () => ({ __esModule: true, default: { get: vi.fn() } }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (typeof opts === 'string' ? opts : opts?.defaultValue) || key }),
}));
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return { ...actual, message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn() } };
});

const AGENT = {
  user_id: 41, full_name: 'Sardor Alimov', first_name: 'Sardor', last_name: 'Alimov', phone: '+998901234574', email: null,
  status: 'active', staff_roles: ['sales_agent'], telegram_linked: false, is_active: true, districts: ['chilanzar'],
  weekly_new_outlet_target: 5, employment_type: 'employee', notes: null, outlets_assigned: 3, outlets_active: 2,
  visits_today: 4, orders_today: 2,
  last_login: null, created_at: '2026-09-01T00:00:00Z',
};
const LIST = { data: { data: { items: [AGENT] }, meta: { total: 1, summary: { total_agents: 1, active_agents: 1 } } } };

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => (
    <MemoryRouter initialEntries={['/staff/sales-agents']}>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </MemoryRouter>
  );
};

beforeEach(() => {
  vi.clearAllMocks();
  staffService.getSalesAgents.mockResolvedValue(LIST);
  staffService.createSalesAgent.mockResolvedValue({ data: { data: { sales_agent: AGENT } } });
  staffService.setSalesAgentActive.mockResolvedValue({ data: { data: { sales_agent: { ...AGENT, is_active: false } } } });
  api.get.mockResolvedValue({ data: { success: true, data: { districts: [{ key: 'chilanzar', name: 'Chilanzar' }, { key: 'yunusabad', name: 'Yunusabad' }] } } });
});

it('renders the summary cards and the agent row with outlet counts', async () => {
  render(<SalesAgents />, { wrapper: createWrapper() });
  const row = await screen.findByRole('row', { name: /Sardor Alimov/ });
  expect(row).toHaveTextContent('+998901234574');
  expect(row).toHaveTextContent('3 / 2');
  expect(screen.getByText('Total agents')).toBeInTheDocument();
  expect(staffService.getSalesAgents).toHaveBeenCalledWith({ page: 1, per_page: 20, search: undefined, status: undefined });
});

it('creates an agent with the payload the backend expects', async () => {
  render(<SalesAgents />, { wrapper: createWrapper() });
  await screen.findByRole('row', { name: /Sardor Alimov/ });
  fireEvent.click(screen.getByRole('button', { name: /add sales agent/i }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Nodira Karimova' } });
  fireEvent.change(within(dialog).getByLabelText('Phone'), { target: { value: '+998901234580' } });
  fireEvent.change(within(dialog).getByLabelText('Weekly new-outlet target'), { target: { value: '4' } });
  fireEvent.click(within(dialog).getByRole('button', { name: /save/i }));
  await waitFor(() => expect(staffService.createSalesAgent).toHaveBeenCalledTimes(1));
  // No `email` key at all: an untouched input on the CREATE form means "I have none to add".
  // Sending `email: null` asserted "clear it", and because the backend attaches to whoever
  // already owns the typed phone, that wiped a real account's email (an admin's, locking them
  // out of the panel). `exclude_unset` makes the omitted key a clean no-op instead.
  expect(staffService.createSalesAgent.mock.calls[0][0]).toEqual({
    full_name: 'Nodira Karimova', phone: '+998901234580', districts: [], weekly_new_outlet_target: 4, employment_type: 'employee', notes: null,
  });
});

it('toggles activation from the table', async () => {
  render(<SalesAgents />, { wrapper: createWrapper() });
  const row = await screen.findByRole('row', { name: /Sardor Alimov/ });
  fireEvent.click(within(row).getByRole('switch'));
  await waitFor(() => expect(staffService.setSalesAgentActive).toHaveBeenCalledWith(41, false));
});

it("shows today's field activity on the cards and on the row", async () => {
  // A SECOND agent, and summary totals that deliberately do NOT equal the sum of the two rows
  // (R33): 11 and 5, against rows summing to 7 and 2. The page is paginated at 20 and the
  // summary is estate-wide, so a page-local `items.reduce(...)` would render 7/2 here and fail —
  // which is the whole point of picking numbers a sum cannot produce.
  staffService.getSalesAgents.mockResolvedValue({
    data: {
      data: { items: [AGENT, { ...AGENT, user_id: 77, full_name: 'Nodira Karimova', phone: '+998901234580', visits_today: 3, orders_today: 0 }] },
      meta: { total: 2, summary: { total_agents: 9, active_agents: 8, visits_today: 11, orders_today: 5 } },
    },
  });
  render(<SalesAgents />, { wrapper: createWrapper() });

  const row = await screen.findByRole('row', { name: /Sardor Alimov/ });
  expect(within(row).getByText('4')).toBeInTheDocument();

  // The cards are the route's estate-wide summary, rendered — not a second rule computed here.
  // `getAllByText`, not `getByText`: the table's `scroll.x` makes rc-table render an extra hidden
  // measure row that repeats every column header's text off-screen, so "Visits today" matches the
  // card AND both the real and the measure-row copies of the column header.
  const visitsCard = screen.getAllByText('Visits today').map((el) => el.closest('.ant-card')).find(Boolean);
  expect(within(visitsCard).getByText('11')).toBeInTheDocument();
  const ordersCard = screen.getAllByText('Orders today').map((el) => el.closest('.ant-card')).find(Boolean);
  expect(within(ordersCard).getByText('5')).toBeInTheDocument();
});
