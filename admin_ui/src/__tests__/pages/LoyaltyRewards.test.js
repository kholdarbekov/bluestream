import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import LoyaltyRewards from '../../pages/LoyaltyRewards';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getLoyaltyRewards: vi.fn(),
    getLoyaltyReward: vi.fn(),
    getLoyaltyPrograms: vi.fn(),
    createLoyaltyReward: vi.fn(),
    updateLoyaltyReward: vi.fn(),
    deleteLoyaltyReward: vi.fn(),
    getProducts: vi.fn(),
  },
}));

vi.mock('../../utils/exportUtils', () => ({
  __esModule: true,
  default: { exportLoyaltyRewards: vi.fn() },
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

describe('LoyaltyRewards page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getLoyaltyRewards.mockResolvedValue({ items: [], total: 0 });
    adminService.getLoyaltyPrograms.mockResolvedValue({ items: [], total: 0 });
    adminService.getLoyaltyReward.mockResolvedValue({});
    adminService.getProducts.mockResolvedValue({ data: { items: [{ id: 2, name: '19 litrlik suv' }] } });
  });

  // Regression: the reward-detail Drawer children are eagerly evaluated on render
  // even though the Drawer is closed. Before the fix they dereferenced
  // `rewardDetailQuery.data.*` with `data` undefined, throwing a TypeError on load.
  it('renders without dereferencing detail data before a reward is selected', async () => {
    render(<LoyaltyRewards />, { wrapper: createWrapper() });
    expect(await screen.findByText('Total Rewards')).toBeInTheDocument();
  });

  it('shows product dropdown and quantity field when reward_type is free_product', async () => {
    render(<LoyaltyRewards />, { wrapper: createWrapper() });

    // Wait for the page to be ready
    expect(await screen.findByText('Total Rewards')).toBeInTheDocument();

    // Open the Create modal
    const createButton = screen.getByRole('button', { name: /create reward/i });
    fireEvent.click(createButton);

    // Wait for the modal title to appear (the div.ant-modal-title, not the button)
    expect(await screen.findByRole('dialog')).toBeInTheDocument();

    // The modal opens with reward_type defaulting to 'discount', so change it to 'free_product'.
    // AntD Select renders a combobox; find the one for the reward_type field by its placeholder/label proximity.
    // We set reward_type value directly via the combobox role.
    // There are multiple comboboxes on the page; grab the one labelled 'Type' inside the modal.
    const dialog = screen.getByRole('dialog');

    // Find all combobox inputs inside the modal
    const comboboxes = dialog.querySelectorAll('.ant-select-selector');
    // The reward_type select is the second one (after program_id)
    const rewardTypeSelector = comboboxes[1];
    fireEvent.mouseDown(rewardTypeSelector);

    // Wait for the dropdown options to appear and pick 'Free Product'
    const freeProductOption = await screen.findByTitle('Free Product');
    fireEvent.click(freeProductOption);

    // Now the free_product fields should be rendered
    // Assert getProducts was called (the query fired)
    await waitFor(() => {
      expect(adminService.getProducts).toHaveBeenCalled();
    });

    // Assert the free_product fields rendered. 'Quantity' is unique to this block —
    // the reward_type combobox's selected value also reads 'Free Product', so we
    // assert on Quantity rather than the ambiguous 'Free Product' label.
    const quantityField = await screen.findByLabelText('Quantity');
    expect(quantityField).toBeInTheDocument();

    // Assert the product option populates the dropdown — verifies productOptions
    // derivation (data?.data?.items) is wired correctly.
    // Re-query selectors now that the free_product row has mounted.
    const updatedComboboxes = dialog.querySelectorAll('.ant-select-selector');
    // [0] = program_id, [1] = reward_type, [2] = free_product_id
    const productSelector = updatedComboboxes[2];
    fireEvent.mouseDown(productSelector);

    expect(await screen.findByTitle('19 litrlik suv')).toBeInTheDocument();
  });
});
