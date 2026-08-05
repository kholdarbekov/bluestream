import adminService from '../../services/adminService';
import api from '../../services/api';

// Mock the API
vi.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn()
  },
  getCookie: vi.fn()
}));

describe('AdminService', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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

  describe('getUserPaymentMethods', () => {
    it('fetches admin user payment methods', async () => {
      const mockData = {
        data: {
          available_methods: [{ method: 'payme' }],
          payment_restrictions: { cod_restricted: true }
        }
      };
      api.get.mockResolvedValue({ data: mockData });

      const result = await adminService.getUserPaymentMethods(42);

      expect(api.get).toHaveBeenCalledWith('/admin/users/42/payment-methods');
      expect(result).toEqual(mockData);
    });
  });

  describe('user notification settings', () => {
    it('fetches user notification settings', async () => {
      const mockData = {
        data: {
          notification_settings: {
            delivery_telegram_status_updates_enabled: true
          }
        }
      };
      api.get.mockResolvedValue({ data: mockData });

      const result = await adminService.getUserNotificationSettings(15);

      expect(api.get).toHaveBeenCalledWith('/admin/users/15/notification-settings');
      expect(result).toEqual(mockData);
    });

    it('updates user notification settings', async () => {
      const payload = {
        delivery_telegram_status_updates_enabled: false,
        reason: 'Customer requested via phone'
      };
      const mockData = { message: 'ok' };
      api.put.mockResolvedValue({ data: mockData });

      const result = await adminService.updateUserNotificationSettings(15, payload);

      expect(api.put).toHaveBeenCalledWith('/admin/users/15/notification-settings', payload);
      expect(result).toEqual(mockData);
    });
  });

  describe('Click fiscalization operations', () => {
    it('retries payment fiscalization from admin orders', async () => {
      const mockData = { success: true, data: { fiscalization: { status: 'processing' } } };
      api.post.mockResolvedValue({ data: mockData });

      const result = await adminService.retryPaymentFiscalization(91);

      expect(api.post).toHaveBeenCalledWith('/admin/payments/91/fiscalization/retry');
      expect(result).toEqual(mockData);
    });

    it('loads product marking-code inventory', async () => {
      const mockData = { success: true, data: { items: [], total: 0 } };
      api.get.mockResolvedValue({ data: mockData });

      const result = await adminService.listProductMarkingCodes(44, { page: 2, status: 'available' });

      expect(api.get).toHaveBeenCalledWith('/admin/products/44/marking-codes', {
        params: { page: 2, status: 'available' },
      });
      expect(result).toEqual(mockData);
    });

    it('creates product marking codes', async () => {
      const payload = { codes: ['CODE-1', 'CODE-2'], notes: 'manual seed' };
      const mockData = { success: true, data: { created: 2 } };
      api.post.mockResolvedValue({ data: mockData });

      const result = await adminService.createProductMarkingCodes(44, payload);

      expect(api.post).toHaveBeenCalledWith('/admin/products/44/marking-codes', payload);
      expect(result).toEqual(mockData);
    });

    it('updates one product marking code', async () => {
      const payload = { status: 'archived', notes: 'used externally' };
      const mockData = { success: true, data: { marking_code: { id: 9 } } };
      api.put.mockResolvedValue({ data: mockData });

      const result = await adminService.updateProductMarkingCode(44, 9, payload);

      expect(api.put).toHaveBeenCalledWith('/admin/products/44/marking-codes/9', payload);
      expect(result).toEqual(mockData);
    });
  });

  describe('notification collections', () => {
    it('normalizes notification campaigns for the notifications page', async () => {
      api.get.mockResolvedValue({
        data: {
          success: true,
          data: {
            items: [
              {
                id: 4,
                name: 'Weekend retention push',
                channel: 'sms',
                status: 'scheduled',
                recipient_count: '12',
                sent_count: '0'
              }
            ]
          },
          meta: {
            total: 1,
            page: 1,
            per_page: 20,
            pages: 1,
            has_next: false,
            has_prev: false
          }
        }
      });

      const result = await adminService.getNotificationCampaigns({
        page: 1,
        per_page: 20,
        channel: 'telegram'
      });

      expect(api.get).toHaveBeenCalledWith('/admin/notification-campaigns', {
        params: { page: 1, per_page: 20, channel: 'telegram' }
      });
      expect(result).toEqual({
        campaigns: [
          {
            id: 4,
            name: 'Weekend retention push',
            channel: 'sms',
            category: 'general',
            status: 'scheduled',
            recipient_count: 12,
            sent_count: 0,
            delivered_count: 0,
            failed_count: 0,
            pending_count: 0,
            specific_user_ids: [],
            recipient_ids_snapshot: [],
            summary: {
              total: 0,
              sent: 0,
              delivered: 0,
              failed: 0,
              pending: 0,
              delivery_rate: 0
            },
            recent_notifications: []
          }
        ],
        pagination: {
          total: 1,
          page: 1,
          per_page: 20,
          pages: 1,
          has_next: false,
          has_prev: false
        }
      });
    });

    it('normalizes notification templates for the notifications page', async () => {
      api.get.mockResolvedValue({
        data: {
          success: true,
          data: {
            items: [
              {
                id: 8,
                name: 'Delivery reminder',
                channel: 'push',
                notification_type: 'delivery_update',
                subject: 'Driver is nearby'
              }
            ]
          },
          meta: {
            total: 1,
            page: 1,
            per_page: 20
          }
        }
      });

      const result = await adminService.getNotificationTemplates({ page: 1, per_page: 20 });

      expect(api.get).toHaveBeenCalledWith('/admin/notification-templates', {
        params: { page: 1, per_page: 20 }
      });
      expect(result).toEqual({
        templates: [
          {
            id: 8,
            name: 'Delivery reminder',
            channel: 'push',
            notification_type: 'delivery_update',
            subject: 'Driver is nearby',
            category: 'delivery',
            description: 'Driver is nearby',
            usage_count: 0,
            translations: {}
          }
        ],
        pagination: {
          total: 1,
          page: 1,
          per_page: 20,
          pages: 1,
          has_next: false,
          has_prev: false
        }
      });
    });

    it('loads notification campaign detail', async () => {
      api.get.mockResolvedValue({
        data: {
          data: {
            campaign: {
              id: 12,
              name: 'Spring sale',
              notification_type: 'promotional',
              channel: 'telegram',
              summary: { sent: 10, failed: 1 }
            }
          }
        }
      });

      const result = await adminService.getNotificationCampaign(12);

      expect(api.get).toHaveBeenCalledWith('/admin/notification-campaigns/12');
      expect(result).toEqual({
        id: 12,
        name: 'Spring sale',
        notification_type: 'promotional',
        channel: 'telegram',
        category: 'promotion',
        sent_count: 10,
        delivered_count: 0,
        failed_count: 1,
        pending_count: 0,
        recipient_count: 0,
        specific_user_ids: [],
        recipient_ids_snapshot: [],
        summary: {
          total: 0,
          sent: 10,
          delivered: 0,
          failed: 1,
          pending: 0,
          delivery_rate: 0
        },
        recent_notifications: []
      });
    });

    it('loads notification template channels metadata', async () => {
      api.get.mockResolvedValue({
        data: {
          data: {
            channels: [
              { value: 'email', label: 'Email' },
              { value: 'telegram', label: 'Telegram' }
            ]
          }
        }
      });

      const result = await adminService.getNotificationTemplateChannels();

      expect(api.get).toHaveBeenCalledWith('/admin/notification-templates/channels');
      expect(result).toEqual([
        { value: 'email', label: 'Email' },
        { value: 'telegram', label: 'Telegram' }
      ]);
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

  // Place groups are ownerless (may span customers) — none of these URLs is
  // scoped by a canonical customer id, unlike the legacy address-group route.
  describe('place groups', () => {
    it('creates a place group from any customers addresses', async () => {
      api.post.mockResolvedValue({ data: { success: true, data: { place_group_id: 7 } } });

      const result = await adminService.createPlaceGroup([2, 9], 'Acme office', 'coworkers');

      expect(api.post).toHaveBeenCalledWith('/admin/place-groups', {
        addressIds: [2, 9],
        label: 'Acme office',
        reason: 'coworkers'
      });
      expect(result).toEqual({ success: true, data: { place_group_id: 7 } });
    });

    it('fetches one place group detail', async () => {
      api.get.mockResolvedValue({ data: { success: true, data: { place_group_id: 7 } } });

      await adminService.getPlaceGroup(7);

      expect(api.get).toHaveBeenCalledWith('/admin/place-groups/7');
    });

    it('adds addresses to an existing place group', async () => {
      api.post.mockResolvedValue({ data: { success: true } });

      await adminService.addPlaceGroupAddresses(7, [9], 'new hire');

      expect(api.post).toHaveBeenCalledWith('/admin/place-groups/7/addresses', {
        addressIds: [9],
        reason: 'new hire'
      });
    });

    it('removes one address, sending the reason in the DELETE body', async () => {
      api.delete.mockResolvedValue({ data: { success: true } });

      await adminService.removePlaceGroupAddress(7, 3, 'moved out');

      // `bottlesLeaving` defaults to 0 — the bottles stay with the PLACE and the
      // departing address starts a fresh scope (spec 7.1).
      expect(api.delete).toHaveBeenCalledWith('/admin/place-groups/7/addresses/3', {
        data: { reason: 'moved out', bottlesLeaving: 0 }
      });
    });

    it('sends bottlesLeaving in the remove body', async () => {
      api.delete.mockResolvedValue({ data: { data: {} } });

      await adminService.removePlaceGroupAddress(9, 44, 'left', 2);

      expect(api.delete).toHaveBeenCalledWith('/admin/place-groups/9/addresses/44',
        { data: { reason: 'left', bottlesLeaving: 2 } });
    });

    // Spec 7.4's merge review. The preview takes the exclusions so the figures
    // it returns are the ones the admin is actually deciding against.
    it('fetches the merge preview from the address-keyed route', async () => {
      api.get.mockResolvedValue({ data: { data: {} } });

      await adminService.getPlaceGroupMergePreview([44, 45], { groupId: 9, exclude: [41] });

      expect(api.get).toHaveBeenCalledWith('/admin/place-groups/merge-preview',
        { params: { address_ids: '44,45', group_id: 9, exclude: '41' } });
    });

    it('omits group_id and exclude from the preview when there are none', async () => {
      api.get.mockResolvedValue({ data: { data: {} } });

      await adminService.getPlaceGroupMergePreview([45, 44]);

      expect(api.get).toHaveBeenCalledWith('/admin/place-groups/merge-preview',
        { params: { address_ids: '45,44' } });
    });

    it('forwards the reviewed merge on the create body', async () => {
      api.post.mockResolvedValue({ data: { success: true } });

      await adminService.createPlaceGroup([44, 45], null, 'counted them', {
        excludedLedgerEntryIds: [41],
        resultingBalance: 5,
        previewEntryIds: [41]
      });

      expect(api.post).toHaveBeenCalledWith('/admin/place-groups', {
        addressIds: [44, 45],
        label: null,
        reason: 'counted them',
        excludedLedgerEntryIds: [41],
        resultingBalance: 5,
        previewEntryIds: [41]
      });
    });

    it('forwards the reviewed merge on the add body', async () => {
      api.post.mockResolvedValue({ data: { success: true } });

      await adminService.addPlaceGroupAddresses(9, [44], 'new hire', {
        excludedLedgerEntryIds: [41],
        previewEntryIds: [41, 42]
      });

      expect(api.post).toHaveBeenCalledWith('/admin/place-groups/9/addresses', {
        addressIds: [44],
        reason: 'new hire',
        excludedLedgerEntryIds: [41],
        previewEntryIds: [41, 42]
      });
    });

    it('fetches place-group suggestions for a user', async () => {
      api.get.mockResolvedValue({ data: { success: true, data: { suggestions: [] } } });

      await adminService.getPlaceGroupSuggestions(11);

      expect(api.get).toHaveBeenCalledWith('/admin/users/11/place-group-suggestions');
    });

    it('dismisses a place suggestion pairwise (never the person-dismiss route)', async () => {
      api.post.mockResolvedValue({ data: { success: true } });

      await adminService.dismissPlaceGroupSuggestion(2, 9, 'different buildings');

      expect(api.post).toHaveBeenCalledWith('/admin/place-group-suggestions/dismiss', {
        addressIdA: 2,
        addressIdB: 9,
        reason: 'different buildings'
      });
    });

    it('searches addresses across users, excluding grouped ones by default', async () => {
      api.get.mockResolvedValue({ data: { success: true, data: { addresses: [] } } });

      await adminService.searchAddresses('Carol');

      expect(api.get).toHaveBeenCalledWith('/admin/addresses/search', {
        params: { q: 'Carol', exclude_grouped: 1 }
      });
    });

    it('can include already-grouped addresses in the search', async () => {
      api.get.mockResolvedValue({ data: { success: true, data: { addresses: [] } } });

      await adminService.searchAddresses('Carol', false);

      expect(api.get).toHaveBeenCalledWith('/admin/addresses/search', {
        params: { q: 'Carol', exclude_grouped: 0 }
      });
    });
  });

  // Bottle balances were re-keyed from (user_id, address_id) to the PLACE
  // (address group when set, else the address). These pins exist because two
  // routes 404ed against the current backend with zero coverage catching it.
  describe('bottle tracking — place-keyed routes', () => {
    it('places the bottle ledger on the address-keyed route', async () => {
      api.get.mockResolvedValue({ data: { data: { items: [] } } });

      await adminService.getPlaceBottleLedger(44, { page: 1 });

      expect(api.get).toHaveBeenCalledWith('/admin/bottles/ledger/44', { params: { page: 1 } });
    });

    it('reconciles by address, not by user+address', async () => {
      api.post.mockResolvedValue({ data: { data: {} } });

      await adminService.reconcileBottleBalance(44);

      expect(api.post).toHaveBeenCalledWith('/admin/bottles/reconcile/44');
    });

    it('fetches the customer place summary by user id, on the unchanged URL', async () => {
      api.get.mockResolvedValue({ data: { data: {} } });

      await adminService.getCustomerPlaceSummary(42);

      expect(api.get).toHaveBeenCalledWith('/admin/bottles/balances/42');
    });
  });

  // The COD active-debt cap override must survive BOTH hops: preview owns the
  // blocking_reasons that gate Confirm, and apply re-runs the same preview
  // server-side.
  describe('order payment-method edit', () => {
    it('forwards bypass_cod_check on the preview call', async () => {
      api.post.mockResolvedValue({ data: { success: true, data: { blocking_reasons: [] } } });

      await adminService.previewOrderPaymentMethod(771, { new_method: 'cash', bypass_cod_check: true });

      expect(api.post).toHaveBeenCalledWith('/admin/orders/771/payment-method/preview', {
        new_method: 'cash',
        bypass_cod_check: true
      });
    });

    it('forwards bypass_cod_check on the apply call', async () => {
      api.post.mockResolvedValue({ data: { success: true, data: { order_id: 771 } } });

      await adminService.submitOrderPaymentMethod(771, {
        new_method: 'cash',
        reason: 'card payment failed, driver took cash',
        bypass_cod_check: true
      });

      expect(api.post).toHaveBeenCalledWith('/admin/orders/771/payment-method', {
        new_method: 'cash',
        reason: 'card payment failed, driver took cash',
        bypass_cod_check: true
      });
    });

    it('always sends an explicit false when the override is absent', async () => {
      api.post.mockResolvedValue({ data: { success: true, data: {} } });

      await adminService.previewOrderPaymentMethod(771, { new_method: 'click' });

      expect(api.post).toHaveBeenCalledWith('/admin/orders/771/payment-method/preview', {
        new_method: 'click',
        bypass_cod_check: false
      });
    });
  });
});
