import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { post: vi.fn(), get: vi.fn() },
  getCookie: vi.fn(),
}));

import api from '../../services/api';
import adminService from '../../services/adminService';

describe('adminService collected-cash methods', () => {
  beforeEach(() => vi.clearAllMocks());

  it('previewCollectedCashEdit POSTs to the preview route and returns data', async () => {
    api.post.mockResolvedValue({ data: { data: { applied_to_order: 54000 } } });
    const out = await adminService.previewCollectedCashEdit(42, { new_amount: 60000 });
    expect(api.post).toHaveBeenCalledWith('/admin/orders/42/collected-cash/preview', { new_amount: 60000 });
    expect(out).toEqual({ data: { applied_to_order: 54000 } });
  });

  it('editCollectedCash POSTs to the apply route and returns data', async () => {
    api.post.mockResolvedValue({ data: { data: { order_id: 42 } } });
    const out = await adminService.editCollectedCash(42, { new_amount: 60000, reason: 'driver collected 60k' });
    expect(api.post).toHaveBeenCalledWith('/admin/orders/42/collected-cash', { new_amount: 60000, reason: 'driver collected 60k' });
    expect(out).toEqual({ data: { order_id: 42 } });
  });
});
