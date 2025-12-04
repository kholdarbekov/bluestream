import api, { getCookie } from './api';

class AdminService {
  // Dashboard API calls
  async getDashboardData() {
    const response = await api.get('/admin/dashboard');
    return response.data;
  }

  async getAnalyticsData(params = {}) {
    const response = await api.get('/admin/analytics', { params });
    return response.data;
  }

  // User management
  async getUsers(params = {}) {
    const response = await api.get('/admin/users', { params });
    return response.data;
  }

  async getUserDetails(userId) {
    const response = await api.get(`/admin/users/${userId}`);
    return response.data;
  }

  async updateUserStatus(userId, status, reason) {
    const response = await api.put(`/admin/users/${userId}/status`, { status, reason });
    return response.data;
  }

  // Order management
  async getOrders(params = {}) {
    const response = await api.get('/admin/orders', { params });
    return response.data;
  }

  async updateOrderStatus(orderId, status, notes) {
    const response = await api.put(`/admin/orders/${orderId}/status`, { status, notes });
    return response.data;
  }

  // Product management
  async getProducts(params = {}) {
    const response = await api.get('/admin/products', { params });
    return response.data;
  }

  async updateProductStock(productId, stockQuantity, reason) {
    const response = await api.put(`/admin/products/${productId}/stock`, {
      stock_quantity: stockQuantity,
      reason
    });
    return response.data;
  }

  async createProduct(productData) {
    const response = await api.post('/admin/products', productData);
    return response.data;
  }

  async updateProduct(productId, productData) {
    const response = await api.put(`/admin/products/${productId}`, productData);
    return response.data;
  }

  async deleteProduct(productId) {
    const response = await api.delete(`/admin/products/${productId}`);
    return response.data;
  }

  // Delivery management
  async getDeliveryPersonnel(params = {}) {
    const response = await api.get('/admin/delivery-personnel', { params });
    return response.data;
  }

  async getDeliveries(params = {}) {
    const response = await api.get('/admin/deliveries', { params });
    return response.data;
  }

  async updateDelivery(deliveryId, data) {
    const response = await api.put(`/admin/deliveries/${deliveryId}`, data);
    return response.data;
  }

  // Loyalty Program management
  async getLoyaltyPrograms(params = {}) {
    const response = await api.get('/admin/loyalty-programs', { params });
    return response.data;
  }

  async getLoyaltyCustomers(params = {}) {
    const response = await api.get('/admin/loyalty-customers', { params });
    return response.data;
  }

  async createLoyaltyProgram(programData) {
    const response = await api.post('/admin/loyalty-programs', programData);
    return response.data;
  }

  async updateLoyaltyProgram(programId, programData) {
    const response = await api.put(`/admin/loyalty-programs/${programId}`, programData);
    return response.data;
  }

  // Notification management
  async getNotificationCampaigns(params = {}) {
    const response = await api.get('/admin/notification-campaigns', { params });
    return response.data;
  }

  async getNotificationTemplates(params = {}) {
    const response = await api.get('/admin/notification-templates', { params });
    return response.data;
  }

  async createNotificationCampaign(campaignData) {
    const response = await api.post('/admin/notification-campaigns', campaignData);
    return response.data;
  }

  async createNotificationTemplate(templateData) {
    const response = await api.post('/admin/notification-templates', templateData);
    return response.data;
  }

  // Advanced Analytics
  async getAnalytics(params = {}) {
    const response = await api.get('/admin/analytics/overview', { params });
    return response.data;
  }

  async getSalesTrends(params = {}) {
    const response = await api.get('/admin/analytics/sales-trends', { params });
    return response.data;
  }

  async getChurnPrediction(params = {}) {
    const response = await api.get('/admin/analytics/customer-churn', { params });
    return response.data;
  }

  async getDeliveryHeatmap(params = {}) {
    const response = await api.get('/admin/analytics/delivery-heatmap', { params });
    return response.data;
  }

  async getRevenueForecast(params = {}) {
    const response = await api.get('/admin/analytics/revenue-forecast', { params });
    return response.data;
  }

  // Export functionality
  async exportData(type, params = {}) {
    const response = await api.get(`/admin/export/${type}`, {
      params,
      responseType: 'blob'
    });
    return response.data;
  }

  async exportReportPDF(reportType, filters = {}) {
    const response = await api.post('/admin/export/pdf', {
      report_type: reportType,
      ...filters
    }, {
      responseType: 'blob'
    });
    return response.data;
  }

  async exportReportExcel(reportType, filters = {}) {
    const response = await api.post('/admin/export/excel', {
      report_type: reportType,
      ...filters
    }, {
      responseType: 'blob'
    });
    return response.data;
  }

