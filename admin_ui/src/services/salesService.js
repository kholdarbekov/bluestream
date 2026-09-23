import api from './api';

/**
 * Sales module (outlets) API client.
 *
 * Unwraps to `response.data.data` so pages read `{ items }` / `{ outlet }` directly, with one
 * exception: `getOutlets` also lifts `meta` (total + the per-stage `summary`), because the list
 * route answers through `paginated_response` and the page needs both halves.
 */
const unwrap = (response) => response?.data?.data || response?.data || {};

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
    const response = await api.put(`/admin/sales/outlets/${outletId}`, payload);
    return unwrap(response);
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
    const response = await api.post(`/admin/sales/outlets/${outletId}/assign`, { agent_user_id: agentUserId });
    return unwrap(response);
  }

  async markLost(outletId, reason, note = null) {
    const response = await api.post(`/admin/sales/outlets/${outletId}/mark-lost`, { reason, note });
    return unwrap(response);
  }

  async bulkAssign(district, agentUserId) {
    const response = await api.post('/admin/sales/outlets/bulk-assign', { district, agent_user_id: agentUserId });
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
}

export default new SalesService();
