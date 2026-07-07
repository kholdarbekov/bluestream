import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import TimeSlots from '../../pages/TimeSlots';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getTimeSlots: vi.fn(),
    createTimeSlot: vi.fn(),
    updateTimeSlot: vi.fn(),
    deleteTimeSlot: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

describe('TimeSlots page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getTimeSlots.mockResolvedValue({
      data: {
        items: [{
          id: 1, name: 'Morning', start_time: '08:00', end_time: '12:00',
          max_orders: 50, delivery_fee: 10000, is_premium: false, premium_fee: 0,
          is_active: true, available_days: [0, 1, 2, 3, 4, 5, 6],
        }],
        total: 1,
      },
    });
  });

  it('renders the page title and a time slot row using the translated (defaultValue) text', async () => {
    render(<TimeSlots />, { wrapper: createWrapper() });
    expect(await screen.findByText('Delivery Time Slots Management')).toBeInTheDocument();
    expect(await screen.findByText('Morning')).toBeInTheDocument();
    expect(screen.getAllByText('All Days').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Active').length).toBeGreaterThan(0);
  });

  it('opens the create modal with a translated Name field', async () => {
    render(<TimeSlots />, { wrapper: createWrapper() });
    await screen.findByText('Morning');
    fireEvent.click(screen.getByRole('button', { name: /create time slot/i }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Name')).toBeInTheDocument();
  });
});
