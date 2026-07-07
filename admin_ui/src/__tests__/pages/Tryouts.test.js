import React from 'react';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Tryouts from '../../pages/Tryouts';
import adminService from '../../services/adminService';
import tryoutService from '../../services/tryoutService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getProducts: vi.fn(),
    getDeliveryPersonnel: vi.fn(),
  },
}));

vi.mock('../../services/tryoutService', () => ({
  __esModule: true,
  default: {
    getTryouts: vi.fn(),
    exportTryouts: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

describe('Tryouts page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getProducts.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getDeliveryPersonnel.mockResolvedValue({ data: { items: [], total: 0 } });
    tryoutService.getTryouts.mockResolvedValue({
      items: [{
        id: 1,
        tryout_number: 'TRY-1001',
        trial_contact: { full_name: 'Jane Doe', phone: '+998901234567' },
        status: 'active',
        outcome: 'pending',
        outstanding_bottles_total: 2,
        pickup_state: 'not_due',
        return_due_at: null,
      }],
      total: 1,
      summary: {},
    });
  });

  it('renders the page title and a try-out row using the translated (defaultValue) text', async () => {
    render(<Tryouts />, { wrapper: createWrapper() });
    expect(await screen.findByText('Try-outs')).toBeInTheDocument();
    expect(await screen.findByText('TRY-1001')).toBeInTheDocument();
    expect(screen.getAllByText('Actions').length).toBeGreaterThan(0);
  });
});
