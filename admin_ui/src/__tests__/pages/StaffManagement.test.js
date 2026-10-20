import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import StaffManagement from '../../pages/StaffManagement';
import staffService from '../../services/staffService';

vi.mock('../../services/staffService');
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, fallback) => fallback || key }),
}));
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return { ...actual, message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn() } };
});

const page = (items) => ({ data: { data: { items }, meta: { has_next: false } } });

const SALES_AGENT = {
  user_id: 41, full_name: 'Sardor Alimov', phone: '+998901234574', staff_roles: ['sales_agent'],
};

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => (
    <MemoryRouter initialEntries={['/staff/management']}>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </MemoryRouter>
  );
};

beforeEach(() => {
  vi.clearAllMocks();
  staffService.getStaffOverview.mockResolvedValue({ data: { data: { overview: {} } } });
  staffService.getDeliveryPersons.mockResolvedValue(page([]));
  staffService.getOperators.mockResolvedValue(page([]));
  staffService.getSalesAgents.mockResolvedValue(page([SALES_AGENT]));
});

it('loads sales agents as invite candidates and offers the sales_agent role', async () => {
  render(<StaffManagement />, { wrapper: createWrapper() });

  await waitFor(() => expect(staffService.getSalesAgents).toHaveBeenCalledWith({ page: 1, per_page: 100 }));

  fireEvent.click(screen.getByRole('tab', { name: 'staff:roles' }));

  const row = await screen.findByRole('row', { name: /Sardor Alimov/ });
  // The role Select renders the *translated* option label only when
  // <Option value="sales_agent"> exists; without it antd falls back to the raw
  // value, so this asserts both the candidate wiring and the new option.
  expect(within(row).getByText('staff:sales_agent')).toBeInTheDocument();
});

describe('Cash reconciliation tab report window', () => {
  const pickerInputs = () => [...document.querySelectorAll('.ant-picker-range input')];
  const dayCell = (day) => document.querySelector(`.ant-picker-dropdown td[title="${day}"]`);
  const lastParams = () => staffService.getCashReconciliation.mock.calls.at(-1)[0];
  const cashPayload = (total = 2) => ({
    data: {
      data: {
        start_date: '2026-03-01',
        end_date: '2026-03-07',
        page: 1,
        per_page: 20,
        total,
        report: [],
        sessions: [],
        summary: {},
        grand_total_cash: 0,
      },
    },
  });

  const openCashTab = async () => {
    render(<StaffManagement />, { wrapper: createWrapper() });
    fireEvent.click(screen.getByRole('tab', { name: 'staff:cash_reconciliation' }));
    await waitFor(() => expect(pickerInputs().map((input) => input.value)).toEqual(['2026-03-01', '2026-03-07']));
  };

  it('first asks for the backend default window, then sends exactly the picked dates', async () => {
    staffService.getCashReconciliation.mockResolvedValue(cashPayload());
    await openCashTab();
    // No dates and no `period`: the 7-day default is the backend's decision alone.
    expect(staffService.getCashReconciliation.mock.calls[0][0]).toEqual({ page: 1, per_page: 20 });

    fireEvent.mouseDown(pickerInputs()[0]);
    fireEvent.click(pickerInputs()[0]);
    await waitFor(() => expect(dayCell('2026-03-02')).toBeTruthy());
    fireEvent.click(dayCell('2026-03-02'));
    fireEvent.click(dayCell('2026-03-05'));

    await waitFor(() => expect(lastParams()).toEqual({
      start_date: '2026-03-02',
      end_date: '2026-03-05',
      page: 1,
      per_page: 20,
    }));
  });

  it('pages the session table through the total the backend reports', async () => {
    staffService.getCashReconciliation.mockResolvedValue(cashPayload(45));
    await openCashTab();

    fireEvent.click(await screen.findByTitle('3'));

    await waitFor(() => expect(lastParams()).toEqual({ page: 3, per_page: 20 }));
  });
});
