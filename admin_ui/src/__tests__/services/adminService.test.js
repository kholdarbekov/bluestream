import adminService from '../../services/adminService';
import api from '../../services/api';

// Mock the API
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: jest.fn(),
    post: jest.fn(),
    put: jest.fn(),
    delete: jest.fn()
  },
  getCookie: jest.fn()
}));

describe('AdminService', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe('getDashboardData', () => {
    it('fetches dashboard data successfully', async () => {
      const mockData = {
        total_revenue: 100000,
        total_orders: 500,
        total_customers: 200
      };

      api.get.mockResolvedValue({ data: mockData });

      const result = await adminService.getDashboardData();

      expect(api.get).toHaveBeenCalledWith('/admin/dashboard', { params: {} });
      expect(result).toEqual(mockData);
    });

    it('handles API errors', async () => {
      api.get.mockRejectedValue(new Error('Network error'));

      await expect(adminService.getDashboardData()).rejects.toThrow('Network error');
    });
  });

  describe('getUsers', () => {
    it('fetches users with default parameters', async () => {
      const mockData = {
        users: [
          { id: 1, name: 'John Doe', email: 'john@example.com' }
        ],
        pagination: { total: 1, page: 1, per_page: 20 }
      };

      api.get.mockResolvedValue({ data: mockData });

      const result = await adminService.getUsers();

      expect(api.get).toHaveBeenCalledWith('/admin/users', { params: {} });
      expect(result).toEqual(mockData);
    });

    it('fetches users with custom parameters', async () => {
      const params = { page: 2, per_page: 10, search: 'john' };
      const mockData = { users: [], pagination: { total: 0, page: 2, per_page: 10 } };

      api.get.mockResolvedValue({ data: mockData });

      const result = await adminService.getUsers(params);

      expect(api.get).toHaveBeenCalledWith('/admin/users', { params });
      expect(result).toEqual(mockData);
    });
  });

  describe('updateUserStatus', () => {
    it('updates user status successfully', async () => {
      const mockResponse = { message: 'User status updated' };
      api.put.mockResolvedValue({ data: mockResponse });

      const result = await adminService.updateUserStatus(1, 'inactive', 'Account suspended');

      expect(api.put).toHaveBeenCalledWith('/admin/users/1/status', {
        status: 'inactive',
        reason: 'Account suspended'
      });
      expect(result).toEqual(mockResponse);
    });

    it('updates user profile successfully', async () => {
      const mockResponse = { message: 'User updated' };
      const payload = {
        first_name: 'Acme',
        phone: '+998901234567',
        user_type: 'entity',
        company_name: 'Acme Water'
      };
      api.put.mockResolvedValue({ data: mockResponse });

      const result = await adminService.updateUser(1, payload);

      expect(api.put).toHaveBeenCalledWith('/admin/users/1', payload);
      expect(result).toEqual(mockResponse);
    });
  });

  describe('getOrders', () => {
    it('fetches orders successfully', async () => {
      const mockData = {
        orders: [
          { id: 1, order_number: 'ORD-001', total_amount: 100.50 }
        ],
        pagination: { total: 1, page: 1, per_page: 20 }
      };

      api.get.mockResolvedValue({ data: mockData });

      const result = await adminService.getOrders();

      expect(api.get).toHaveBeenCalledWith('/admin/orders', { params: {} });
      expect(result).toEqual(mockData);
    });
  });

  describe('corporate contracts', () => {
    it('fetches corporate contracts successfully', async () => {
      const mockData = {
        data: {
          items: [{ id: 1, contract_number: 'CTR-001' }]
        }
      };

      api.get.mockResolvedValue({ data: mockData });

      const result = await adminService.getCorporateContracts({ page: 1, per_page: 20 });

      expect(api.get).toHaveBeenCalledWith('/admin/corporate/contracts', {
        params: { page: 1, per_page: 20 }
      });
      expect(result).toEqual(mockData);
    });

    it('updates contract prices successfully', async () => {
      const prices = [{ product_id: 1, unit_price: 12000 }];
      const mockResponse = { message: 'ok' };

      api.put.mockResolvedValue({ data: mockResponse });

      const result = await adminService.updateCorporateContractPrices(7, prices);

      expect(api.put).toHaveBeenCalledWith('/admin/corporate/contracts/7/prices', { prices });
      expect(result).toEqual(mockResponse);
    });

    it('previews contract overlaps successfully', async () => {
      const payload = { user_id: 1, prices: [{ product_id: 2, is_active: true }] };
      const mockResponse = { data: { preview: { has_conflicts: false } } };

      api.post.mockResolvedValue({ data: mockResponse });

      const result = await adminService.previewCorporateContractOverlaps(payload);

      expect(api.post).toHaveBeenCalledWith('/admin/corporate/contracts/overlap-preview', payload);
      expect(result).toEqual(mockResponse);
    });

    it('submits corporate topup successfully', async () => {
      const payload = { units: 10, amount: 100000, transfer_ref: 'BANK-1' };
      const mockResponse = { message: 'topup created' };

      api.post.mockResolvedValue({ data: mockResponse });

      const result = await adminService.topupCorporateContract(4, payload);

      expect(api.post).toHaveBeenCalledWith('/admin/corporate/contracts/4/prepayments/topup', payload);
      expect(result).toEqual(mockResponse);
    });
  });

  describe('updateOrderStatus', () => {
    it('updates order status successfully', async () => {
      const mockResponse = { message: 'Order status updated' };
      api.put.mockResolvedValue({ data: mockResponse });

      const result = await adminService.updateOrderStatus(1, 'shipped', 'Package shipped via FedEx');

      expect(api.put).toHaveBeenCalledWith('/admin/orders/1/status', {
        status: 'shipped',
        notes: 'Package shipped via FedEx'
      });
      expect(result).toEqual(mockResponse);
    });
  });

  describe('getProducts', () => {
    it('fetches products successfully', async () => {
      const mockData = {
        products: [
          { id: 1, name: 'Water Bottle', price: 25.99, stock_quantity: 100 }
        ],
        pagination: { total: 1, page: 1, per_page: 20 }
      };

      api.get.mockResolvedValue({ data: mockData });

      const result = await adminService.getProducts();

      expect(api.get).toHaveBeenCalledWith('/admin/products', { params: {} });
      expect(result).toEqual(mockData);
    });
  });

  describe('createProduct', () => {
    it('creates product successfully', async () => {
      const productData = {
        name: 'New Water Bottle',
        price: 29.99,
        stock_quantity: 50,
        category: 'water_bottles'
      };
      const mockResponse = { id: 1, ...productData };

      api.post.mockResolvedValue({ data: mockResponse });

      const result = await adminService.createProduct(productData);

      expect(api.post).toHaveBeenCalledWith('/admin/products', productData);
      expect(result).toEqual(mockResponse);
    });
  });

  describe('updateProduct', () => {
    it('updates product successfully', async () => {
      const productData = { name: 'Updated Water Bottle', price: 35.99 };
      const mockResponse = { id: 1, ...productData };

      api.put.mockResolvedValue({ data: mockResponse });

      const result = await adminService.updateProduct(1, productData);

      expect(api.put).toHaveBeenCalledWith('/admin/products/1', productData);
      expect(result).toEqual(mockResponse);
    });
  });

  describe('deleteProduct', () => {
    it('deletes product successfully', async () => {
      const mockResponse = { message: 'Product deleted successfully' };

      api.delete.mockResolvedValue({ data: mockResponse });

      const result = await adminService.deleteProduct(1);

      expect(api.delete).toHaveBeenCalledWith('/admin/products/1');
      expect(result).toEqual(mockResponse);
    });
  });

  describe('getAnalytics', () => {
    it('fetches analytics data successfully', async () => {
      api.get
        .mockResolvedValueOnce({
          data: {
            dashboard: {
              revenue: { total_revenue: 150000, growth_rate: 15.5, average_order_value: 125 },
              orders: { total_orders: 1200, completion_rate: 92 },
              customers: { active_customers: 800, repeat_rate: 34 },
              delivery: { success_rate: 96 },
              growth: {
                daily_revenue: [{ date: '2026-02-01', revenue: 5000 }],
                daily_orders: [{ date: '2026-02-01', count: 40 }]
              }
            }
          }
        })
        .mockResolvedValueOnce({
          data: {
            product_analytics: [
              { product_id: 1, product_name: 'Water', revenue: 80000, quantity_sold: 120, order_count: 80 }
            ]
          }
        })
        .mockResolvedValueOnce({
          data: {
            customer_analytics: {
              acquisition: { total_new_customers: 40 },
              retention: { current_period_customers: 120, retained_customers: 75 },
              churn: { churned_customers: 15, total_customers: 200, active_customers: 185 }
            }
          }
        });

      const result = await adminService.getAnalytics({ timeframe: '30d' });

      expect(api.get).toHaveBeenNthCalledWith(1, '/analytics/dashboard', {
        params: { period: 'month' }
      });
      expect(api.get).toHaveBeenNthCalledWith(2, '/analytics/products', {
        params: { period: 'month' }
      });
      expect(api.get).toHaveBeenNthCalledWith(3, '/analytics/customers', {
        params: { period: 'month' }
      });
      expect(result).toMatchObject({
        total_revenue: 150000,
        total_orders: 1200,
        active_customers: 800,
        growth_rate: 15.5,
        top_products: [{ id: 1, name: 'Water', sales: 80000 }]
      });
    });
  });

  describe('analytics detail methods', () => {
    it('normalizes sales trends from analytics endpoints', async () => {
      api.get
        .mockResolvedValueOnce({
          data: {
            revenue_analytics: {
              total_revenue: 250000,
              average_order_value: 125,
              trend: [{ date: '2026-02-01', revenue: 8000, orders: 64 }]
            }
          }
        })
        .mockResolvedValueOnce({
          data: {
            conversion_funnel: {
              conversion_rates: { overall: 8.5 }
            }
          }
        });

      const result = await adminService.getSalesTrends({ timeframe: '7d' });

      expect(api.get).toHaveBeenNthCalledWith(1, '/analytics/revenue', {
        params: { period: 'week', granularity: 'daily' }
      });
      expect(api.get).toHaveBeenNthCalledWith(2, '/analytics/conversion-funnel', {
        params: { period: 'week' }
      });
      expect(result).toMatchObject({
        monthly_revenue: 250000,
        monthly_orders: 64,
        avg_order_value: 125,
        conversion_rate: 8.5
      });
    });

    it('normalizes churn predictions for the analytics page', async () => {
      api.get.mockResolvedValue({
        data: {
          predictions: {
            churn_rate: 12.4,
            at_risk_count: 18,
            high_risk_count: 6,
            customers: [{ id: 4, customer_name: 'Jane Doe', risk_score: 88.5, total_spent: 120000 }]
          }
        }
      });

      const result = await adminService.getChurnPrediction({ timeframe: '30d' });

      expect(api.get).toHaveBeenCalledWith('/analytics/predictions', {
        params: { period: 'month', type: 'churn' }
      });
      expect(result).toMatchObject({
        churn_rate: 12.4,
        at_risk_count: 18,
        high_risk_count: 6
      });
      expect(result.customers[0].risk_score).toBe(88.5);
    });

    it('normalizes revenue forecast analytics', async () => {
      api.get.mockResolvedValue({
        data: {
          predictions: {
            next_month_revenue: 100000,
            next_quarter_revenue: 320000,
            confidence_level: 81,
            historical: [{ date: '2026-02-01', revenue: 3000 }],
            predictions: [{ date: '2026-03-01', predicted_revenue: 3500 }],
            drivers: [{ factor: 'Historical trend', impact: 'Steady growth', trend: 'positive', weight: 45 }]
          }
        }
      });

      const result = await adminService.getRevenueForecast({ timeframe: '90d' });

      expect(api.get).toHaveBeenCalledWith('/analytics/predictions', {
        params: { period: 'quarter', type: 'revenue', horizon: 90 }
      });
      expect(result).toMatchObject({
        next_month: 100000,
        next_quarter: 320000,
        confidence_level: 81
      });
      expect(result.forecast).toEqual([null, 3500]);
    });
  });

  describe('exportData', () => {
    it('exports data as blob successfully', async () => {
      const mockBlob = new Blob(['test data'], { type: 'application/xlsx' });

      api.get.mockResolvedValue({ data: mockBlob });

      const result = await adminService.exportData('users', { status: 'active' });

      expect(api.get).toHaveBeenCalledWith('/admin/export/users', {
        params: { status: 'active' },
        responseType: 'blob'
      });
      expect(result).toEqual(mockBlob);
    });
  });

  describe('generateReport', () => {
    it('generates report successfully', async () => {
      const mockResponse = { report_id: '12345', status: 'generated' };
      const filters = { start_date: '2024-01-01', end_date: '2024-01-31' };

      api.post.mockResolvedValue({ data: mockResponse });

      const result = await adminService.generateReport('monthly_sales', filters);

      expect(api.post).toHaveBeenCalledWith('/admin/reports/generate', {
        report_type: 'monthly_sales',
        start_date: '2024-01-01',
        end_date: '2024-01-31'
      });
      expect(result).toEqual(mockResponse);
    });
  });

  describe('translation management', () => {
    it('fetches translations with query params', async () => {
      const params = { page: 1, per_page: 50, search: 'telegram.welcome', category: 'telegram', language: 'en' };
      const mockData = { success: true, data: { translations: [] }, meta: { total: 0 } };

      api.get.mockResolvedValue({ data: mockData });

      const result = await adminService.getTranslations(params);

      expect(api.get).toHaveBeenCalledWith('/admin/translations', { params });
      expect(result).toEqual(mockData);
    });

    it('syncs entity translations for selected entity type', async () => {
      const payload = { entity_ids: [1, 2] };
      const mockData = { success: true, message: 'Synced translations' };

      api.post.mockResolvedValue({ data: mockData });

      const result = await adminService.syncEntityTranslations({
        entityType: 'Product',
        data: payload
      });

      expect(api.post).toHaveBeenCalledWith('/admin/translations/sync/Product', payload);
      expect(result).toEqual(mockData);
    });

    it('rejects sync request without entity type', async () => {
      await expect(
        adminService.syncEntityTranslations({ data: {} })
      ).rejects.toThrow('Entity type is required');
    });
  });
});
