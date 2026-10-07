import api from './api';

/**
 * Sales module (outlets) API client.
 *
 * Unwraps to `response.data.data` so pages read `{ items }` / `{ outlet }` directly, with one
 * exception: `getOutlets` also lifts `meta` (total + the per-stage `summary`), because the list
 * route answers through `paginated_response` and the page needs both halves.
 */
const unwrap = (response) => response?.data?.data || response?.data || {};

// C14 (spec §6.7): the refusals the order-approval queue explains itself, inline, in the admin's
// language. Both decisions name them, so api.js does not toast them as well: one refusal, one
// message. Held to the literal raise sites by tests/unit/test_admin_ui_payload_fixture_contracts.py.
export const ORDER_APPROVAL_HANDLED_CODES = [
  'SALES_ORDER_APPROVAL_NOT_FOUND',
  'SALES_ORDER_APPROVAL_NOT_PENDING',
  'SALES_PAY_SELF_DECISION',
  'ADMIN_REASON_REQUIRED',
  'ADMIN_REASON_TOO_LONG',
  'INVENTORY_CONFIRMATION_FAILED',
];

// Final-review I1: a manager who holds an agent profile may not change his own visit plan (class,
// cadence, assignment, lost), and the Outlets page explains that refusal itself, so api.js does not
// toast it as well.
export const OUTLET_PLAN_HANDLED_CODES = ['SALES_PAY_SELF_DECISION'];

class SalesService {
  async getOutlets(params = {}) {
    const response = await api.get('/admin/sales/outlets', { params });
    const meta = response?.data?.meta || {};
    return { ...unwrap(response), total: meta.total, page: meta.page, per_page: meta.per_page, summary: meta.summary || {} };
  }

  // Deliberately UNPAGINATED server-side: `?format=pins` answers every matching pinned outlet,
  // because a truncated map is indistinguishable from a district that has no outlets at all.
  async getPins(params = {}) {
    const response = await api.get('/admin/sales/outlets', { params: { ...params, format: 'pins' } });
    return unwrap(response)?.pins || [];
  }

  async getOutlet(outletId) {
    const response = await api.get(`/admin/sales/outlets/${outletId}`);
    return unwrap(response);
  }

  async updateOutlet(outletId, payload) {
    const response = await api.put(`/admin/sales/outlets/${outletId}`, payload, {
      handledErrorCodes: OUTLET_PLAN_HANDLED_CODES,
    });
    return unwrap(response);
  }

  async addOutletContact(outletId, payload) {
    const response = await api.post(`/admin/sales/outlets/${outletId}/contacts`, payload);
    return unwrap(response);
  }

  async updateOutletContact(outletId, contactId, payload) {
    const response = await api.put(`/admin/sales/outlets/${outletId}/contacts/${contactId}`, payload);
    return unwrap(response);
  }

  async deleteOutletContact(outletId, contactId) {
    const response = await api.delete(`/admin/sales/outlets/${outletId}/contacts/${contactId}`);
    return unwrap(response);
  }

  // `meta` rides inside `data`, the visits-route shape, so `unwrap` is the whole adapter.
  async getOutletPhotos(outletId, params = {}) {
    const response = await api.get(`/admin/sales/outlets/${outletId}/photos`, { params });
    return unwrap(response);
  }

  // A photo id only (D27): the backend resolves the Telegram file id from its own row.
  async getVisitPhotoBlob(photoId) {
    const response = await api.get(`/admin/sales/visit-photos/${photoId}/file`, { responseType: 'blob' });
    return response.data;
  }

  // D25: ONE body shape, both keys always present. `attach` decides whether approval CREATES a
  // customer account or joins the outlet to the account the backend matched by phone and
  // published as `account_candidate` on the outlet GET — this client never looks a phone up.
  // `contract_number` stays optional; the route's `payload.get("contract_number")` has always
  // read a missing key and an explicit null the same way.
  async approveOutlet(outletId, { contract_number = null, attach = false } = {}) {
    const response = await api.post(`/admin/sales/outlets/${outletId}/approve`, {
      contract_number: contract_number || null,
      attach: Boolean(attach),
    });
    return unwrap(response);
  }

