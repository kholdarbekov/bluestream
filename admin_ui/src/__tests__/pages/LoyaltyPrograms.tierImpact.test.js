import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Modal } from 'antd';

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

const goldTier = {
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
};

// A 409 with this shape is Task 7's "impact confirmation required" response:
// the server refuses a threshold raise or a deactivation once, naming how many
// members already hold the badge, and expects a resubmit with confirm_impact.
const impactError = ({ message, stranded_members, tier, new_min_points }) =>
  Object.assign(new Error('conflict'), {
    response: {
      status: 409,
      data: {
        message,
        data: { error_code: 'impact_confirmation_required', stranded_members, tier, new_min_points },
      },
    },
  });

// A 422 with this shape is the ladder validator's threshold_gap: the edit
// leaves a points range no tier covers. It changes no pricing, so unlike
// threshold_overlap/threshold_invalid it is also waivable via confirm_impact.
const gapError = ({ message }) =>
  Object.assign(new Error('unprocessable'), {
    response: {
      status: 422,
      data: {
        message,
        data: { error_code: 'threshold_gap' },
      },
    },
  });

describe('LoyaltyPrograms page — tier update impact confirmation', () => {
  // Modal.confirm portals outside React Testing Library's root, so an
  // un-dismissed confirmation from one test would otherwise leak a second
  // `role="dialog"` node into the next test.
  afterEach(() => {
    Modal.destroyAll();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getLoyaltyPrograms.mockResolvedValue({
      items: [{ id: 1, name: 'Default', is_default: true, is_active: true, member_count: 0, tier_count: 1 }],
      total: 1,
    });
    adminService.getLoyaltyStreakRules.mockResolvedValue({ streak_rules: [], streak_rule_count: 0 });
    adminService.getLoyaltyTiers.mockResolvedValue({ items: [goldTier] });
  });

  const goToTiersTab = async () => {
    render(<LoyaltyPrograms />, { wrapper: createWrapper() });
    await screen.findByText('Total Programs');
    fireEvent.click(screen.getByRole('tab', { name: 'Tiers' }));
    await screen.findByText('Gold');
  };

  const openEditDialog = async () => {
    await goToTiersTab();
    const goldRow = screen.getByText('Gold').closest('tr');
    fireEvent.click(goldRow.querySelector('.anticon-edit').closest('button'));
    return screen.findByRole('dialog');
  };

  // Modal.confirm mounts its own dialog root (class `ant-modal-confirm`) as a
  // sibling of the edit-tier form Modal, so once the confirmation is up there
  // are two `role="dialog"` nodes on screen — this scopes to the confirm one.
  // (It also repeats its title text once in the header and once in the body,
  // so scope-then-assert-on-content, not a title text lookup.)
  const findConfirmDialog = async () => {
    await waitFor(() => expect(document.querySelector('.ant-modal-confirm')).toBeTruthy());
    return document.querySelector('.ant-modal-confirm');
  };

  it('a 409 impact_confirmation_required opens a confirm dialog instead of failing silently', async () => {
    adminService.updateLoyaltyTier.mockRejectedValueOnce(
      impactError({
        message: '3 member(s) hold the Gold badge below the new threshold of 9000.',
        stranded_members: 3,
        tier: 'Gold',
        new_min_points: 9000,
      }),
    );

    const dialog = await openEditDialog();
    fireEvent.change(within(dialog).getByLabelText('Minimum AquaCoins'), { target: { value: '9000' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update' }));

    await waitFor(() => expect(adminService.updateLoyaltyTier).toHaveBeenCalledTimes(1));
    const confirmDialog = await findConfirmDialog();
    expect(
      within(confirmDialog).getByText('3 member(s) hold the Gold badge below the new threshold of 9000.'),
    ).toBeInTheDocument();
  });

  it('confirming re-submits with the original values plus confirm_impact: true', async () => {
    adminService.updateLoyaltyTier.mockRejectedValueOnce(
      impactError({ message: undefined, stranded_members: 3, tier: 'Gold', new_min_points: 9000 }),
    );
    adminService.updateLoyaltyTier.mockResolvedValueOnce({ id: 7 });

    const dialog = await openEditDialog();
    fireEvent.change(within(dialog).getByLabelText('Minimum AquaCoins'), { target: { value: '9000' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update' }));

    await waitFor(() => expect(adminService.updateLoyaltyTier).toHaveBeenCalledTimes(1));
    const confirmDialog = await findConfirmDialog();
    fireEvent.click(within(confirmDialog).getByRole('button', { name: 'Proceed' }));

    await waitFor(() => expect(adminService.updateLoyaltyTier).toHaveBeenCalledTimes(2));
    const [tierId, secondValues] = adminService.updateLoyaltyTier.mock.calls[1];
    expect(tierId).toBe(7);
    expect(secondValues).toEqual(
      expect.objectContaining({
        name: 'Gold',
        min_points: 9000,
        confirm_impact: true,
      }),
    );
  });

  it('dismissing the confirmation dialog does not re-submit', async () => {
    adminService.updateLoyaltyTier.mockRejectedValueOnce(
      impactError({ message: undefined, stranded_members: 3, tier: 'Gold', new_min_points: 9000 }),
    );

    const dialog = await openEditDialog();
    fireEvent.change(within(dialog).getByLabelText('Minimum AquaCoins'), { target: { value: '9000' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update' }));

    await waitFor(() => expect(adminService.updateLoyaltyTier).toHaveBeenCalledTimes(1));
    const confirmDialog = await findConfirmDialog();
    fireEvent.click(within(confirmDialog).getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(document.querySelector('.ant-modal-confirm')).not.toBeInTheDocument());
    expect(adminService.updateLoyaltyTier).toHaveBeenCalledTimes(1);
  });

  it('the deactivation branch (new_min_points absent) never prints the literal "null"', async () => {
    adminService.updateLoyaltyTier.mockRejectedValueOnce(
      impactError({ message: undefined, stranded_members: 2, tier: 'Gold', new_min_points: null }),
    );

    const dialog = await openEditDialog();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update' }));

    await waitFor(() => expect(adminService.updateLoyaltyTier).toHaveBeenCalledTimes(1));
    const confirmDialog = await findConfirmDialog();
    expect(within(confirmDialog).getByText(/Deactivating this tier will affect them/)).toBeInTheDocument();
    expect(confirmDialog.textContent).not.toMatch(/\bnull\b/);
  });

  it('a 422 threshold_gap on update opens a confirm dialog explaining no pricing changes, and re-submits with confirm_impact: true', async () => {
    adminService.updateLoyaltyTier.mockRejectedValueOnce(
      gapError({ message: 'Gold ends at 11999 and Silver starts at 15000 — points between map to no tier.' }),
    );
    adminService.updateLoyaltyTier.mockResolvedValueOnce({ id: 7 });

    const dialog = await openEditDialog();
    fireEvent.change(within(dialog).getByLabelText('Minimum AquaCoins'), { target: { value: '5000' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update' }));

    await waitFor(() => expect(adminService.updateLoyaltyTier).toHaveBeenCalledTimes(1));
    const confirmDialog = await findConfirmDialog();
    expect(
      within(confirmDialog).getByText('Gold ends at 11999 and Silver starts at 15000 — points between map to no tier.'),
    ).toBeInTheDocument();
    expect(
      within(confirmDialog).getByText(/only affects how the tier table is displayed/),
    ).toBeInTheDocument();

    fireEvent.click(within(confirmDialog).getByRole('button', { name: 'Proceed' }));

    await waitFor(() => expect(adminService.updateLoyaltyTier).toHaveBeenCalledTimes(2));
    const [, secondValues] = adminService.updateLoyaltyTier.mock.calls[1];
    expect(secondValues).toEqual(expect.objectContaining({ min_points: 5000, confirm_impact: true }));
  });

  it('a 422 threshold_overlap on update is reported as an error, not offered a confirm dialog', async () => {
    adminService.updateLoyaltyTier.mockRejectedValueOnce(
      Object.assign(new Error('unprocessable'), {
        response: {
          status: 422,
          data: {
            message: 'Silver starts at 0, not above Bronze\'s 0.',
            data: { error_code: 'threshold_overlap' },
          },
        },
      }),
    );

    const dialog = await openEditDialog();
    fireEvent.change(within(dialog).getByLabelText('Minimum AquaCoins'), { target: { value: '0' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update' }));

    await waitFor(() => expect(adminService.updateLoyaltyTier).toHaveBeenCalledTimes(1));
    // No confirm dialog is offered for a hard failure, and no second retry fires.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.querySelector('.ant-modal-confirm')).not.toBeInTheDocument();
    expect(adminService.updateLoyaltyTier).toHaveBeenCalledTimes(1);
  });
});

describe('LoyaltyPrograms page — tier delete impact confirmation', () => {
  afterEach(() => {
    Modal.destroyAll();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getLoyaltyPrograms.mockResolvedValue({
      items: [{ id: 1, name: 'Default', is_default: true, is_active: true, member_count: 0, tier_count: 1 }],
      total: 1,
    });
    adminService.getLoyaltyStreakRules.mockResolvedValue({ streak_rules: [], streak_rule_count: 0 });
    adminService.getLoyaltyTiers.mockResolvedValue({ items: [goldTier] });
  });

  const goToTiersTab = async () => {
    render(<LoyaltyPrograms />, { wrapper: createWrapper() });
    await screen.findByText('Total Programs');
    fireEvent.click(screen.getByRole('tab', { name: 'Tiers' }));
    await screen.findByText('Gold');
  };

  const clickDeleteAndConfirmIntent = async () => {
    await goToTiersTab();
    const goldRow = screen.getByText('Gold').closest('tr');
    fireEvent.click(goldRow.querySelector('.anticon-delete').closest('button'));
    const initialDialog = await screen.findByRole('dialog');
    fireEvent.click(within(initialDialog).getByRole('button', { name: 'OK' }));
  };

  it('deleting a tier that members hold opens a confirm dialog instead of silently doing nothing', async () => {
    adminService.deleteLoyaltyTier.mockRejectedValueOnce(
      impactError({
        message: '3 member(s) hold the Gold badge.',
        stranded_members: 3,
        tier: 'Gold',
        new_min_points: null,
      }),
    );

    await clickDeleteAndConfirmIntent();

    await waitFor(() => expect(adminService.deleteLoyaltyTier).toHaveBeenCalledTimes(1));
    expect(adminService.deleteLoyaltyTier).toHaveBeenCalledWith(7, undefined);
    await screen.findByText('3 member(s) hold the Gold badge.');
    expect(screen.getByRole('button', { name: 'Proceed' })).toBeInTheDocument();
  });

  it('confirming the delete-impact dialog re-submits the delete with confirm_impact: true', async () => {
    adminService.deleteLoyaltyTier.mockRejectedValueOnce(
      impactError({ message: undefined, stranded_members: 3, tier: 'Gold', new_min_points: null }),
    );
    adminService.deleteLoyaltyTier.mockResolvedValueOnce({});

    await clickDeleteAndConfirmIntent();

    await waitFor(() => expect(adminService.deleteLoyaltyTier).toHaveBeenCalledTimes(1));
    const proceedButton = await screen.findByRole('button', { name: 'Proceed' });
    fireEvent.click(proceedButton);

    await waitFor(() => expect(adminService.deleteLoyaltyTier).toHaveBeenCalledTimes(2));
    expect(adminService.deleteLoyaltyTier).toHaveBeenNthCalledWith(2, 7, { confirm_impact: true });
  });

  it('a 422 threshold_gap on delete opens a confirm dialog and re-submits with confirm_impact: true', async () => {
    adminService.deleteLoyaltyTier.mockRejectedValueOnce(
      gapError({ message: 'Gold is the top tier and must have no upper bound.' }),
    );
    adminService.deleteLoyaltyTier.mockResolvedValueOnce({});

    await clickDeleteAndConfirmIntent();

    await waitFor(() => expect(adminService.deleteLoyaltyTier).toHaveBeenCalledTimes(1));
    await screen.findByText('Gold is the top tier and must have no upper bound.');
    fireEvent.click(screen.getByRole('button', { name: 'Proceed' }));

    await waitFor(() => expect(adminService.deleteLoyaltyTier).toHaveBeenCalledTimes(2));
    expect(adminService.deleteLoyaltyTier).toHaveBeenNthCalledWith(2, 7, { confirm_impact: true });
  });
});
