import exportUtils from '../../utils/exportUtils';
import adminService from '../../services/adminService';
import { saveAs } from 'file-saver';
import * as XLSX from 'xlsx';

// Mock dependencies
jest.mock('../../services/adminService');
jest.mock('file-saver');
jest.mock('xlsx');
jest.mock('jspdf', () => {
  return jest.fn().mockImplementation(() => ({
    setFontSize: jest.fn(),
    text: jest.fn(),
    autoTable: jest.fn(),
    save: jest.fn()
  }));
});

describe('ExportUtils', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe('exportToExcel', () => {
    it('exports data to Excel successfully', () => {
      const mockData = [
        { id: 1, name: 'John Doe', email: 'john@example.com' },
        { id: 2, name: 'Jane Smith', email: 'jane@example.com' }
      ];

      const mockWorksheet = {};
      const mockWorkbook = {};
      const mockBuffer = new ArrayBuffer(8);

      XLSX.utils.json_to_sheet.mockReturnValue(mockWorksheet);
      XLSX.utils.book_new.mockReturnValue(mockWorkbook);
      XLSX.utils.book_append_sheet.mockImplementation(() => {});
      XLSX.write.mockReturnValue(mockBuffer);

      const result = exportUtils.exportToExcel(mockData, 'test_export', 'Test Sheet');

      expect(XLSX.utils.json_to_sheet).toHaveBeenCalledWith(mockData);
      expect(XLSX.utils.book_new).toHaveBeenCalled();
      expect(XLSX.utils.book_append_sheet).toHaveBeenCalledWith(mockWorkbook, mockWorksheet, 'Test Sheet');
      expect(saveAs).toHaveBeenCalled();
      expect(result.success).toBe(true);
      expect(result.message).toBe('Excel file exported successfully');
    });

    it('handles export errors gracefully', () => {
      XLSX.utils.json_to_sheet.mockImplementation(() => {
        throw new Error('Export failed');
      });

      const result = exportUtils.exportToExcel([], 'test_export');

      expect(result.success).toBe(false);
      expect(result.message).toBe('Failed to export Excel file');
    });
  });

  describe('exportToCSV', () => {
    it('exports data to CSV successfully', () => {
      const mockData = [
        { id: 1, name: 'John Doe', email: 'john@example.com' }
      ];

      const mockWorksheet = {};
      const mockCSV = 'id,name,email\n1,John Doe,john@example.com';

      XLSX.utils.json_to_sheet.mockReturnValue(mockWorksheet);
      XLSX.utils.sheet_to_csv.mockReturnValue(mockCSV);

      const result = exportUtils.exportToCSV(mockData, 'test_export');

      expect(XLSX.utils.json_to_sheet).toHaveBeenCalledWith(mockData);
      expect(XLSX.utils.sheet_to_csv).toHaveBeenCalledWith(mockWorksheet);
      expect(saveAs).toHaveBeenCalled();
      expect(result.success).toBe(true);
      expect(result.message).toBe('CSV file exported successfully');
    });
  });

  describe('exportUsers', () => {
    it('exports users data successfully', async () => {
      const mockUsersData = {
        users: [
          {
            id: 1,
            name: 'John Doe',
            email: 'john@example.com',
            role: 'customer',
            status: 'active',
            created_at: '2024-01-01T00:00:00Z',
            last_login: '2024-01-15T10:00:00Z'
          }
        ]
      };

      adminService.getUsers.mockResolvedValue(mockUsersData);

      // Mock the exportToExcel method
      const exportToExcelSpy = jest.spyOn(exportUtils, 'exportToExcel');
      exportToExcelSpy.mockReturnValue({ success: true, message: 'Excel file exported successfully' });

      const result = await exportUtils.exportUsers({}, 'excel');

      expect(adminService.getUsers).toHaveBeenCalledWith({ per_page: 10000 });
      expect(exportToExcelSpy).toHaveBeenCalled();
      expect(result.success).toBe(true);

      exportToExcelSpy.mockRestore();
    });

    it('handles API errors during user export', async () => {
      adminService.getUsers.mockRejectedValue(new Error('API Error'));

      const result = await exportUtils.exportUsers({}, 'excel');

      expect(result.success).toBe(false);
      expect(result.message).toBe('Failed to export users');
    });
  });

  describe('exportOrders', () => {
    it('exports orders data successfully', async () => {
      const mockOrdersData = {
        orders: [
          {
            id: 1,
            order_number: 'ORD-001',
            customer_name: 'John Doe',
            customer_email: 'john@example.com',
            total_amount: 99.99,
            status: 'delivered',
            payment_status: 'paid',
            created_at: '2024-01-01T00:00:00Z',
            items_count: 2
          }
        ]
      };

      adminService.getOrders.mockResolvedValue(mockOrdersData);

      const exportToExcelSpy = jest.spyOn(exportUtils, 'exportToExcel');
      exportToExcelSpy.mockReturnValue({ success: true, message: 'Excel file exported successfully' });

      const result = await exportUtils.exportOrders({}, 'excel');

      expect(adminService.getOrders).toHaveBeenCalledWith({ per_page: 10000 });
      expect(exportToExcelSpy).toHaveBeenCalled();
      expect(result.success).toBe(true);

      exportToExcelSpy.mockRestore();
    });

    it('exports orders to PDF format', async () => {
      const mockOrdersData = {
        orders: [
          {
            order_number: 'ORD-001',
            customer_name: 'John Doe',
            total_amount: 99.99,
            status: 'delivered',
            created_at: '2024-01-01T00:00:00Z'
          }
        ]
      };

      adminService.getOrders.mockResolvedValue(mockOrdersData);

      const exportToPDFSpy = jest.spyOn(exportUtils, 'exportToPDF');
      exportToPDFSpy.mockReturnValue({ success: true, message: 'PDF file exported successfully' });

      const result = await exportUtils.exportOrders({}, 'pdf');

      expect(exportToPDFSpy).toHaveBeenCalled();
      expect(result.success).toBe(true);

      exportToPDFSpy.mockRestore();
    });
  });

  describe('exportProducts', () => {
    it('exports products data successfully', async () => {
      const mockProductsData = {
        products: [
          {
            id: 1,
            sku: 'WB-001',
            name: 'Water Bottle',
            category: 'water_bottles',
            price: 25.99,
            stock_quantity: 100,
            status: 'active',
            created_at: '2024-01-01T00:00:00Z',
            is_featured: true
          }
        ]
      };

      adminService.getProducts.mockResolvedValue(mockProductsData);

      const exportToExcelSpy = jest.spyOn(exportUtils, 'exportToExcel');
      exportToExcelSpy.mockReturnValue({ success: true, message: 'Excel file exported successfully' });

      const result = await exportUtils.exportProducts({}, 'excel');

      expect(adminService.getProducts).toHaveBeenCalledWith({ per_page: 10000 });
      expect(exportToExcelSpy).toHaveBeenCalled();
      expect(result.success).toBe(true);

      exportToExcelSpy.mockRestore();
    });
  });

  describe('exportData', () => {
    it('calls correct export method based on type', async () => {
      const exportUsersSpy = jest.spyOn(exportUtils, 'exportUsers');
      exportUsersSpy.mockResolvedValue({ success: true, message: 'Users exported successfully' });

      const result = await exportUtils.exportData('users', {}, 'excel');

      expect(exportUsersSpy).toHaveBeenCalledWith({}, 'excel');
      expect(result.success).toBe(true);

      exportUsersSpy.mockRestore();
    });

    it('returns error for unsupported export type', async () => {
      const result = await exportUtils.exportData('unsupported_type', {}, 'excel');

      expect(result.success).toBe(false);
      expect(result.message).toBe('Export type not supported');
    });
  });
});