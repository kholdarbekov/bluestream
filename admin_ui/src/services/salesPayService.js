import api from './api';

/**
 * Sales-agent pay (spec §5.2, §5.3, §6.4). One method per route, nothing else.
 *
 * Every pay route answers one `success_response` envelope, so `unwrap` (the salesService.js
 * adapter) is the whole reader. A9, A22 and M1 carry their paging `meta` inside `data`.
 * The UI computes no figure and picks no month: every value a screen shows came back from here.
 */
const unwrap = (response) => response?.data?.data || response?.data || {};

// The §4.16 refusals a pay modal explains in the admin's language (§6.5). A request that names
// them gets no interceptor toast for them, so the admin reads one message, not two. Pinned to the
// literal raise sites by tests/unit/test_admin_ui_payload_fixture_contracts.py.
export const PAY_HANDLED_CODES = [
  'SALES_PAY_MONTH_INVALID',
  'SALES_PAY_NOT_STARTED',
  'SALES_PAY_ALREADY_STARTED',
  'SALES_PAY_MONTH_LOCKED',
  'SALES_PAY_NOT_FOUND',
  'SALES_PAY_STATE_INVALID',
  'SALES_PAY_PERIOD_NOT_ENDED',
  'SALES_PAY_PREVIOUS_PERIOD_OPEN',
  'SALES_PAY_PREVIOUS_PERIOD_NOT_APPROVED',
  'SALES_PAY_SYNC_INCOMPLETE',
  'SALES_PAY_TERMS_MISSING',
  'SALES_PAY_PLAN_INVALID',
  'SALES_PAY_TERMS_INVALID',
  'SALES_PAY_DATE_INVALID',
  'SALES_PAY_AMOUNT_INVALID',
  'SALES_PAY_REASON_REQUIRED',
  'SALES_PAY_PENALTY_TYPE_INACTIVE',
  'SALES_PAY_SELF_DECISION',
];

const HANDLED = { handledErrorCodes: PAY_HANDLED_CODES };
const PAY = '/admin/sales/pay';
const periodUrl = (month) => `${PAY}/periods/${month}`;
const statementUrl = (month, agentId) => `${periodUrl(month)}/agents/${agentId}`;

class SalesPayService {
  // --- Months (A0-A7) ---
  async getPeriods() {
    return unwrap(await api.get(`${PAY}/periods`));
  }

  async startPay({ month, isShadow }) {
    return unwrap(await api.post(`${PAY}/start`, { month, is_shadow: isShadow }, HANDLED));
  }

  async getPeriod(month) {
    return unwrap(await api.get(periodUrl(month)));
  }

  async closePeriod(month) {
    return unwrap(await api.post(`${periodUrl(month)}/close`, {}, HANDLED));
  }

  async recalculatePeriod(month) {
    return unwrap(await api.post(`${periodUrl(month)}/recalculate`, {}, HANDLED));
  }

  async approvePeriod(month) {
    return unwrap(await api.post(`${periodUrl(month)}/approve`, {}, HANDLED));
  }

  async markPeriodPaid(month, paidOn) {
    return unwrap(await api.post(`${periodUrl(month)}/mark-paid`, { paid_on: paidOn }, HANDLED));
  }

  async setHolidays(month, days) {
    return unwrap(await api.put(`${periodUrl(month)}/holidays`, { days }, HANDLED));
  }

  // --- One agent in one month (A8-A11) ---
  async getStatement(month, agentId) {
    return unwrap(await api.get(statementUrl(month, agentId)));
  }

  async getStatementLines(month, agentId, { kind, page, perPage }) {
    return unwrap(await api.get(`${statementUrl(month, agentId)}/lines`, { params: { kind, page, per_page: perPage } }));
  }

  async setUnpaidDays(month, agentId, days) {
    return unwrap(await api.put(`${statementUrl(month, agentId)}/unpaid-days`, { days }, HANDLED));
  }

  // `month` is always a published month: the drawer's own month, or A16's `nets_in` for a
  // recorded repayment (Q15). Never computed by the caller.
  async createAdjustment(month, agentId, { amount, reason }) {
    return unwrap(await api.post(`${statementUrl(month, agentId)}/adjustments`, { amount, reason }, HANDLED));
  }

  // --- Plans (A12-A15) ---
  async getPlans() {
    return unwrap(await api.get(`${PAY}/plans`));
  }

  async createPlan({ name, version }) {
    return unwrap(await api.post(`${PAY}/plans`, { name, version }, HANDLED));
  }

  async getPlanVersion(planId, versionId) {
    return unwrap(await api.get(`${PAY}/plans/${planId}/versions/${versionId}`));
  }

  async createPlanVersion(planId, version) {
    return unwrap(await api.post(`${PAY}/plans/${planId}/versions`, version, HANDLED));
  }

  // --- Agent terms and employment (A16-A18) ---
  async getAgentTerms(agentId) {
    return unwrap(await api.get(`${PAY}/agents/${agentId}/terms`));
  }

  async addAgentTerms(agentId, payload) {
    return unwrap(await api.post(`${PAY}/agents/${agentId}/terms`, payload, HANDLED));
  }

  async setEmployment(agentId, { start, end }) {
    return unwrap(await api.put(`${PAY}/agents/${agentId}/employment`, { start, end }, HANDLED));
  }

  // --- Penalty types (A19-A21) ---
  async getPenaltyTypes() {
    return unwrap(await api.get(`${PAY}/penalty-types`));
  }

  async createPenaltyType(payload) {
    return unwrap(await api.post(`${PAY}/penalty-types`, payload, HANDLED));
  }

  async updatePenaltyType(typeId, payload) {
    return unwrap(await api.patch(`${PAY}/penalty-types/${typeId}`, payload, HANDLED));
  }

  // --- Penalties (A22-A26) ---
  async getPenalties({ status, agentId, month, page, perPage }) {
    return unwrap(await api.get(`${PAY}/penalties`, {
      params: { status, agent_id: agentId, month, page, per_page: perPage },
    }));
  }

  async createPenalty(payload) {
    return unwrap(await api.post(`${PAY}/penalties`, payload, HANDLED));
  }

  async confirmPenalty(id, { amount }) {
    return unwrap(await api.post(`${PAY}/penalties/${id}/confirm`, { amount }, HANDLED));
  }

  async rejectPenalty(id, note) {
    return unwrap(await api.post(`${PAY}/penalties/${id}/reject`, { note }, HANDLED));
  }

  async cancelPenalty(id, note) {
    return unwrap(await api.post(`${PAY}/penalties/${id}/cancel`, { note }, HANDLED));
  }

  // --- Manager proposals (M1-M2; the page is Task 13's) ---
  async getPenaltyProposals({ status, agentId, page, perPage }) {
    return unwrap(await api.get('/admin/sales/penalty-proposals', {
      params: { status, agent_id: agentId, page, per_page: perPage },
    }));
  }

  async proposePenalty(payload) {
    return unwrap(await api.post('/admin/sales/penalty-proposals', payload, HANDLED));
  }
}

export default new SalesPayService();
