import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import LoyaltyPrograms from '../../pages/LoyaltyPrograms';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getLoyaltyPrograms: vi.fn(),
    getLoyaltyTiers: vi.fn(),
    getLoyaltyStreakRules: vi.fn(),
    createLoyaltyProgram: vi.fn(),
    updateLoyaltyProgram: vi.fn(),
    deleteLoyaltyProgram: vi.fn(),
    createLoyaltyTier: vi.fn(),
    updateLoyaltyTier: vi.fn(),
    deleteLoyaltyTier: vi.fn(),
    createLoyaltyStreakRule: vi.fn(),
    updateLoyaltyStreakRule: vi.fn(),
    deleteLoyaltyStreakRule: vi.fn(),
  },
}));

vi.mock('../../utils/exportUtils', () => ({
  __esModule: true,
  default: { exportLoyaltyPrograms: vi.fn() },
}));

vi.mock('../../utils/dateUtils', () => ({
  formatDate: (value) => value || '-',
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

describe('LoyaltyPrograms page — Surprise Reward config', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getLoyaltyPrograms.mockResolvedValue({
      items: [{ id: 1, name: 'Default', is_default: true, is_active: true, member_count: 0, tier_count: 0 }],
      total: 1,
    });
    adminService.getLoyaltyTiers.mockResolvedValue({ items: [] });
    adminService.getLoyaltyStreakRules.mockResolvedValue({ streak_rules: [], streak_rule_count: 0 });
  });

  it('shows the surprise reward fields in the Create Program form', async () => {
    render(<LoyaltyPrograms />, { wrapper: createWrapper() });

    expect(await screen.findByText('Total Programs')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Create Program'));

    expect(await screen.findByText('Surprise Rewards')).toBeInTheDocument();
    expect(screen.getByText('Surprise Rewards Enabled')).toBeInTheDocument();
    expect(screen.getByText('Win Chance (%)')).toBeInTheDocument();
    expect(screen.getByText('Reward Amounts (comma-separated)')).toBeInTheDocument();
    expect(screen.getByText('Per-user Cooldown (days)')).toBeInTheDocument();
    expect(screen.getByText('Global Daily Cap')).toBeInTheDocument();
  });
});
