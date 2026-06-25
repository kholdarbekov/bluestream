import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import LoyaltyPrograms from '../../pages/LoyaltyPrograms';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getLoyaltyPrograms: vi.fn(),
    getLoyaltyTiers: vi.fn(),
    getLoyaltyStreakRules: vi.fn(),
    createLoyaltyStreakRule: vi.fn(),
    updateLoyaltyStreakRule: vi.fn(),
    deleteLoyaltyStreakRule: vi.fn(),
    createLoyaltyProgram: vi.fn(),
    updateLoyaltyProgram: vi.fn(),
    deleteLoyaltyProgram: vi.fn(),
    createLoyaltyTier: vi.fn(),
    updateLoyaltyTier: vi.fn(),
    deleteLoyaltyTier: vi.fn(),
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

describe('LoyaltyPrograms page — Streak Rules tab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getLoyaltyPrograms.mockResolvedValue({
      items: [{ id: 1, name: 'Default', is_default: true, is_active: true, member_count: 0, tier_count: 0 }],
      total: 1,
    });
    adminService.getLoyaltyTiers.mockResolvedValue({ items: [] });
    adminService.getLoyaltyStreakRules.mockResolvedValue({
      streak_rules: [
        {
          id: 1,
          name: '3 in 30',
          required_orders: 3,
          window_days: 30,
          bonus_points: 300,
          min_order_amount: null,
          is_active: true,
          translations: { name: {} },
        },
      ],
      streak_rule_count: 1,
    });
    adminService.createLoyaltyStreakRule.mockResolvedValue({ id: 2 });
  });

  it('renders the streak rule row after switching to the Streak Rules tab', async () => {
    render(<LoyaltyPrograms />, { wrapper: createWrapper() });

    // Wait for the page to load programs (auto-selects the default program)
    expect(await screen.findByText('Total Programs')).toBeInTheDocument();

    // Switch to the Streak Rules tab
    const streakTab = await screen.findByText('Streak Rules');
    fireEvent.click(streakTab);

    // The streak rule name should be visible in the table
    expect(await screen.findByText('3 in 30')).toBeInTheDocument();
  });

  it('calls getLoyaltyStreakRules with the auto-selected program id', async () => {
    render(<LoyaltyPrograms />, { wrapper: createWrapper() });

    // Wait for programs to resolve and trigger the streak query
    await screen.findByText('Total Programs');

    // Switch to Streak Rules tab to make results visible
    const streakTab = await screen.findByText('Streak Rules');
    fireEvent.click(streakTab);

    await screen.findByText('3 in 30');

    expect(adminService.getLoyaltyStreakRules).toHaveBeenCalledWith(
      expect.objectContaining({ program_id: 1 })
    );
  });

  it('creating a streak rule mirrors the EN name into translations (en/ru/uz)', async () => {
    render(<LoyaltyPrograms />, { wrapper: createWrapper() });
    await screen.findByText('Total Programs');
    fireEvent.click(await screen.findByText('Streak Rules'));
    await screen.findByText('3 in 30');

    fireEvent.click(screen.getByRole('button', { name: /Add Streak Rule/ }));
    const dialog = await screen.findByRole('dialog');

    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: '5 in 30' } });
    fireEvent.change(within(dialog).getByLabelText('Name (RU)'), { target: { value: '5 за 30' } });
    fireEvent.change(within(dialog).getByLabelText('Name (UZ)'), { target: { value: '30 kunda 5' } });
    fireEvent.change(within(dialog).getByLabelText('Required Orders'), { target: { value: '5' } });
    fireEvent.change(within(dialog).getByLabelText('Window (days)'), { target: { value: '30' } });
    fireEvent.change(within(dialog).getByLabelText('Bonus AquaCoins'), { target: { value: '500' } });

    fireEvent.click(within(dialog).getByRole('button', { name: /^Create$/ }));

    await waitFor(() => expect(adminService.createLoyaltyStreakRule).toHaveBeenCalled());
    const payload = adminService.createLoyaltyStreakRule.mock.calls[0][0];
    expect(payload.name).toBe('5 in 30');
    // EN mirrors the canonical name so the public /loyalty-guide page resolves
    // English instead of falling back to the Uzbek translation.
    expect(payload.translations.name).toEqual(
      expect.objectContaining({ en: '5 in 30', ru: '5 за 30', uz: '30 kunda 5' })
    );
  });
});
