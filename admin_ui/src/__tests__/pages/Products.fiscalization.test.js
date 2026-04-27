import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Products from '../../pages/Products';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService');
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallbackOrOptions, maybeOptions) => {
      const fallback =
        typeof fallbackOrOptions === 'string'
          ? fallbackOrOptions
          : fallbackOrOptions?.defaultValue;
      const options =
        typeof fallbackOrOptions === 'string'
          ? maybeOptions
          : fallbackOrOptions;

      if (options?.count !== undefined && typeof fallback === 'string') {
        return fallback.replace('{{count}}', String(options.count));
      }
      return fallback || key;
    },
  }),
}));

vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    Dropdown: ({ menu, children }) => (
      <div>
        {children}
        {menu?.items
          ?.filter((item) => item && item.type !== 'divider' && item.onClick)
          .map((item) => (
            <button key={item.key} onClick={item.onClick} type="button" disabled={item.disabled}>
              {typeof item.label === 'string' ? item.label : item.key}
            </button>
          ))}
      </div>
    ),
  };
});

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

describe('Products fiscal workflow', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    adminService.getCategories.mockResolvedValue({
      data: {
        items: [{ id: 8, name: 'Water', is_active: true }],
      },
    });

    adminService.getProducts.mockResolvedValue({
      data: {
        items: [
          {
            id: 44,
            name: 'Aqua Element 18.9L',
            sku: 'AQUA-19',
            category_id: 8,
            price: 18000,
            base_price: 18000,
            stock_quantity: 24,
            status: 'active',
            created_at: '2026-03-11T10:00:00+00:00',
            image_url: null,
            images: [],
            barcode: '4780011111111',
            spic: 'SPIC-19L',
            package_code: 'PACK-19L',
            units: 'pcs',
            vat_percent: 12,
            fiscalization_enabled: true,
            requires_marking_codes: true,
            marking_code_counts: { available: 3, reserved: 1, used: 4, archived: 0 },
            marking_codes_low_stock: true,
            marking_codes_low_stock_threshold: 10,
            missing_required_fields: [],
          },
        ],
      },
      meta: {
        total: 1,
      },
    });

    adminService.listProductMarkingCodes.mockResolvedValue({
      data: {
        items: [
          {
            id: 501,
            code: 'MARK-001',
            status: 'available',
            notes: 'imported',
            created_at: '2026-03-10T09:00:00+00:00',
            used_at: null,
          },
        ],
        total: 1,
        summary: { available: 3, reserved: 1, used: 4, archived: 0 },
      },
    });
  });

  it('opens product details and loads marking-code inventory', async () => {
    const user = userEvent.setup();
    render(<Products />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getProducts).toHaveBeenCalled();
    });

    await user.click(await screen.findByText(/view_details|View Details/i));
    await user.click(await screen.findByText(/marking_codes|Marking Codes/i));

    await waitFor(() => {
      expect(adminService.listProductMarkingCodes).toHaveBeenCalledWith(44, expect.any(Object));
    });

    expect(await screen.findByText('MARK-001')).toBeInTheDocument();
    expect(screen.getByText(/add_marking_codes|Add Codes/i)).toBeInTheDocument();
    expect(screen.getByText('Available')).toBeInTheDocument();
  });
});
