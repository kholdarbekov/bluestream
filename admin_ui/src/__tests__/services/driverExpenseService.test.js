import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { post: vi.fn(), get: vi.fn() },
  getCookie: vi.fn(),
}));

import api from '../../services/api';
import driverExpenseService, { DRIVER_EXPENSE_HANDLED_CODES } from '../../services/driverExpenseService';

// The Driver expenses page's HTTP contract. The query param names are the ones `DriverExpenseListQuery` reads
// (business_app/serializers/driver_expense_serializers.py), exercised by tests/integration/test_driver_expense_admin_read_api.py;
// the body keys are the fields the decision routes read (`_field("approved_amount")`, `_field("reason")` in
// business_app/api/admin_driver_expenses.py). Both page tests mock this module whole, so a renamed param or body key is
// caught here or nowhere.
const HANDLED = { handledErrorCodes: ['DRIVER_EXPENSE_DECISION_BLOCKED', 'DRIVER_EXPENSE_SELF_DECISION'] };

describe('driverExpenseService', () => {
  beforeEach(() => vi.clearAllMocks());

  it('names exactly the two refusals the page explains itself', () => {
    expect(DRIVER_EXPENSE_HANDLED_CODES).toEqual(HANDLED.handledErrorCodes);
  });

  it('list sends every filter as the snake_case query the backend reads and returns the envelope data', async () => {
    const body = { items: [{ id: 7 }], page: 1, per_page: 20, total: 1, pending_count: 1 };
    api.get.mockResolvedValue({ data: { success: true, data: body } });

    const out = await driverExpenseService.list({
      status: 'all', driverId: 31, category: 'fuel', startDate: '2026-11-05', endDate: '2026-11-20', page: 2, perPage: 20,
    });

    expect(api.get).toHaveBeenCalledWith('/admin/driver-expenses', {
      params: {
        status: 'all', driver_id: 31, category: 'fuel', start_date: '2026-11-05', end_date: '2026-11-20', page: 2, per_page: 20,
      },
    });
    expect(out).toBe(body);
  });

  it('list leaves an unpicked filter undefined, so axios drops it from the query string', async () => {
    api.get.mockResolvedValue({ data: { data: { items: [] } } });

    await driverExpenseService.list({ status: 'pending', page: 1, perPage: 20 });

    expect(api.get).toHaveBeenCalledWith('/admin/driver-expenses', {
      params: {
        status: 'pending', driver_id: undefined, category: undefined, start_date: undefined, end_date: undefined,
        page: 1, per_page: 20,
      },
    });
  });

  it('get reads one claim with no request config', async () => {
    const body = { expense: { id: 42 }, warnings: {}, reason_min_length: 5 };
    api.get.mockResolvedValue({ data: { data: body } });

    const out = await driverExpenseService.get('42');

    expect(api.get).toHaveBeenCalledWith('/admin/driver-expenses/42');
    expect(api.get.mock.calls[0]).toHaveLength(1);
    expect(out).toBe(body);
  });

  it('preview posts the typed amount as approved_amount and toasts like any read', async () => {
    const body = { effect: { session_id: 124 }, blocking_codes: [], approve_max_amount: 150000 };
    api.post.mockResolvedValue({ data: { data: body } });

    const out = await driverExpenseService.preview(7, 120000);

    expect(api.post).toHaveBeenCalledWith('/admin/driver-expenses/7/preview', { approved_amount: 120000 });
    expect(api.post.mock.calls[0]).toHaveLength(2);
    expect(out).toBe(body);
  });

  it('approve posts approved_amount and names the refusals the dialog explains', async () => {
    const body = { expense: { id: 7, status: 'approved' } };
    api.post.mockResolvedValue({ data: { data: body } });

    const out = await driverExpenseService.approve(7, 120000);

    expect(api.post).toHaveBeenCalledWith('/admin/driver-expenses/7/approve', { approved_amount: 120000 }, HANDLED);
    expect(out).toBe(body);
  });

  it('reject posts the reason and names the refusals the dialog explains', async () => {
    const body = { expense: { id: 7, status: 'rejected' } };
    api.post.mockResolvedValue({ data: { data: body } });

    const out = await driverExpenseService.reject(7, 'Not a business expense');

    expect(api.post).toHaveBeenCalledWith('/admin/driver-expenses/7/reject', { reason: 'Not a business expense' }, HANDLED);
    expect(out).toBe(body);
  });

  it('undo posts the reason and names the refusals the dialog explains', async () => {
    const body = { expense: { id: 7, status: 'pending' } };
    api.post.mockResolvedValue({ data: { data: body } });

    const out = await driverExpenseService.undo(7, 'Approved by mistake');

    expect(api.post).toHaveBeenCalledWith('/admin/driver-expenses/7/undo', { reason: 'Approved by mistake' }, HANDLED);
    expect(out).toBe(body);
  });

  it('getReceiptBlob asks for a blob by expense id and returns the blob itself', async () => {
    const blob = new Blob(['jpeg'], { type: 'image/jpeg' });
    api.get.mockResolvedValue({ data: blob });

    const out = await driverExpenseService.getReceiptBlob(7);

    expect(api.get).toHaveBeenCalledWith('/admin/driver-expenses/7/receipt', { responseType: 'blob' });
    expect(out).toBe(blob);
  });
});
