import * as XLSX from 'xlsx';
import { saveAs } from 'file-saver';
import jsPDF from 'jspdf';
import 'jspdf-autotable';
import adminService from '../services/adminService';
import { formatLocalDate, formatLocaleDateTime, nowTashkent } from './dateUtils';

class ExportUtils {
  // Export data to Excel
  exportToExcel(data, filename, sheetName = 'Data') {
    try {
      const worksheet = XLSX.utils.json_to_sheet(data);
      const workbook = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(workbook, worksheet, sheetName);

      // Auto-size columns
      const cols = Object.keys(data[0] || {}).map(() => ({ wch: 15 }));
      worksheet['!cols'] = cols;

      const excelBuffer = XLSX.write(workbook, { bookType: 'xlsx', type: 'array' });
      const blob = new Blob([excelBuffer], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
      saveAs(blob, `${filename}.xlsx`);

      return { success: true, message: 'Excel file exported successfully' };
    } catch (error) {
      console.error('Excel export error:', error);
      return { success: false, message: 'Failed to export Excel file' };
    }
  }

  // Export data to CSV
  exportToCSV(data, filename) {
    try {
      const worksheet = XLSX.utils.json_to_sheet(data);
      const csvOutput = XLSX.utils.sheet_to_csv(worksheet);
      const blob = new Blob([csvOutput], { type: 'text/csv;charset=utf-8;' });
      saveAs(blob, `${filename}.csv`);

      return { success: true, message: 'CSV file exported successfully' };
    } catch (error) {
      console.error('CSV export error:', error);
      return { success: false, message: 'Failed to export CSV file' };
    }
  }

  // Export data to PDF
  exportToPDF(data, filename, title = 'Data Report', columns = null) {
    try {
      const doc = new jsPDF();

      // Add title
      doc.setFontSize(18);
      doc.text(title, 14, 20);

      // Add date
      doc.setFontSize(10);
      doc.text(`Generated on: ${nowTashkent()}`, 14, 30);

      // Prepare table data
      const tableColumns = columns || Object.keys(data[0] || {});
      const tableRows = data.map(row =>
        tableColumns.map(col => row[col] || '')
      );

      // Add table
      doc.autoTable({
        head: [tableColumns],
        body: tableRows,
        startY: 40,
        theme: 'grid',
        styles: {
          fontSize: 8,
          cellPadding: 3
        },
        headStyles: {
          fillColor: [24, 144, 255],
          textColor: [255, 255, 255],
          fontSize: 9,
          fontStyle: 'bold'
        },
        alternateRowStyles: {
          fillColor: [245, 245, 245]
        }
      });

      doc.save(`${filename}.pdf`);

      return { success: true, message: 'PDF file exported successfully' };
    } catch (error) {
      console.error('PDF export error:', error);
      return { success: false, message: 'Failed to export PDF file' };
    }
  }

  // Export analytics report
  async exportAnalyticsReport(type, filters = {}, format = 'excel') {
    try {
      const response = await adminService.exportReportExcel('analytics', filters);
      const blob = new Blob([response], {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
      });
      saveAs(blob, `analytics_report_${new Date().toISOString().split('T')[0]}.xlsx`);

      return { success: true, message: 'Analytics report exported successfully' };
    } catch (error) {
      console.error('Analytics export error:', error);
      return { success: false, message: 'Failed to export analytics report' };
    }
  }

  // Export users data
  async exportUsers(filters = {}, format = 'excel') {
    try {
      const filename = `users_export_${new Date().toISOString().split('T')[0]}`;

      if (format === 'api') {
        const response = await adminService.exportData('users', filters);
        const blob = new Blob([response], {
          type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        });
        saveAs(blob, `${filename}.xlsx`);
      } else {
        // Fallback: fetch data and export locally
        const userData = await adminService.getUsers({ ...filters, per_page: 10000 });
        const exportData = (userData.data?.items || []).map(user => ({
          'ID': user.id,
          'Name': user.name,
          'Email': user.email,
          'Role': user.role,
          'Status': user.status,
          'Created': formatLocalDate(user.created_at),
          'Last Login': user.last_login ? formatLocalDate(user.last_login) : 'Never'
        }));

        return this.exportToExcel(exportData, filename, 'Users');
      }

      return { success: true, message: 'Users exported successfully' };
    } catch (error) {
      console.error('Users export error:', error);
      return { success: false, message: 'Failed to export users' };
    }
  }

  // Export orders data
  async exportOrders(filters = {}, format = 'excel') {
    try {
      const filename = `orders_export_${new Date().toISOString().split('T')[0]}`;

      const ordersData = await adminService.getOrders({ ...filters, per_page: 10000 });
      const exportData = (ordersData.data?.items || []).map(order => ({
        'Order Number': order.order_number,
        'Customer': order.customer_name,
        'Email': order.customer_email,
        'Total Amount': `${order.total_amount?.toFixed(2)} UZS`,
        'Status': order.status,
        'Payment Status': order.payment_status,
        'Order Date': formatLocalDate(order.created_at),
        'Items Count': order.items_count
      }));

      if (format === 'pdf') {
        return this.exportToPDF(exportData, filename, 'Orders Report',
          ['Order Number', 'Customer', 'Total Amount', 'Status', 'Order Date']);
      } else if (format === 'csv') {
        return this.exportToCSV(exportData, filename);
      } else {
        return this.exportToExcel(exportData, filename, 'Orders');
      }
    } catch (error) {
      console.error('Orders export error:', error);
      return { success: false, message: 'Failed to export orders' };
    }
  }

  // Export products data
  async exportProducts(filters = {}, format = 'excel') {
    try {
      const filename = `products_export_${new Date().toISOString().split('T')[0]}`;

      const productsData = await adminService.getProducts({ ...filters, per_page: 10000 });
      const exportData = (productsData.data?.items || []).map(product => ({
        'SKU': product.sku,
        'Product Name': product.name,
        'Category': product.category,
        'Price': `${product.price?.toFixed(2)} UZS`,
        'Stock': product.stock_quantity,
        'Status': product.status,
        'Created': formatLocalDate(product.created_at),
        'Featured': product.is_featured ? 'Yes' : 'No'
      }));

      if (format === 'pdf') {
        return this.exportToPDF(exportData, filename, 'Products Report',
          ['SKU', 'Product Name', 'Category', 'Price', 'Stock', 'Status']);
      } else if (format === 'csv') {
        return this.exportToCSV(exportData, filename);
      } else {
        return this.exportToExcel(exportData, filename, 'Products');
      }
    } catch (error) {
      console.error('Products export error:', error);
      return { success: false, message: 'Failed to export products' };
    }
  }

  // Export deliveries data
  async exportDeliveries(filters = {}, format = 'excel') {
    try {
      const filename = `deliveries_export_${new Date().toISOString().split('T')[0]}`;

      const deliveriesData = await adminService.getDeliveries({ ...filters, per_page: 10000 });
      const exportData = (deliveriesData.data?.items || []).map(delivery => ({
        'Delivery ID': delivery.delivery_id,
        'Order Number': delivery.order_number,
        'Customer': delivery.customer_name,
        'Driver': delivery.driver_name || 'Not Assigned',
        'Address': delivery.delivery_address,
        'Status': delivery.status,
        'Priority': delivery.priority,
        'Scheduled Date': formatLocalDate(delivery.scheduled_date),
        'Created': formatLocalDate(delivery.created_at)
      }));

      if (format === 'pdf') {
        return this.exportToPDF(exportData, filename, 'Deliveries Report',
          ['Delivery ID', 'Order Number', 'Customer', 'Status', 'Scheduled Date']);
      } else if (format === 'csv') {
        return this.exportToCSV(exportData, filename);
      } else {
        return this.exportToExcel(exportData, filename, 'Deliveries');
      }
    } catch (error) {
      console.error('Deliveries export error:', error);
      return { success: false, message: 'Failed to export deliveries' };
    }
  }

  // Export loyalty programs data
  async exportLoyaltyPrograms(filters = {}, format = 'excel') {
    try {
      const filename = `loyalty_programs_export_${new Date().toISOString().split('T')[0]}`;

      const programsData = await adminService.getLoyaltyPrograms({ ...filters, per_page: 10000 });
      const exportData = (programsData.items || []).map(program => ({
        'Program Name': program.name,
        'Status': program.is_active ? 'Active' : 'Inactive',
        'Default Program': program.is_default ? 'Yes' : 'No',
        'UZS per Point': program.uzs_per_point || 0,
        'Sign-up Bonus': program.signup_bonus || 0,
        'Referral Bonus': program.referral_bonus || 0,
        'Birthday Bonus': program.birthday_bonus || 0,
        'Min Redemption Points': program.min_redemption_points || 0,
        'Member Count': program.member_count || 0,
        'Tier Count': program.tier_count || 0,
        'Created': formatLocalDate(program.created_at)
      }));

      return this.exportToExcel(exportData, filename, 'Loyalty Programs');
    } catch (error) {
      console.error('Loyalty programs export error:', error);
      return { success: false, message: 'Failed to export loyalty programs' };
    }
  }

  async exportLoyaltyMembers(filters = {}, format = 'excel') {
    try {
      const filename = `loyalty_members_export_${new Date().toISOString().split('T')[0]}`;

      const membersData = await adminService.getLoyaltyMembers({ ...filters, per_page: 10000 });
      const exportData = (membersData.items || []).map(member => ({
        'Customer': member.customer_name,
        'Email': member.customer_email || '',
        'Phone': member.customer_phone || '',
        'Program': member.program_name || '',
        'Tier': member.current_tier || '',
        'Current Balance': member.current_balance || 0,
        'Total Earned': member.total_earned || 0,
        'Total Redeemed': member.total_redeemed || 0,
        'Member Since': formatLocalDate(member.member_since),
        'Last Activity': formatLocalDate(member.last_activity_at)
      }));

      return this.exportToExcel(exportData, filename, 'Loyalty Members');
    } catch (error) {
      console.error('Loyalty members export error:', error);
      return { success: false, message: 'Failed to export loyalty members' };
    }
  }

  async exportLoyaltyRewards(filters = {}, format = 'excel') {
    try {
      const filename = `loyalty_rewards_export_${new Date().toISOString().split('T')[0]}`;

      const rewardsData = await adminService.getLoyaltyRewards({ ...filters, per_page: 10000 });
      const exportData = (rewardsData.items || []).map(reward => ({
        'Reward Name': reward.name,
        'Program': reward.program_name || '',
        'Type': reward.reward_type,
        'Points Cost': reward.points_cost || 0,
        'Redemptions Used': reward.redemptions_used || 0,
        'Max Redemptions': reward.max_redemptions || '',
        'Featured': reward.is_featured ? 'Yes' : 'No',
        'Status': reward.is_active ? 'Active' : 'Inactive',
        'Valid Until': formatLocalDate(reward.valid_until),
        'Created': formatLocalDate(reward.created_at)
      }));

      return this.exportToExcel(exportData, filename, 'Loyalty Rewards');
    } catch (error) {
      console.error('Loyalty rewards export error:', error);
      return { success: false, message: 'Failed to export loyalty rewards' };
    }
  }

  // Export notification campaigns data
  async exportNotificationCampaigns(filters = {}, format = 'excel') {
    try {
      const filename = `notification_campaigns_export_${new Date().toISOString().split('T')[0]}`;

      const campaignsData = await adminService.getNotificationCampaigns({ ...filters, per_page: 10000 });
      const exportData = (campaignsData.campaigns || []).map(campaign => ({
        'Campaign Name': campaign.name,
        'Channel': campaign.channel,
        'Subject': campaign.subject,
        'Recipients': campaign.recipient_count,
        'Sent': campaign.sent_count,
        'Status': campaign.status,
        'Scheduled': campaign.scheduled_at ? formatLocaleDateTime(campaign.scheduled_at) : 'Immediate',
        'Created': formatLocalDate(campaign.created_at)
      }));

      return this.exportToExcel(exportData, filename, 'Notification Campaigns');
    } catch (error) {
      console.error('Notification campaigns export error:', error);
      return { success: false, message: 'Failed to export notification campaigns' };
    }
  }

  // Generic export handler
  async exportData(type, filters = {}, format = 'excel') {
    const exportMethods = {
      users: this.exportUsers,
      orders: this.exportOrders,
      products: this.exportProducts,
      deliveries: this.exportDeliveries,
      'loyalty-members': this.exportLoyaltyMembers,
      'loyalty-programs': this.exportLoyaltyPrograms,
      'loyalty-rewards': this.exportLoyaltyRewards,
      'notification-campaigns': this.exportNotificationCampaigns
    };

    const exportMethod = exportMethods[type];
    if (!exportMethod) {
      return { success: false, message: 'Export type not supported' };
    }

    return exportMethod.call(this, filters, format);
  }
}

export default new ExportUtils();