  // Campaign management
  async getCampaigns(params = {}) {
    const response = await api.get('/admin/campaigns', { params });
    return response.data;
  }

  // Reports
  async generateReport(reportType, filters = {}) {
    const response = await api.post('/admin/reports/generate', {
      report_type: reportType,
      ...filters
    });
    return response.data;
  }

  // Bulk actions
  async performBulkAction(action, targetType, targetIds, parameters = {}) {
    const response = await api.post('/admin/bulk-actions', {
      action,
      target_type: targetType,
      target_ids: targetIds,
      parameters
    });
    return response.data;
  }

  // System settings
  async getSystemSettings() {
    const response = await api.get('/admin/system-settings');
    return response.data;
  }

  async updateSystemSettings(settings) {
    const response = await api.put('/admin/system-settings', { settings });
    return response.data;
  }

  // Audit logs
  async getAuditLogs(params = {}) {
    const response = await api.get('/admin/audit-logs', { params });
    return response.data;
  }

  // Announcements
  async sendAnnouncement(title, message, targetUsers, channels) {
    const response = await api.post('/admin/send-announcement', {
      title,
      message,
      target_users: targetUsers,
      channels
    });
    return response.data;
  }

  // Backup
  async createBackup(type, includeFiles) {
    const response = await api.post('/admin/backup', {
      type,
      include_files: includeFiles
    });
    return response.data;
  }

  // Translation Management
  async getTranslations(params = {}) {
    const response = await api.get('/admin/translations', { params });
    return response.data;
  }

  async getTranslation(id) {
    const response = await api.get(`/admin/translations/${id}`);
    return response.data;
  }

  async createTranslation(translationData) {
    const response = await api.post('/admin/translations', translationData);
    return response.data;
  }

  async updateTranslation({ id, data }) {
    const response = await api.put(`/admin/translations/${id}`, data);
    return response.data;
  }

  async deleteTranslation(id) {
    const response = await api.delete(`/admin/translations/${id}`);
    return response.data;
  }

  async getTranslatableEntities() {
    const response = await api.get('/admin/translations/entities');
    return response.data;
  }

  async syncEntityTranslations({ entityType, data }) {
    const response = await api.post(`/admin/translations/sync/${entityType}`, data);
    return response.data;
  }

  async exportTranslations(params = {}) {
    const response = await api.get('/admin/translations/export', { 
      params,
      responseType: 'blob'
    });
    return response.data;
  }

  async importTranslations(data) {
    const response = await api.post('/admin/translations/import', data);
    return response.data;
  }

  async getTranslationCompletion(params = {}) {
    const response = await api.get('/admin/translations/completion', { params });
    return response.data;
  }

  async getMissingTranslations(params = {}) {
    const response = await api.get('/admin/translations/missing', { params });
    return response.data;
  }

  // Blog management
  async getBlogPosts(params = {}) {
    const response = await api.get('/admin/blog/posts', { params });
    return response.data;
  }

  async getBlogPost(postId) {
    const response = await api.get(`/admin/blog/posts/${postId}`);
    return response.data;
  }

  async createBlogPost(postData) {
    const response = await api.post('/admin/blog/posts', postData);
    return response.data;
  }

  async updateBlogPost(postId, postData) {
    const response = await api.put(`/admin/blog/posts/${postId}`, postData);
    return response.data;
  }

  async deleteBlogPost(postId) {
    const response = await api.delete(`/admin/blog/posts/${postId}`);
    return response.data;
  }

  async publishBlogPost(postId) {
    const response = await api.post(`/admin/blog/posts/${postId}/publish`);
    return response.data;
  }

  async unpublishBlogPost(postId) {
    const response = await api.post(`/admin/blog/posts/${postId}/unpublish`);
    return response.data;
  }

  // File upload
  async uploadImage(file, options = {}) {
    const formData = new FormData();
    formData.append('file', file);

    // Add optional parameters
    if (options.folder) formData.append('folder', options.folder);
    if (options.resize !== undefined) formData.append('resize', options.resize);
    if (options.max_width) formData.append('max_width', options.max_width);
    if (options.max_height) formData.append('max_height', options.max_height);
    if (options.quality) formData.append('quality', options.quality);

    const csrfToken = getCookie('csrf_access_token');

    const response = await api.post('/admin/upload/image', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
        'X-CSRF-TOKEN': csrfToken,
      },
      withCredentials: true,
    });
    return response.data;
  }

  async uploadFile(file, folder = 'documents') {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('folder', folder);

    const response = await api.post('/admin/upload/file', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    });
    return response.data;
  }
}

export default new AdminService();