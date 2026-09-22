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