  async rejectOutlet(outletId, reason) {
    const response = await api.post(`/admin/sales/outlets/${outletId}/reject`, { reason });
    return unwrap(response);
  }

  async assignOutlet(outletId, agentUserId) {
    const response = await api.post(`/admin/sales/outlets/${outletId}/assign`, { agent_user_id: agentUserId }, {
      handledErrorCodes: OUTLET_PLAN_HANDLED_CODES,
    });
    return unwrap(response);
  }

  async markLost(outletId, reason, note = null) {
    const response = await api.post(`/admin/sales/outlets/${outletId}/mark-lost`, { reason, note }, {
      handledErrorCodes: OUTLET_PLAN_HANDLED_CODES,
    });
    return unwrap(response);
  }

  async bulkAssign(district, agentUserId) {
    const response = await api.post('/admin/sales/outlets/bulk-assign', { district, agent_user_id: agentUserId }, {
      handledErrorCodes: OUTLET_PLAN_HANDLED_CODES,
    });
    return unwrap(response);
  }

  async importExistingCustomers() {
    const response = await api.post('/admin/sales/outlets/import-existing-customers', {});
    return unwrap(response);
  }

  // --- Phase 3 read surfaces (admin only) ------------------------------------------------
  // Each route answers ONE `success_response` envelope whose `data` carries the rows, the paging
  // `meta` and the echoed window together, so `unwrap` is the whole adapter. `getOutlets` above
  // is the exception, not the pattern: its route answers through `paginated_response`, which puts
  // `meta` beside `data` rather than inside it.
  async getVisits(params = {}) {
    const response = await api.get('/admin/sales/visits', { params });
    return unwrap(response);
  }

  async getPlanVsFact(params = {}) {
    const response = await api.get('/admin/sales/plan-vs-fact', { params });
    return unwrap(response);
  }

  async getExceptions(params = {}) {
    const response = await api.get('/admin/sales/exceptions', { params });
    return unwrap(response);
  }

  // `userId` is a users.id, never a sales_agent_profiles.id — the route is spelled
  // `/agents/<int:user_id>/metrics` for the same reason.
  async getAgentMetrics(userId, params = {}) {
    const response = await api.get(`/admin/sales/agents/${userId}/metrics`, { params });
    return unwrap(response);
  }

  async getAgentsMetrics(params = {}) {
    const response = await api.get('/admin/sales/agents/metrics', { params });
    return unwrap(response);
  }

  // --- Same-day order approvals (C14, spec §5.7) -------------------------------------------
  // `meta`, `statuses` and `pending_count` ride inside `data`, so `unwrap` is the whole adapter.
  async listOrderApprovals({ status, agentId, page, perPage } = {}) {
    const response = await api.get('/admin/sales/order-approvals', {
      params: { status, agent_id: agentId, page, per_page: perPage },
    });
    return unwrap(response);
  }

  async approveAgentOrder(orderId) {
    const response = await api.post(`/admin/sales/order-approvals/${orderId}/approve`, {}, {
      handledErrorCodes: ORDER_APPROVAL_HANDLED_CODES,
    });
    return unwrap(response);
  }

  // The reason travels as typed: `require_admin_reason` strips it and refuses a blank or an
  // over-long one (ADMIN_REASON_REQUIRED / ADMIN_REASON_TOO_LONG), shown inline by the page.
  async rejectAgentOrder(orderId, reason) {
    const response = await api.post(`/admin/sales/order-approvals/${orderId}/reject`, { reason }, {
      handledErrorCodes: ORDER_APPROVAL_HANDLED_CODES,
    });
    return unwrap(response);
  }
}

export default new SalesService();
