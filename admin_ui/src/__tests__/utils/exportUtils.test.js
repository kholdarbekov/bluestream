import exportUtils from '../../utils/exportUtils';
import adminService from '../../services/adminService';
import { saveAs } from 'file-saver';
import ExcelJS from 'exceljs';
import jsPDF from 'jspdf';
import autoTable from 'jspdf-autotable';

vi.mock('../../services/adminService');
vi.mock('file-saver', () => ({
  saveAs: vi.fn(),
  default: { saveAs: vi.fn() },
}));

const mockWriteBuffer = vi.fn();
const mockAddRows = vi.fn();
const mockAddWorksheet = vi.fn();

vi.mock('exceljs', () => {
  const Workbook = vi.fn().mockImplementation(() => ({
    addWorksheet: mockAddWorksheet,
    xlsx: { writeBuffer: mockWriteBuffer },
  }));
  return { default: { Workbook } };
});

vi.mock('jspdf', () => ({
  default: vi.fn().mockImplementation(() => ({
    setFontSize: vi.fn(),
    text: vi.fn(),
    save: vi.fn(),
  })),
}));

vi.mock('jspdf-autotable', () => ({
  default: vi.fn(),
}));

describe('ExportUtils', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockAddWorksheet.mockReturnValue({ columns: [], addRows: mockAddRows });
    mockWriteBuffer.mockResolvedValue(new ArrayBuffer(8));
  });

  describe('exportToExcel', () => {
    it('exports data to Excel successfully', async () => {
      const mockData = [
        { id: 1, name: 'John Doe', email: 'john@example.com' },
        { id: 2, name: 'Jane Smith', email: 'jane@example.com' },
      ];

      const result = await exportUtils.exportToExcel(mockData, 'test_export', 'Test Sheet');

      expect(ExcelJS.Workbook).toHaveBeenCalled();
      expect(mockAddWorksheet).toHaveBeenCalledWith('Test Sheet');
      expect(mockAddRows).toHaveBeenCalledWith(mockData);
      expect(mockWriteBuffer).toHaveBeenCalled();
      expect(saveAs).toHaveBeenCalled();
      expect(result.success).toBe(true);
      expect(result.message).toBe('Excel file exported successfully');
    });

    it('handles export errors gracefully', async () => {
      mockWriteBuffer.mockRejectedValueOnce(new Error('Export failed'));

      const result = await exportUtils.exportToExcel([{ a: 1 }], 'test_export');

      expect(result.success).toBe(false);
      expect(result.message).toBe('Failed to export Excel file');
    });
  });

  describe('exportToCSV', () => {
    it('exports data to CSV successfully', () => {
      const mockData = [
        { id: 1, name: 'John Doe', email: 'john@example.com' },
      ];

      const result = exportUtils.exportToCSV(mockData, 'test_export');

      expect(saveAs).toHaveBeenCalled();
      const blob = saveAs.mock.calls[0][0];
      const filename = saveAs.mock.calls[0][1];
      expect(filename).toBe('test_export.csv');
      expect(blob.type).toContain('text/csv');
      expect(result.success).toBe(true);
      expect(result.message).toBe('CSV file exported successfully');
    });

    it('escapes CSV cells containing commas and quotes', () => {
      const mockData = [
        { name: 'Smith, Jane', quote: 'She said "hi"' },
      ];

      exportUtils.exportToCSV(mockData, 'csv_edge');

      expect(saveAs).toHaveBeenCalled();
    });
  });

  describe('exportToPDF', () => {
    it('generates a PDF via autoTable', () => {
      const mockData = [{ col1: 'a', col2: 'b' }];

      const result = exportUtils.exportToPDF(mockData, 'test_export', 'Title');

      expect(jsPDF).toHaveBeenCalled();
      expect(autoTable).toHaveBeenCalled();
      expect(result.success).toBe(true);
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
            last_login: '2024-01-15T10:00:00Z',
          },
        ],
      };

      adminService.getUsers.mockResolvedValue(mockUsersData);

      const exportToExcelSpy = vi.spyOn(exportUtils, 'exportToExcel');
      exportToExcelSpy.mockResolvedValue({ success: true, message: 'Excel file exported successfully' });

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
            items_count: 2,
          },
        ],
      };

      adminService.getOrders.mockResolvedValue(mockOrdersData);

      const exportToExcelSpy = vi.spyOn(exportUtils, 'exportToExcel');
      exportToExcelSpy.mockResolvedValue({ success: true, message: 'Excel file exported successfully' });

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
            created_at: '2024-01-01T00:00:00Z',
          },
        ],
      };

      adminService.getOrders.mockResolvedValue(mockOrdersData);

      const exportToPDFSpy = vi.spyOn(exportUtils, 'exportToPDF');
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
            is_featured: true,
          },
        ],
      };

      adminService.getProducts.mockResolvedValue(mockProductsData);

      const exportToExcelSpy = vi.spyOn(exportUtils, 'exportToExcel');
      exportToExcelSpy.mockResolvedValue({ success: true, message: 'Excel file exported successfully' });

      const result = await exportUtils.exportProducts({}, 'excel');

      expect(adminService.getProducts).toHaveBeenCalledWith({ per_page: 10000 });
      expect(exportToExcelSpy).toHaveBeenCalled();
      expect(result.success).toBe(true);

      exportToExcelSpy.mockRestore();
    });
  });

  describe('exportData', () => {
    it('calls correct export method based on type', async () => {
      const exportUsersSpy = vi.spyOn(exportUtils, 'exportUsers');
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
