import api, { getCookie } from './api';

const TIMEFRAME_TO_PERIOD = {
  '7d': 'week',
  '30d': 'month',
  '90d': 'quarter',
  '1y': 'year'
};

const buildAnalyticsParams = (params = {}) => {
  const queryParams = { ...params };
  if (!queryParams.period) {
    queryParams.period = TIMEFRAME_TO_PERIOD[queryParams.timeframe] || 'month';
  }
  delete queryParams.timeframe;
  return queryParams;
};

const formatChartLabel = (value) => {
  if (!value) {
    return '';
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
};

const toNumber = (value, fallback = 0) => {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
};

const normalizeOverviewAnalytics = (dashboardPayload, productsPayload, customersPayload) => {
  const dashboard = dashboardPayload?.dashboard || {};
  const revenue = dashboard.revenue || {};
  const orders = dashboard.orders || {};
  const customers = dashboard.customers || {};
  const delivery = dashboard.delivery || {};
  const growth = dashboard.growth || {};
  const customerAnalytics = customersPayload?.customer_analytics || {};
  const churn = customerAnalytics.churn || {};
  const retention = customerAnalytics.retention || {};
  const acquisition = customerAnalytics.acquisition || {};

  return {
    total_revenue: toNumber(revenue.total_revenue),
    total_orders: toNumber(orders.total_orders),
    active_customers: toNumber(customers.active_customers),
    growth_rate: toNumber(revenue.growth_rate),
    revenue_trend: (growth.daily_revenue || []).map((item) => ({
      label: formatChartLabel(item.date),
      value: toNumber(item.revenue)
    })),
    order_trend: (growth.daily_orders || []).map((item) => ({
      label: formatChartLabel(item.date),
      value: toNumber(item.count)
    })),
    top_products: (productsPayload?.product_analytics || []).slice(0, 5).map((product) => ({
      id: product.product_id,
      name: product.product_name,
      sales: toNumber(product.revenue),
      quantity_sold: toNumber(product.quantity_sold),
      order_count: toNumber(product.order_count)
    })),
    customer_segments: {
      new: toNumber(acquisition.total_new_customers),
      active: toNumber(retention.current_period_customers),
      loyal: toNumber(retention.retained_customers),
      at_risk: toNumber(churn.churned_customers),
      inactive: Math.max(
        0,
        toNumber(churn.total_customers) - toNumber(churn.active_customers)
      )
    },
    average_order_value: toNumber(revenue.average_order_value),
    completion_rate: toNumber(orders.completion_rate),
    repeat_rate: toNumber(customers.repeat_rate),
    delivery_success_rate: toNumber(delivery.success_rate)
  };
};

const normalizeSalesAnalytics = (revenuePayload, conversionPayload) => {
  const revenueAnalytics = revenuePayload?.revenue_analytics || {};
  const trend = revenueAnalytics.trend || [];
  const conversionRates = conversionPayload?.conversion_funnel?.conversion_rates || {};

  return {
    monthly_revenue: toNumber(revenueAnalytics.total_revenue),
    monthly_orders: trend.reduce((sum, item) => sum + toNumber(item.orders), 0),
    avg_order_value: toNumber(revenueAnalytics.average_order_value),
    conversion_rate: toNumber(conversionRates.overall),
    labels: trend.map((item) => formatChartLabel(item.date || item.hour || item.day_name)),
    revenue: trend.map((item) => toNumber(item.revenue)),
    orders: trend.map((item) => toNumber(item.orders))
  };
};

const normalizeChurnPrediction = (predictionPayload) => {
  const predictions = predictionPayload?.predictions || {};
  return {
    churn_rate: toNumber(predictions.churn_rate),
    at_risk_count: toNumber(predictions.at_risk_count),
    high_risk_count: toNumber(predictions.high_risk_count),
    customers: (predictions.customers || []).map((customer) => ({
      ...customer,
      total_spent: toNumber(customer.total_spent),
      risk_score: toNumber(customer.risk_score)
    }))
  };
};

const normalizeDeliveryAnalytics = (deliveryPayload) => {
  const deliveryAnalytics = deliveryPayload?.delivery_analytics || {};
  const performance = deliveryAnalytics.performance || {};
  const regions = deliveryAnalytics.geographic_patterns?.by_city || [];

  return {
    overall_on_time_rate: toNumber(performance.success_rate),
    avg_delivery_time: toNumber(performance.average_delivery_time_hours),
    failed_deliveries: Math.max(
      0,
      toNumber(performance.total_deliveries) - toNumber(performance.successful_deliveries)
    ),
    regions: regions.map((region) => ({
      region: region.city || 'Unknown',
      total_deliveries: toNumber(region.orders),
      on_time_rate: toNumber(performance.success_rate),
      avg_delivery_time: toNumber(performance.average_delivery_time_hours),
      performance:
        toNumber(performance.success_rate) >= 95
          ? 'excellent'
          : toNumber(performance.success_rate) >= 85
            ? 'good'
            : toNumber(performance.success_rate) >= 70
              ? 'average'
              : 'poor'
    }))
  };
};

const normalizeRevenueForecast = (predictionPayload) => {
  const predictions = predictionPayload?.predictions || {};
  const historical = predictions.historical || [];
  const forecast = predictions.predictions || [];

  return {
    next_month: toNumber(predictions.next_month_revenue),
    next_quarter: toNumber(predictions.next_quarter_revenue),
    confidence_level: toNumber(predictions.confidence_level),
    labels: [
      ...historical.map((item) => formatChartLabel(item.date)),
      ...forecast.map((item) => formatChartLabel(item.date))
    ],
    historical: historical.map((item) => toNumber(item.revenue)),
    forecast: [
      ...new Array(historical.length).fill(null),
      ...forecast.map((item) => toNumber(item.predicted_revenue))
    ],
    factors: (predictions.drivers || []).map((driver) => ({
      factor: driver.factor,
      impact: driver.impact,
      trend: driver.trend,
      weight: toNumber(driver.weight)
    }))
  };
};

const normalizeAdminCollectionResponse = (payload = {}) => {
  const envelope = payload || {};
  const data = envelope.data || {};
  const meta = envelope.meta || {};
  const items = data.items || data.programs || data.rewards || data.tiers || [];

  return {
    items,
    meta,
    summary: meta.summary || data.summary || {},
    total: meta.total ?? data.total ?? items.length,
    page: meta.page ?? 1,
    per_page: meta.per_page ?? items.length,
  };
};

const buildLegacyPaginationShape = (normalized) => ({
  total: normalized.total,
  page: normalized.page,
  per_page: normalized.per_page,
  pages: normalized.meta.pages ?? 1,
  has_next: normalized.meta.has_next ?? false,
  has_prev: normalized.meta.has_prev ?? false,
});

const notificationTypeCategory = (notificationType) => {
  const value = `${notificationType || ''}`.toLowerCase();
  if (value.startsWith('order_')) return 'order';
  if (value.startsWith('delivery_')) return 'delivery';
  if (value.startsWith('payment_')) return 'payment';
  if (value.includes('loyalty') || value.includes('reward')) return 'loyalty';
  if (value.startsWith('subscription_')) return 'subscription';
  if (value.includes('security')) return 'security';
  if (value.includes('promotional')) return 'promotion';
  if (value.includes('reminder')) return 'reminder';
  if (value.includes('system')) return 'system';
  return 'general';
};

const normalizeNotificationCampaign = (campaign = {}) => ({
  ...campaign,
  category: campaign.category || notificationTypeCategory(campaign.notification_type),
  sent_count: toNumber(campaign.sent_count ?? campaign.summary?.sent),
  delivered_count: toNumber(campaign.delivered_count ?? campaign.summary?.delivered),
  failed_count: toNumber(campaign.failed_count ?? campaign.summary?.failed),
  pending_count: toNumber(campaign.pending_count ?? campaign.summary?.pending),
  recipient_count: toNumber(campaign.recipient_count),
  specific_user_ids: campaign.specific_user_ids || [],
  recipient_ids_snapshot: campaign.recipient_ids_snapshot || [],
  summary: {
    total: toNumber(campaign.summary?.total),
    sent: toNumber(campaign.summary?.sent),
    delivered: toNumber(campaign.summary?.delivered),
    failed: toNumber(campaign.summary?.failed),
    pending: toNumber(campaign.summary?.pending),
    delivery_rate: toNumber(campaign.summary?.delivery_rate),
  },
  recent_notifications: campaign.recent_notifications || [],
});

const normalizeNotificationTemplate = (template = {}) => ({
  ...template,
  description: template.description || template.subject || '',
  category: template.category || notificationTypeCategory(template.notification_type),
  usage_count: toNumber(template.usage_count),
  translations: template.translations || {},
});

const normalizeNotificationCampaignCollection = (payload = {}) => {
  const normalized = normalizeAdminCollectionResponse(payload);

  return {
    campaigns: normalized.items.map(normalizeNotificationCampaign),
    pagination: buildLegacyPaginationShape(normalized),
  };
};

const normalizeNotificationTemplateCollection = (payload = {}) => {
  const normalized = normalizeAdminCollectionResponse(payload);

  return {
    templates: normalized.items.map(normalizeNotificationTemplate),
    pagination: buildLegacyPaginationShape(normalized),
  };
};

class AdminService {
  // Dashboard API calls
  async getDashboardData(params = {}) {
    const response = await api.get('/admin/dashboard', { params });
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

  async getCustomerMapPins() {
    const response = await api.get('/admin/customers/map-pins');
    return response.data;
  }

  async getUserDetails(userId) {
    const response = await api.get(`/admin/users/${userId}`);
    return response.data;
  }

  async getUserPaymentMethods(userId) {
    const response = await api.get(`/admin/users/${userId}/payment-methods`);
    return response.data;
  }

  async getUserNotificationSettings(userId) {
    const response = await api.get(`/admin/users/${userId}/notification-settings`);
    return response.data;
  }

  async updateUserNotificationSettings(userId, payload) {
    const response = await api.put(`/admin/users/${userId}/notification-settings`, payload);
    return response.data;
  }

  async updateUserStatus(userId, status, reason) {
    const response = await api.put(`/admin/users/${userId}/status`, { status, reason });
    return response.data;
  }

  // Create new user (for call center operations)
  async createUser(userData) {
    const response = await api.post('/admin/users', userData);
    return response.data;
  }

  async updateUser(userId, userData) {
    const response = await api.put(`/admin/users/${userId}`, userData);
    return response.data;
  }

  // Unlock a locked user account
  async unlockUserAccount(userId) {
    const response = await api.post(`/admin/users/${userId}/unlock`);
    return response.data;
  }

  // User address management
  async getUserAddresses(userId) {
    const response = await api.get(`/admin/users/${userId}/addresses`);
    return response.data;
  }

  async getUserCart(userId) {
    const response = await api.get(`/admin/users/${userId}/cart`);
    return response.data;
  }

  async createUserAddress(userId, addressData) {
    const response = await api.post(`/admin/users/${userId}/addresses`, addressData);
    return response.data;
  }

  async updateUserAddress(userId, addressId, addressData) {
    const response = await api.put(`/admin/users/${userId}/addresses/${addressId}`, addressData);
    return response.data;
  }

  async deleteUserAddress(userId, addressId) {
    const response = await api.delete(`/admin/users/${userId}/addresses/${addressId}`);
    return response.data;
  }

  // Create order for user (call center operations)
  async createOrderForUser(orderData) {
    const response = await api.post('/admin/orders', orderData);
    return response.data;
  }

  // Order management
  async getOrders(params = {}) {
    const response = await api.get('/admin/orders', { params });
    return response.data;
  }

  async getOrderDetails(orderId) {
    const response = await api.get(`/admin/orders/${orderId}`);
    return response.data;
  }

  async retryPaymentFiscalization(paymentId) {
    const response = await api.post(`/admin/payments/${paymentId}/fiscalization/retry`);
    return response.data;
  }

  async updateOrderStatus(orderId, status, notes, { bottles_returned } = {}) {
    const body = { status, notes };
    if (bottles_returned != null) body.bottles_returned = bottles_returned;
    const response = await api.put(`/admin/orders/${orderId}/status`, body);
    return response.data;
  }

  async recordStaffCashCollection(payload) {
    const response = await api.post('/admin/staff/cash-reconciliation/collections', payload);
    return response.data;
  }

  async previewPersonalCardTransfer(orderId, payload) {
    const response = await api.post(`/admin/orders/${orderId}/personal-card-transfer/preview`, payload);
    return response.data;
  }

  async getPaymentMethods(context = 'order') {
    const response = await api.get('/payments/methods', { params: { context } });
    return response.data?.data?.available_methods || [];
  }

  // Order edit (admin)
  async previewOrderEdit(orderId, payload) {
    const response = await api.post(`/admin/orders/${orderId}/edit-preview`, payload);
    return response.data;
  }

  async submitOrderEdit(orderId, payload) {
    const response = await api.post(`/admin/orders/${orderId}/edit`, payload);
    return response.data;
  }

  async previewCollectedCashEdit(orderId, payload) {
    const response = await api.post(`/admin/orders/${orderId}/collected-cash/preview`, payload);
    return response.data;
  }

  async editCollectedCash(orderId, payload) {
    const response = await api.post(`/admin/orders/${orderId}/collected-cash`, payload);
    return response.data;
  }

  async previewOrderPaymentMethod(orderId, payload) {
    const response = await api.post(`/admin/orders/${orderId}/payment-method/preview`, payload);
    return response.data;
  }

  async submitOrderPaymentMethod(orderId, payload) {
    const response = await api.post(`/admin/orders/${orderId}/payment-method`, payload);
    return response.data;
  }

  async getOrderEditHistory(orderId) {
    const response = await api.get(`/admin/orders/${orderId}/edit-history`);
    return response.data;
  }

  // Corporate contract management
  async getCorporateContracts(params = {}) {
    const response = await api.get('/admin/corporate/contracts', { params });
    return response.data;
  }

  async createCorporateContract(contractData) {
    const response = await api.post('/admin/corporate/contracts', contractData);
    return response.data;
  }

  async getCorporateContract(contractId) {
    const response = await api.get(`/admin/corporate/contracts/${contractId}`);
    return response.data;
  }

  async updateCorporateContract(contractId, contractData) {
    const response = await api.put(`/admin/corporate/contracts/${contractId}`, contractData);
    return response.data;
  }

  async updateCorporateContractPrices(contractId, prices) {
    const response = await api.put(`/admin/corporate/contracts/${contractId}/prices`, { prices });
    return response.data;
  }

  async previewCorporateContractOverlaps(previewData) {
    const response = await api.post('/admin/corporate/contracts/overlap-preview', previewData);
    return response.data;
  }

  async topupCorporateContract(contractId, topupData) {
    const response = await api.post(`/admin/corporate/contracts/${contractId}/prepayments/topup`, topupData);
    return response.data;
  }

  async adjustCorporateContractAmount(contractId, payload) {
    const response = await api.post(`/admin/corporate/contracts/${contractId}/adjustments`, payload);
    return response.data;
  }

  async getCorporateContractBalance(contractId) {
    const response = await api.get(`/admin/corporate/contracts/${contractId}/balance`);
    return response.data;
  }

  async getCorporateContractLedger(contractId, params = {}) {
    const response = await api.get(`/admin/corporate/contracts/${contractId}/ledger`, { params });
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

  async listProductMarkingCodes(productId, params = {}) {
    const response = await api.get(`/admin/products/${productId}/marking-codes`, { params });
    return response.data;
  }

  async createProductMarkingCodes(productId, payload) {
    const response = await api.post(`/admin/products/${productId}/marking-codes`, payload);
    return response.data;
  }

  async updateProductMarkingCode(productId, markingCodeId, payload) {
    const response = await api.put(
      `/admin/products/${productId}/marking-codes/${markingCodeId}`,
      payload,
    );
    return response.data;
  }

  async importProductMarkingCodesCsv(productId, input) {
    if (input instanceof File || input instanceof Blob) {
      const formData = new FormData();
      formData.append('file', input);

      const csrfToken = getCookie('csrf_access_token');
      const response = await api.post(
        `/admin/products/${productId}/marking-codes/import`,
        formData,
        {
          headers: {
            'Content-Type': 'multipart/form-data',
            'X-CSRF-TOKEN': csrfToken,
          },
          withCredentials: true,
        },
      );
      return response.data;
    }

    const response = await api.post(`/admin/products/${productId}/marking-codes/import`, input);
    return response.data;
  }

  async exportProductMarkingCodes(productId, params = {}) {
    const response = await api.get(`/admin/products/${productId}/marking-codes/export`, {
      params,
      responseType: 'blob',
    });
    return response.data;
  }

  // Product Category management
  async getCategories(params = {}) {
    const response = await api.get('/admin/categories', { params });
    return response.data;
  }

  async getCategory(categoryId) {
    const response = await api.get(`/admin/categories/${categoryId}`);
    return response.data;
  }

  async createCategory(categoryData) {
    const response = await api.post('/admin/categories', categoryData);
    return response.data;
  }

  async updateCategory(categoryId, categoryData) {
    const response = await api.put(`/admin/categories/${categoryId}`, categoryData);
    return response.data;
  }

  async deleteCategory(categoryId, force = false) {
    const response = await api.delete(`/admin/categories/${categoryId}`, {
      params: { force }
    });
    return response.data;
  }

  // Subscription management
  async getSubscriptions(params = {}) {
    const response = await api.get('/admin/subscriptions', { params });
    return normalizeAdminCollectionResponse(response.data);
  }

  async getSubscription(subscriptionId) {
    const response = await api.get(`/admin/subscriptions/${subscriptionId}`);
    return response.data?.data?.subscription || {};
  }

  async createSubscription(payload) {
    const response = await api.post('/admin/subscriptions', payload);
    return response.data?.data?.subscription || response.data;
  }

  async updateSubscription(subscriptionId, payload) {
    const response = await api.put(`/admin/subscriptions/${subscriptionId}`, payload);
    return response.data?.data?.subscription || response.data;
  }

  async pauseSubscription(subscriptionId, payload = {}) {
    const response = await api.post(`/admin/subscriptions/${subscriptionId}/pause`, payload);
    return response.data;
  }

  async resumeSubscription(subscriptionId, payload = {}) {
    const response = await api.post(`/admin/subscriptions/${subscriptionId}/resume`, payload);
    return response.data;
  }

  async cancelSubscription(subscriptionId, payload = {}) {
    const response = await api.post(`/admin/subscriptions/${subscriptionId}/cancel`, payload);
    return response.data;
  }

  async processSubscriptionBilling(subscriptionId) {
    const response = await api.post(`/admin/subscriptions/${subscriptionId}/billing/process`, {});
    return response.data;
  }

  async addSubscriptionItem(subscriptionId, payload) {
    const response = await api.post(`/admin/subscriptions/${subscriptionId}/items`, payload);
    return response.data;
  }

  async updateSubscriptionItem(subscriptionId, itemId, payload) {
    const response = await api.put(`/admin/subscriptions/${subscriptionId}/items/${itemId}`, payload);
    return response.data;
  }

  async removeSubscriptionItem(subscriptionId, itemId) {
    const response = await api.delete(`/admin/subscriptions/${subscriptionId}/items/${itemId}`);
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

  async redispatchDelivery(deliveryId) {
    const response = await api.post(`/admin/deliveries/${deliveryId}/redispatch`, {});
    return response.data;
  }

  // Delivery Time Slot management
  async getTimeSlots(params = {}) {
    const response = await api.get('/admin/delivery/time-slots', { params });
    return response.data;
  }

  async getTimeSlot(slotId) {
    const response = await api.get(`/admin/delivery/time-slots/${slotId}`);
    return response.data;
  }

  async createTimeSlot(slotData) {
    const response = await api.post('/admin/delivery/time-slots', slotData);
    return response.data;
  }

  async updateTimeSlot(slotId, slotData) {
    const response = await api.put(`/admin/delivery/time-slots/${slotId}`, slotData);
    return response.data;
  }

  async deleteTimeSlot(slotId) {
    const response = await api.delete(`/admin/delivery/time-slots/${slotId}`);
    return response.data;
  }

  // Loyalty Program management
  async getLoyaltyMembers(params = {}) {
    const response = await api.get('/admin/loyalty/members', { params });
    return normalizeAdminCollectionResponse(response.data);
  }

  async getLoyaltyMember(userId) {
    const response = await api.get(`/admin/loyalty/members/${userId}`);
    return response.data?.data || {};
  }

  async getLoyaltyMemberTransactions(userId, params = {}) {
    const response = await api.get(`/admin/loyalty/members/${userId}/transactions`, { params });
    return normalizeAdminCollectionResponse(response.data);
  }

  async getLoyaltyPrograms(params = {}) {
    const response = await api.get('/admin/loyalty/programs', { params });
    return normalizeAdminCollectionResponse(response.data);
  }

  async createLoyaltyProgram(programData) {
    const response = await api.post('/admin/loyalty/programs', programData);
    return response.data?.data?.program || response.data;
  }

  async updateLoyaltyProgram(programId, programData) {
    const response = await api.put(`/admin/loyalty/programs/${programId}`, programData);
    return response.data?.data?.program || response.data;
  }

  async deleteLoyaltyProgram(programId) {
    const response = await api.delete(`/admin/loyalty/programs/${programId}`);
    return response.data;
  }

  // Loyalty Tier management
  async getLoyaltyTiers(params = {}) {
    const response = await api.get('/admin/loyalty/tiers', { params });
    return normalizeAdminCollectionResponse(response.data);
  }

  async createLoyaltyTier(tierData) {
    const response = await api.post('/admin/loyalty/tiers', tierData);
    return response.data?.data?.tier || response.data;
  }

  async updateLoyaltyTier(tierId, tierData) {
    const response = await api.put(`/admin/loyalty/tiers/${tierId}`, tierData);
    return response.data?.data?.tier || response.data;
  }

  async deleteLoyaltyTier(tierId) {
    const response = await api.delete(`/admin/loyalty/tiers/${tierId}`);
    return response.data;
  }

  // Loyalty Streak Rule management
  async getLoyaltyStreakRules(params = {}) {
    const response = await api.get('/admin/loyalty/streak-rules', { params });
    return response.data?.data || { streak_rules: [], streak_rule_count: 0 };
  }

  async createLoyaltyStreakRule(ruleData) {
    const response = await api.post('/admin/loyalty/streak-rules', ruleData);
    return response.data?.data?.streak_rule || response.data;
  }

  async updateLoyaltyStreakRule(ruleId, ruleData) {
    const response = await api.put(`/admin/loyalty/streak-rules/${ruleId}`, ruleData);
    return response.data?.data?.streak_rule || response.data;
  }

  async deleteLoyaltyStreakRule(ruleId) {
    const response = await api.delete(`/admin/loyalty/streak-rules/${ruleId}`);
    return response.data;
  }

  // Loyalty Consecutive-Strike Rule management
  async getLoyaltyConsecutiveStrikeRules(params = {}) {
    const response = await api.get('/admin/loyalty/consecutive-strike-rules', { params });
    return response.data?.data || { consecutive_strike_rules: [], count: 0 };
  }

  async createLoyaltyConsecutiveStrikeRule(ruleData) {
    const response = await api.post('/admin/loyalty/consecutive-strike-rules', ruleData);
    return response.data?.data?.consecutive_strike_rule || response.data;
  }

  async updateLoyaltyConsecutiveStrikeRule(ruleId, ruleData) {
    const response = await api.put(`/admin/loyalty/consecutive-strike-rules/${ruleId}`, ruleData);
    return response.data?.data?.consecutive_strike_rule || response.data;
  }

  async deleteLoyaltyConsecutiveStrikeRule(ruleId) {
    const response = await api.delete(`/admin/loyalty/consecutive-strike-rules/${ruleId}`);
    return response.data;
  }

  async getLoyaltyRewards(params = {}) {
    const response = await api.get('/admin/loyalty/rewards', { params });
    return normalizeAdminCollectionResponse(response.data);
  }

  async getLoyaltyReward(rewardId) {
    const response = await api.get(`/admin/loyalty/rewards/${rewardId}`);
    return response.data?.data?.reward || {};
  }

  async createLoyaltyReward(rewardData) {
    const response = await api.post('/admin/loyalty/rewards', rewardData);
    return response.data?.data?.reward || response.data;
  }

  async updateLoyaltyReward(rewardId, rewardData) {
    const response = await api.put(`/admin/loyalty/rewards/${rewardId}`, rewardData);
    return response.data?.data?.reward || response.data;
  }

  async deleteLoyaltyReward(rewardId) {
    const response = await api.delete(`/admin/loyalty/rewards/${rewardId}`);
    return response.data;
  }

  async getLoyaltyAnalytics(params = {}) {
    const response = await api.get('/admin/loyalty/analytics', { params });
    return response.data?.data || {};
  }

  // Notification management
  async getNotificationCampaigns(params = {}) {
    const response = await api.get('/admin/notification-campaigns', { params });
    return normalizeNotificationCampaignCollection(response.data);
  }

  async getNotificationCampaign(campaignId) {
    const response = await api.get(`/admin/notification-campaigns/${campaignId}`);
    return normalizeNotificationCampaign(response.data?.data?.campaign || {});
  }

  async getNotificationTemplates(params = {}) {
    const response = await api.get('/admin/notification-templates', { params });
    return normalizeNotificationTemplateCollection(response.data);
  }

  async getNotificationTemplate(templateId) {
    const response = await api.get(`/admin/notification-templates/${templateId}`);
    return normalizeNotificationTemplate(response.data?.data?.template || {});
  }

  async createNotificationCampaign(campaignData) {
    const response = await api.post('/admin/notification-campaigns', campaignData);
    return normalizeNotificationCampaign(response.data?.data?.campaign || {});
  }

  async createNotificationTemplate(templateData) {
    const response = await api.post('/admin/notification-templates', templateData);
    return normalizeNotificationTemplate(response.data?.data?.template || {});
  }

  async updateNotificationCampaign(campaignId, campaignData) {
    const response = await api.put(`/admin/notification-campaigns/${campaignId}`, campaignData);
    return normalizeNotificationCampaign(response.data?.data?.campaign || {});
  }

  async deleteNotificationCampaign(campaignId) {
    const response = await api.delete(`/admin/notification-campaigns/${campaignId}`);
    return response.data;
  }

  async duplicateNotificationCampaign(campaignId) {
    const response = await api.post(`/admin/notification-campaigns/${campaignId}/duplicate`);
    return normalizeNotificationCampaign(response.data?.data?.campaign || {});
  }

  async sendNotificationCampaign(campaignId, payload = {}) {
    const response = await api.post(`/admin/notification-campaigns/${campaignId}/send`, payload);
    return normalizeNotificationCampaign(response.data?.data?.campaign || {});
  }

  async cancelNotificationCampaign(campaignId) {
    const response = await api.post(`/admin/notification-campaigns/${campaignId}/cancel`);
    return normalizeNotificationCampaign(response.data?.data?.campaign || {});
  }

  async updateNotificationTemplate(templateId, templateData) {
    const response = await api.put(`/admin/notification-templates/${templateId}`, templateData);
    return normalizeNotificationTemplate(response.data?.data?.template || {});
  }

  async deleteNotificationTemplate(templateId, reactivate = false) {
    const response = await api.delete(`/admin/notification-templates/${templateId}`, {
      data: reactivate ? { reactivate: true } : undefined
    });
    return normalizeNotificationTemplate(response.data?.data?.template || {});
  }

  async previewNotificationTemplate(templateId, payload = {}) {
    const response = await api.post(`/admin/notification-templates/${templateId}/preview`, payload);
    return response.data?.data?.preview || {};
  }

  async testSendNotificationTemplate(templateId, payload = {}) {
    const response = await api.post(`/admin/notification-templates/${templateId}/test-send`, payload);
    return response.data?.data?.test_send || {};
  }

  async getNotificationTemplateTypes() {
    const response = await api.get('/admin/notification-templates/types');
    return response.data?.data?.types || [];
  }

  async getNotificationTemplateChannels() {
    const response = await api.get('/admin/notification-templates/channels');
    return response.data?.data?.channels || [];
  }

  async getNotificationCampaignSegments() {
    const response = await api.get('/admin/notification-campaign-segments');
    return response.data?.data?.segments || [];
  }

  // Advanced Analytics
  async getAnalytics(params = {}) {
    const queryParams = buildAnalyticsParams(params);
    const [dashboardResponse, productsResponse, customersResponse] = await Promise.all([
      api.get('/analytics/dashboard', { params: queryParams }),
      api.get('/analytics/products', { params: queryParams }),
      api.get('/analytics/customers', { params: queryParams })
    ]);

    return normalizeOverviewAnalytics(
      dashboardResponse.data,
      productsResponse.data,
      customersResponse.data
    );
  }

  async getSalesTrends(params = {}) {
    const queryParams = buildAnalyticsParams(params);
    const [revenueResponse, conversionResponse] = await Promise.all([
      api.get('/analytics/revenue', {
        params: {
          ...queryParams,
          granularity: 'daily'
        }
      }),
      api.get('/analytics/conversion-funnel', { params: queryParams })
    ]);

    return normalizeSalesAnalytics(revenueResponse.data, conversionResponse.data);
  }

  async getChurnPrediction(params = {}) {
    const response = await api.get('/analytics/predictions', {
      params: {
        ...buildAnalyticsParams(params),
        type: 'churn'
      }
    });
    return normalizeChurnPrediction(response.data);
  }

  async getDeliveryHeatmap(params = {}) {
    const response = await api.get('/analytics/delivery', {
      params: buildAnalyticsParams(params)
    });
    return normalizeDeliveryAnalytics(response.data);
  }

  async getRevenueForecast(params = {}) {
    const response = await api.get('/analytics/predictions', {
      params: {
        ...buildAnalyticsParams(params),
        type: 'revenue',
        horizon: 90
      }
    });
    return normalizeRevenueForecast(response.data);
  }

  async getInactiveCustomers(params = {}) {
    const response = await api.get('/admin/analytics/inactive-customers', { params });
    const payload = response.data?.data || {};
    return {
      items: payload.items || [],
      meta: response.data?.meta || { page: 1, per_page: 50, total: 0 }
    };
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
    if (!entityType) {
      throw new Error('Entity type is required');
    }
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

  // --- Bottle Tracking ---

  async getBottleDashboard() {
    const response = await api.get('/admin/bottles/dashboard');
    return response.data;
  }

  async getBottleBalances(params = {}) {
    const response = await api.get('/admin/bottles/balances', { params });
    return response.data;
  }

  async getCustomerBottleBalances(userId) {
    const response = await api.get(`/admin/bottles/balances/${userId}`);
    return response.data;
  }

  async getBottleLedger(params = {}) {
    const response = await api.get('/admin/bottles/ledger', { params });
    return response.data;
  }

  async getBottleLedgerForAddress(userId, addressId, params = {}) {
    const response = await api.get(`/admin/bottles/ledger/${userId}/${addressId}`, { params });
    return response.data;
  }

  async createBottleAdjustment(data) {
    const response = await api.post('/admin/bottles/adjustment', data);
    return response.data;
  }

  async setBottleInitialBalance(data) {
    const response = await api.post('/admin/bottles/initial-balance', data);
    return response.data;
  }

  async getBottleFines(params = {}) {
    const response = await api.get('/admin/bottles/fines', { params });
    return response.data;
  }

  async createBottleFine(data) {
    const response = await api.post('/admin/bottles/fines', data);
    return response.data;
  }

  async updateBottleFine(fineId, data) {
    const response = await api.put(`/admin/bottles/fines/${fineId}`, data);
    return response.data;
  }

  async reconcileBottleBalance(userId, addressId) {
    const response = await api.post(`/admin/bottles/reconcile/${userId}/${addressId}`);
    return response.data;
  }

  // --- Bottle Sessions ---

  async getBottleSessions(params = {}) {
    const response = await api.get('/admin/bottles/sessions', { params });
    return response.data;
  }

  async getBottleSession(sessionId) {
    const response = await api.get(`/admin/bottles/sessions/${sessionId}`);
    return response.data;
  }

  async forceCloseBottleSession(sessionId, data) {
    const response = await api.post(`/admin/bottles/sessions/${sessionId}/force-close`, data);
    return response.data;
  }

  // --- Bottle Transfers ---

  async getBottleTransfers(params = {}) {
    const response = await api.get('/admin/bottles/transfers', { params });
    return response.data;
  }

  async resolveBottleTransferDispute(transferId, data) {
    const response = await api.post(`/admin/bottles/transfers/${transferId}/resolve`, data);
    return response.data;
  }

  // --- Marking-code utilisation task ---

  async getMarkingCodeTaskConfig() {
    const response = await api.get('/admin/marking-code-task/config');
    return response.data;
  }

  async updateMarkingCodeTaskConfig(payload) {
    const response = await api.put('/admin/marking-code-task/config', payload);
    return response.data;
  }

  async updateProductMarkingCodeOverrides(productId, payload) {
    const response = await api.put(
      `/admin/marking-code-task/config/products/${productId}`,
      payload,
    );
    return response.data;
  }

  async listMarkingCodeTaskRuns(params = {}) {
    const response = await api.get('/admin/marking-code-task/runs', { params });
    return response.data;
  }

  async getMarkingCodeTaskRun(runId) {
    const response = await api.get(`/admin/marking-code-task/runs/${runId}`);
    return response.data;
  }

  async getMarkingCodeTaskStats(days = 7) {
    const response = await api.get('/admin/marking-code-task/stats', {
      params: { days },
    });
    return response.data;
  }

  async getMarkingCodePoolStatus() {
    const response = await api.get('/admin/marking-code-task/pool-status');
    return response.data;
  }

  async triggerMarkingCodeTaskRun(payload) {
    const response = await api.post('/admin/marking-code-task/run', payload);
    return response.data;
  }

  // Support inbox
  async getSupportConversations(params = {}) {
    const response = await api.get('/admin/support/conversations', { params });
    return response.data;
  }

  async getSupportThread(conversationId, params = {}) {
    const response = await api.get(`/admin/support/conversations/${conversationId}/messages`, { params });
    return response.data;
  }

  async markSupportRead(conversationId) {
    const response = await api.post(`/admin/support/conversations/${conversationId}/read`);
    return response.data;
  }

  async replySupportMessage(conversationId, content) {
    const response = await api.post(`/admin/support/conversations/${conversationId}/reply`, { content });
    return response.data;
  }

  async startSupportConversation(userId, content) {
    const response = await api.post('/admin/support/conversations', { user_id: userId, content });
    return response.data;
  }
}

const adminService = new AdminService();

export default adminService;
