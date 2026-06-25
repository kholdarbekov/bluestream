import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import LoyaltyPrograms from '../../pages/LoyaltyPrograms';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getLoyaltyPrograms: vi.fn(),
    getLoyaltyTiers: vi.fn(),
    getLoyaltyStreakRules: vi.fn(),
    getLoyaltyConsecutiveStrikeRules: vi.fn(),
    createLoyaltyConsecutiveStrikeRule: vi.fn(),
    updateLoyaltyConsecutiveStrikeRule: vi.fn(),
    deleteLoyaltyConsecutiveStrikeRule: vi.fn(),
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

describe('LoyaltyPrograms — Consecutive Strikes tab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getLoyaltyPrograms.mockResolvedValue({
      items: [{ id: 1, name: 'Default', is_default: true, is_active: true, member_count: 0, tier_count: 0 }],
      total: 1,
    });
    adminService.getLoyaltyTiers.mockResolvedValue({ items: [] });
    adminService.getLoyaltyStreakRules.mockResolvedValue({
      streak_rules: [
        { id: 7, name: '3 in 30', required_orders: 3, window_days: 30, bonus_points: 300, min_order_amount: null, is_active: true, translations: { name: {} } },
      ],
      streak_rule_count: 1,
    });
    adminService.getLoyaltyConsecutiveStrikeRules.mockResolvedValue({
      consecutive_strike_rules: [
        { id: 1, name: '6-in-a-row', required_consecutive: 6, combine_mode: 'all', bonus_points: 1000, is_active: true, strike_rule_ids: [7], strikes: [{ id: 7, name: '3 in 30' }], translations: { name: {} } },
      ],
      count: 1,
    });
    adminService.createLoyaltyConsecutiveStrikeRule.mockResolvedValue({ id: 2 });
  });

  it('renders the consecutive-strike row after switching tabs', async () => {
    render(<LoyaltyPrograms />, { wrapper: createWrapper() });
    expect(await screen.findByText('Total Programs')).toBeInTheDocument();
    fireEvent.click(await screen.findByText('Consecutive Strikes'));
    expect(await screen.findByText('6-in-a-row')).toBeInTheDocument();
  });

  it('submits the exact create payload incl. strike_rule_ids and combine_mode', async () => {
    render(<LoyaltyPrograms />, { wrapper: createWrapper() });
    await screen.findByText('Total Programs');
    fireEvent.click(await screen.findByText('Consecutive Strikes'));
    fireEvent.click(await screen.findByText('Add Consecutive Strike'));

    const dialog = await screen.findByRole('dialog');

    // Fill in text/number fields
    fireEvent.change(dialog.querySelector('#name'), { target: { value: 'Champ' } });
    fireEvent.change(dialog.querySelector('#required_consecutive'), { target: { value: '6' } });
    fireEvent.change(dialog.querySelector('#bonus_points'), { target: { value: '1000' } });

    // combine_mode defaults to 'all' (set in setFieldsValue on modal open),
    // so we do not need to change it — the payload builder uses values.combine_mode || 'all'.

    // Select strike rule id=7 via the antd multi-Select:
    // open the selector (last .ant-select-selector in the dialog is strike_rule_ids multi-select)
    const selectors = dialog.querySelectorAll('.ant-select-selector');
    // selectors order: [0]=combine_mode, [1]=strike_rule_ids
    const strikeRuleSelector = selectors[selectors.length - 1];
    fireEvent.mouseDown(strikeRuleSelector);

    // Wait for the dropdown option to appear and click it by title
    const option = await screen.findByTitle('3 in 30');
    fireEvent.click(option);

    fireEvent.click(screen.getByRole('button', { name: /create/i }));
    await waitFor(() => expect(adminService.createLoyaltyConsecutiveStrikeRule).toHaveBeenCalled());
    const payload = adminService.createLoyaltyConsecutiveStrikeRule.mock.calls[0][0];
    expect(payload).toMatchObject({
      name: 'Champ',
      required_consecutive: 6,
      bonus_points: 1000,
      combine_mode: 'all',
      strike_rule_ids: [7],
    });
  });
});
