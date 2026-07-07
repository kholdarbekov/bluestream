import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import ProductCategories from '../../pages/ProductCategories';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getCategories: vi.fn(),
    createCategory: vi.fn(),
    updateCategory: vi.fn(),
    deleteCategory: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

describe('ProductCategories page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getCategories.mockResolvedValue({
      data: { items: [{ id: 1, name: 'Bottled Water', sort_order: 0, product_count: 3, is_active: true, created_at: '2026-06-01T00:00:00Z' }] },
      meta: { total: 1 },
    });
  });

  it('renders the summary stats and a category row using the translated (defaultValue) text', async () => {
    render(<ProductCategories />, { wrapper: createWrapper() });
    expect(await screen.findByText('Total Categories')).toBeInTheDocument();
    expect(await screen.findByText('Bottled Water')).toBeInTheDocument();
    expect(screen.getAllByText('Active').length).toBeGreaterThan(0);
  });

  it('opens the create modal with translated tab labels', async () => {
    render(<ProductCategories />, { wrapper: createWrapper() });
    await screen.findByText('Bottled Water');
    fireEvent.click(screen.getByRole('button', { name: /add category/i }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Add New Category')).toBeInTheDocument();
    expect(within(dialog).getByText('Uzbek (Default)')).toBeInTheDocument();
  });
});
