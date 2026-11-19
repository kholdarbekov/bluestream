import api from './api';

/**
 * Driver cash expenses (spec 2026-11-17 §6.8): the Driver expenses page's calls. Every call answers
 * `response.data.data`, so pages read `{ items, pending_count, ... }` and `{ expense, warnings }` directly.
 */
const unwrap = (response) => response?.data?.data || {};

// The two refusals the page explains itself, in the admin's language (`block.<code>` copy), so api.js does not toast
// them as well. A decision refused for a reason the page could not know (another admin decided first) names its
// `details.block_code`; deciding on your own claim is its own code.
export const DRIVER_EXPENSE_HANDLED_CODES = ['DRIVER_EXPENSE_DECISION_BLOCKED', 'DRIVER_EXPENSE_SELF_DECISION'];

const decide = (id, action, body) => api.post(`/admin/driver-expenses/${id}/${action}`, body, {
  handledErrorCodes: DRIVER_EXPENSE_HANDLED_CODES,
});

class DriverExpenseService {
  async list({ status, driverId, category, startDate, endDate, page, perPage } = {}) {
    const response = await api.get('/admin/driver-expenses', {
      params: {
        status, driver_id: driverId, category, start_date: startDate, end_date: endDate, page, per_page: perPage,
      },
    });
    return unwrap(response);
  }

  async get(id) {
    return unwrap(await api.get(`/admin/driver-expenses/${id}`));
  }

  async preview(id, approvedAmount) {
    // A read of the effect, not a write: it answers `blocking_codes` instead of refusing, so it toasts like any read.
    return unwrap(await api.post(`/admin/driver-expenses/${id}/preview`, { approved_amount: approvedAmount }));
  }

  async approve(id, approvedAmount) {
    return unwrap(await decide(id, 'approve', { approved_amount: approvedAmount }));
  }

  async reject(id, reason) {
    return unwrap(await decide(id, 'reject', { reason }));
  }

  async undo(id, reason) {
    return unwrap(await decide(id, 'undo', { reason }));
  }

  // An expense id only: the backend resolves the Telegram file from its own row.
  async getReceiptBlob(id) {
    const response = await api.get(`/admin/driver-expenses/${id}/receipt`, { responseType: 'blob' });
    return response.data;
  }
}

export default new DriverExpenseService();
