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

vi.mock('../../utils/dateUtils', () => ({ formatDate: (value) => value || '-' }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

describe('LoyaltyPrograms page — Tiers tab name translations', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getLoyaltyPrograms.mockResolvedValue({
      items: [{ id: 1, name: 'Default', is_default: true, is_active: true, member_count: 0, tier_count: 1 }],
      total: 1,
    });
    adminService.getLoyaltyStreakRules.mockResolvedValue({ streak_rules: [], streak_rule_count: 0 });
    adminService.getLoyaltyTiers.mockResolvedValue({
      items: [
        {
          id: 7,
          program_id: 1,
          name: 'Gold',
          display_order: 2,
          min_points: 5000,
          max_points: 11999,
          points_multiplier: 1.3,
          discount_percentage: 3,
          points_range: '5,000 - 11,999',
          is_active: true,
          translations: { name: { en: 'Gold', ru: 'Золото', uz: 'Oltin' } },
        },
      ],
    });
    adminService.createLoyaltyTier.mockResolvedValue({ id: 8 });
    adminService.updateLoyaltyTier.mockResolvedValue({ id: 7 });
  });

  const goToTiersTab = async () => {
    render(<LoyaltyPrograms />, { wrapper: createWrapper() });
    await screen.findByText('Total Programs');
    fireEvent.click(screen.getByRole('tab', { name: 'Tiers' }));
    await screen.findByText('Gold');
  };

  it('creating a tier sends per-language name translations (en/ru/uz)', async () => {
    await goToTiersTab();

    fireEvent.click(screen.getByRole('button', { name: /Create Tier/ }));
    const dialog = await screen.findByRole('dialog');

    fireEvent.change(within(dialog).getByLabelText('Tier Name'), { target: { value: 'Diamond' } });
    fireEvent.change(within(dialog).getByLabelText('Name (RU)'), { target: { value: 'Алмаз' } });
    fireEvent.change(within(dialog).getByLabelText('Name (UZ)'), { target: { value: 'Olmos' } });
    fireEvent.change(within(dialog).getByLabelText('Minimum AquaCoins'), { target: { value: '20000' } });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(adminService.createLoyaltyTier).toHaveBeenCalled());
    const payload = adminService.createLoyaltyTier.mock.calls[0][0];
    expect(payload.name).toBe('Diamond');
    expect(payload.translations.name).toEqual(
      expect.objectContaining({ en: 'Diamond', ru: 'Алмаз', uz: 'Olmos' })
    );
  });

  it('editing a tier pre-fills the RU/UZ name inputs from translations', async () => {
    await goToTiersTab();

    // Click the edit (pencil) action on the Gold row specifically.
    const goldRow = screen.getByText('Gold').closest('tr');
    fireEvent.click(goldRow.querySelector('.anticon-edit').closest('button'));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).getByLabelText('Name (RU)')).toHaveValue('Золото');
    expect(within(dialog).getByLabelText('Name (UZ)')).toHaveValue('Oltin');
  });
});
