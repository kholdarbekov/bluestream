import React from 'react';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import LoyaltyMembers from '../../pages/LoyaltyMembers';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getLoyaltyMembers: vi.fn(),
    getLoyaltyMember: vi.fn(),
    getLoyaltyPrograms: vi.fn(),
  },
}));

vi.mock('../../utils/exportUtils', () => ({
  __esModule: true,
  default: { exportLoyaltyMembers: vi.fn() },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => (opts && opts.defaultValue) || key,
  }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

describe('LoyaltyMembers page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getLoyaltyMembers.mockResolvedValue({
      items: [],
      total: 0,
      summary: {
        total_members: 0,
        total_points_in_circulation: 0,
        average_points_balance: 0,
      },
    });
    adminService.getLoyaltyPrograms.mockResolvedValue({ items: [], total: 0 });
    adminService.getLoyaltyMember.mockResolvedValue({});
  });

  // Regression: the member-detail Drawer children are eagerly evaluated when the
  // page renders, even though the Drawer is closed and DataView would not render
  // them. Before the fix they dereferenced `memberDetailQuery.data.member.*` with
  // `data` undefined, throwing "Cannot read properties of undefined (reading 'member')".
  it('renders without dereferencing detail data before a member is selected', async () => {
    render(<LoyaltyMembers />, { wrapper: createWrapper() });
    expect(await screen.findByText('Total Members')).toBeInTheDocument();
  });
});
