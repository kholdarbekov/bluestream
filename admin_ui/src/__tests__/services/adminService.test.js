import adminService from '../../services/adminService';
import api from '../../services/api';

// Mock the API
jest.mock('../../services/api');

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

      expect(api.get).toHaveBeenCalledWith('/admin/dashboard');
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
      const mockData = {
        total_revenue: 150000,
        total_orders: 1200,
        active_customers: 800,
        growth_rate: 15.5
      };

      api.get.mockResolvedValue({ data: mockData });

      const result = await adminService.getAnalytics({ timeframe: '30d' });

      expect(api.get).toHaveBeenCalledWith('/admin/analytics/overview', {
        params: { timeframe: '30d' }
      });
      expect(result).toEqual(mockData);
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
});