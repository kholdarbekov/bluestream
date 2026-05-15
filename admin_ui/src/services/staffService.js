/**
 * Staff Service for Admin Panel
 * API client for staff management operations (Phase 6)
 */
import api from './api';

class StaffServiceClass {
  // ─── Delivery Persons ─────────────────────────────────

  /**
   * List delivery persons with filtering and pagination
   * @param {Object} params - { page, per_page, search, status, available }
   */
  getDeliveryPersons(params = {}) {
    return api.get('/admin/staff/delivery-persons', { params });
  }

  /**
   * Create delivery person (user + delivery profile)
   * @param {Object} payload
   */
  createDeliveryPerson(payload) {
    return api.post('/admin/staff/delivery-persons', payload);
  }

  /**
   * Get delivery person details with performance stats
   * @param {number} id - DeliveryPerson ID
   */
  getDeliveryPerson(id) {
    return api.get(`/admin/staff/delivery-persons/${id}`);
  }

  /**
   * Update delivery person profile
   * @param {number} id - DeliveryPerson ID
   * @param {Object} payload
   */
  updateDeliveryPerson(id, payload) {
    return api.put(`/admin/staff/delivery-persons/${id}`, payload);
  }

  /**
   * Toggle notification muting for a delivery person
   * @param {number} id - DeliveryPerson ID
   * @param {boolean} muted - true to mute, false to unmute
   */
  muteNotifications(id, muted) {
    return api.put(`/admin/staff/delivery-persons/${id}/mute`, { muted });
  }

  // ─── Operators ────────────────────────────────────────

  /**
   * List operators with filtering and pagination
   * @param {Object} params - { page, per_page, search, status }
   */
  getOperators(params = {}) {
    return api.get('/admin/staff/operators', { params });
  }

  /**
   * Create operator account (or add operator role to existing user)
   * @param {Object} payload
   */
  createOperator(payload) {
    return api.post('/admin/staff/operators', payload);
  }

  /**
   * Update operator profile and roles
   * @param {number} id - User ID
   * @param {Object} payload
   */
  updateOperator(id, payload) {
    return api.put(`/admin/staff/operators/${id}`, payload);
  }

  /**
   * Update staff roles (dual-role management)
   * @param {number} userId
   * @param {string[]} staffRoles
   */
  updateStaffRoles(userId, staffRoles) {
    return api.put(`/admin/staff/users/${userId}/roles`, { staff_roles: staffRoles });
  }

  // ─── Staff Overview ───────────────────────────────────

  /**
   * Get unified staff overview (all roles)
   */
  getStaffOverview() {
    return api.get('/admin/staff/overview');
  }

  // ─── Delivery Assignment ──────────────────────────────

  /**
   * Assign a delivery to a delivery person
   * @param {number} deliveryId
   * @param {number} deliveryPersonId - user_id of the delivery person
   */
  assignDelivery(deliveryId, deliveryPersonId) {
    return api.post(`/admin/staff/delivery/assign/${deliveryId}`, {
      delivery_person_id: deliveryPersonId,
    });
  }

  /**
   * Reassign a delivery to a different delivery person
   * @param {number} deliveryId
   * @param {number} newDeliveryPersonId - user_id of the new person
   */
  reassignDelivery(deliveryId, newDeliveryPersonId) {
    return api.put(`/admin/staff/delivery/reassign/${deliveryId}`, {
      new_delivery_person_id: newDeliveryPersonId,
    });
  }

  // ─── Cash Reconciliation ──────────────────────────────

  /**
   * Get cash reconciliation report
   * @param {Object} params - { period: 'day'|'week'|'month', driver_id?, status?, warning_only? }
   */
  getCashReconciliation(params = {}) {
    return api.get('/admin/staff/cash-reconciliation', { params });
  }

  recordCashCollection(payload) {
    return api.post('/admin/staff/cash-reconciliation/collections', payload);
  }

  searchCodCollectionUsers(params = {}) {
    return api.get('/admin/staff/cash-reconciliation/users/search', { params });
  }

  getCodCollectionUsersWithOpenDebts(params = {}) {
    return api.get('/admin/staff/cash-reconciliation/users/with-open-cod', { params });
  }

  getCashReconciliationSession(sessionId) {
    return api.get(`/admin/staff/cash-reconciliation/sessions/${sessionId}`);
  }

  verifyCashReconciliationSession(sessionId, payload) {
    return api.post(`/admin/staff/cash-reconciliation/sessions/${sessionId}/verify`, payload);
  }

  resolveCashReconciliationSession(sessionId, payload) {
    return api.post(`/admin/staff/cash-reconciliation/sessions/${sessionId}/resolve`, payload);
  }

  getCustomerCodStatement(customerId) {
    return api.get(`/admin/staff/cash-reconciliation/customers/${customerId}/statement`);
  }

  /**
   * Fetch a customer's full COD cash-collection ledger.
   * @param {number} customerId
   * @param {Object} [params] - { include_voided: 0|1, include_fully_applied: 0|1, limit }
   */
  getCustomerPrepaymentHistory(customerId, params = {}) {
    return api.get(
      `/admin/staff/cash-reconciliation/customers/${customerId}/prepayment-history`,
      { params },
    );
  }

  /**
   * List customers carrying an unapplied COD over-collection (prepayment) balance.
   * @param {Object} [params] - { limit, search }
   */
  listCustomersWithPrepaymentBalance(params = {}) {
    return api.get(
      '/admin/staff/cash-reconciliation/customers/with-prepayment-balance',
      { params },
    );
  }

  getOrderPaymentTimeline(orderId) {
    return api.get(`/admin/staff/cash-reconciliation/orders/${orderId}/timeline`);
  }

  // ─── Invite Link ──────────────────────────────────────

  /**
   * Generate staff bot invite link
   * @param {Object|string} payloadOrRole - payload object or role string
   * @param {number|null} userId - user id when role string is used
   */
  generateInviteLink(payloadOrRole = 'delivery_driver', userId = null) {
    let payload;
    if (typeof payloadOrRole === 'object' && payloadOrRole !== null) {
      payload = { ...payloadOrRole };
    } else {
      payload = { role: payloadOrRole };
      if (userId) {
        payload.user_id = userId;
      }
    }
    return api.post('/admin/staff/invite-link', payload);
  }
}

const staffService = new StaffServiceClass();
export default staffService;
