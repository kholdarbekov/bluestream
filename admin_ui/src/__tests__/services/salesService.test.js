import salesService, { ORDER_APPROVAL_HANDLED_CODES } from '../../services/salesService';
import api from '../../services/api';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

// The house window convention (R12): inclusive LOCAL calendar days, spelled start_date/end_date.
const WINDOW = { start_date: '2026-09-07', end_date: '2026-09-13' };

const envelope = (data) => ({ data: { success: true, data } });

describe('salesService phase-3 read methods', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('reads a page of admin visits and unwraps data.data whole', async () => {
    api.get.mockResolvedValue(envelope({
      visits: [{ id: 7, outlet_name: 'Bahor market', in_radius: false }],
      meta: { page: 2, per_page: 20, total: 41, pages: 3, has_next: true, has_prev: true },
      ...WINDOW,
    }));

    const result = await salesService.getVisits({ ...WINDOW, page: 2, per_page: 20, agent_id: 41, in_radius: false });

    // `in_radius: false` must survive as a boolean: the route reads it through parse_bool_arg,
    // whose whole point is telling "false" apart from "absent".
    expect(api.get).toHaveBeenCalledWith('/admin/sales/visits', {
      params: { start_date: '2026-09-07', end_date: '2026-09-13', page: 2, per_page: 20, agent_id: 41, in_radius: false },
    });
    // `meta` rides INSIDE data for this route (unlike getOutlets, which lifts a sibling `meta`),
    // so the whole unwrapped payload is the return value.
    expect(result).toEqual({
      visits: [{ id: 7, outlet_name: 'Bahor market', in_radius: false }],
      meta: { page: 2, per_page: 20, total: 41, pages: 3, has_next: true, has_prev: true },
      start_date: '2026-09-07',
      end_date: '2026-09-13',
    });
  });

  it('reads the plan-vs-fact rows', async () => {
    api.get.mockResolvedValue(envelope({
      rows: [{ agent_user_id: 41, agent_name: 'Sardor Alimov', day: '2026-09-09', due: 6, completed: 4, counted: 3, unplanned: 1, strike_rate_pct: 75.0, plan_source: 'snapshot', day_status: 'worked' }],
      ...WINDOW,
    }));

    const result = await salesService.getPlanVsFact({ ...WINDOW, agent_id: 41 });

    expect(api.get).toHaveBeenCalledWith('/admin/sales/plan-vs-fact', {
      params: { start_date: '2026-09-07', end_date: '2026-09-13', agent_id: 41 },
    });
    expect(result.rows[0]).toEqual({
      agent_user_id: 41, agent_name: 'Sardor Alimov', day: '2026-09-09', due: 6, completed: 4, counted: 3,
      unplanned: 1, strike_rate_pct: 75.0, plan_source: 'snapshot', day_status: 'worked',
    });
  });

  it('reads the exceptions feed with its type filter', async () => {
    api.get.mockResolvedValue(envelope({
      exceptions: [{ type: 'short_visit', occurred_at: '2026-09-09T05:12:00+00:00', agent_user_id: 41, agent_name: 'Sardor Alimov', outlet_id: 5, outlet_name: 'Bahor market', visit_id: 12, detail: { seconds: 24, threshold_seconds: 60 } }],
      meta: { page: 1, per_page: 20, total: 1, pages: 1, has_next: false, has_prev: false },
      types: ['out_of_range_checkin', 'skipped_checkin', 'short_visit', 'declined_agent_order', 'rejected_agent_order', 'duplicate_photo', 'unvisited', 'duplicate_open_tryout'],
      ...WINDOW,
    }));

    const result = await salesService.getExceptions({ ...WINDOW, type: 'short_visit', page: 1, per_page: 20 });

    expect(api.get).toHaveBeenCalledWith('/admin/sales/exceptions', {
      params: { start_date: '2026-09-07', end_date: '2026-09-13', type: 'short_visit', page: 1, per_page: 20 },
    });
    // `short_visit`'s detail is SECONDS (R8 as corrected in Task 5): the threshold is 60s, so a
    // minutes shape would render every row of this type as "0.3 min".
    expect(result.exceptions[0].detail).toEqual({ seconds: 24, threshold_seconds: 60 });
    expect(result.types).toHaveLength(8);
  });

  it('reads one agent metrics card by users.id', async () => {
    api.get.mockResolvedValue(envelope({
      agent: { id: 41, full_name: 'Sardor Alimov' },
      metrics: { completed_visits: 18, plan_vs_fact_pct: 81.8 },
      ...WINDOW,
    }));

    const result = await salesService.getAgentMetrics(41, WINDOW);

    // The id in the path is a users.id, and it is the FIRST argument — a swapped call would send
    // the params object into the URL (the try-out id-space bug class).
    expect(api.get).toHaveBeenCalledWith('/admin/sales/agents/41/metrics', {
      params: { start_date: '2026-09-07', end_date: '2026-09-13' },
    });
    expect(result.agent).toEqual({ id: 41, full_name: 'Sardor Alimov' });
    expect(result.metrics.plan_vs_fact_pct).toBe(81.8);
  });

  it('reads every active agent in one pass for the performance tab', async () => {
    api.get.mockResolvedValue(envelope({
      agents: [
        { agent_user_id: 41, agent_name: 'Sardor Alimov', phone: '+998901234577', completed_visits: 18 },
        { agent_user_id: 77, agent_name: 'Nodira Karimova', phone: '+998901234579', completed_visits: 23 },
      ],
      ...WINDOW,
    }));

    const result = await salesService.getAgentsMetrics(WINDOW);

    // A DIFFERENT path from the per-agent one: `/agents/metrics`, no id segment.
    expect(api.get).toHaveBeenCalledWith('/admin/sales/agents/metrics', {
      params: { start_date: '2026-09-07', end_date: '2026-09-13' },
    });
    expect(result.agents.map((row) => row.agent_user_id)).toEqual([41, 77]);
  });
});

describe('salesService.approveOutlet', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('posts the attach flag and the contract number as one body', async () => {
    api.post.mockResolvedValue(envelope({ outlet: { id: 5, stage: 'active' } }));

    const result = await salesService.approveOutlet(5, { contract_number: 'DG-2026-77', attach: true });

    // D25: `attach` is what tells the backend to JOIN the account its own phone lookup already
    // found (`account_candidate`) instead of creating one. A missing key reads as "create" on a
    // door whose refusal is a 409 SALES_APPROVAL_PHONE_TAKEN, so it travels explicitly.
    expect(api.post).toHaveBeenCalledWith('/admin/sales/outlets/5/approve', { contract_number: 'DG-2026-77', attach: true });
    expect(result).toEqual({ outlet: { id: 5, stage: 'active' } });
  });

  it('defaults to a plain approve with no contract number', async () => {
    api.post.mockResolvedValue(envelope({ outlet: { id: 6, stage: 'active' } }));

    await salesService.approveOutlet(6);

    // Explicit null, explicit false — `_validated_payload` keeps what the client actually sent
    // (exclude_unset), and both are what phase 1's empty body already meant.
    expect(api.post).toHaveBeenCalledWith('/admin/sales/outlets/6/approve', { contract_number: null, attach: false });
  });
});

describe('salesService outlet editing and photos', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('reads an outlet photo as a blob, by photo id only', async () => {
    const blob = new Blob(['x']);
    api.get.mockResolvedValue({ data: blob });
    await expect(salesService.getVisitPhotoBlob(41)).resolves.toBe(blob);
    expect(api.get).toHaveBeenCalledWith('/admin/sales/visit-photos/41/file', { responseType: 'blob' });
  });

  it('reads the outlet photos page and unwraps data.data', async () => {
    api.get.mockResolvedValue(envelope({ photos: [{ id: 41 }], meta: { total: 1 } }));
    await expect(salesService.getOutletPhotos(5, { page: 2, per_page: 20 })).resolves.toEqual({ photos: [{ id: 41 }], meta: { total: 1 } });
    expect(api.get).toHaveBeenCalledWith('/admin/sales/outlets/5/photos', { params: { page: 2, per_page: 20 } });
  });

  it('writes contacts through the three admin routes', async () => {
    api.post.mockResolvedValue(envelope({ contact: { id: 2 } }));
    api.put.mockResolvedValue(envelope({ contact: { id: 2 } }));
    api.delete.mockResolvedValue(envelope({ contact_id: 2 }));

    await salesService.addOutletContact(5, { name: 'Zafar' });
    await salesService.updateOutletContact(5, 2, { is_primary: true });
    await salesService.deleteOutletContact(5, 2);

    expect(api.post).toHaveBeenCalledWith('/admin/sales/outlets/5/contacts', { name: 'Zafar' });
    expect(api.put).toHaveBeenCalledWith('/admin/sales/outlets/5/contacts/2', { is_primary: true });
    expect(api.delete).toHaveBeenCalledWith('/admin/sales/outlets/5/contacts/2');
  });
});

describe('salesService outlet plan writes (final-review I1)', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  // The Outlets page explains a refused change to the manager's own visit plan itself, so the
  // interceptor must not toast it as well: one refusal, one message.
  const HANDLED = { handledErrorCodes: ['SALES_PAY_SELF_DECISION'] };

  it('names the self-decision refusal on the four plan writes', async () => {
    api.put.mockResolvedValue(envelope({ outlet: { id: 5 } }));
    api.post.mockResolvedValue(envelope({ outlet: { id: 5 } }));

    await salesService.updateOutlet(5, { class: 'A' });
    await salesService.assignOutlet(5, 77);
    await salesService.markLost(5, 'closed', 'Shutters down');
    await salesService.bulkAssign('chilanzar', 77);

    expect(api.put).toHaveBeenCalledWith('/admin/sales/outlets/5', { class: 'A' }, HANDLED);
    expect(api.post.mock.calls).toEqual([
      ['/admin/sales/outlets/5/assign', { agent_user_id: 77 }, HANDLED],
      ['/admin/sales/outlets/5/mark-lost', { reason: 'closed', note: 'Shutters down' }, HANDLED],
      ['/admin/sales/outlets/bulk-assign', { district: 'chilanzar', agent_user_id: 77 }, HANDLED],
    ]);
  });
});

describe('salesService order approvals (C14)', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  // §6.7: the refusals the queue explains inline. A literal here on purpose: the page test and
  // test_admin_ui_payload_fixture_contracts.py read the exported list, this pins its members.
  const HANDLED = {
    handledErrorCodes: [
      'SALES_ORDER_APPROVAL_NOT_FOUND',
      'SALES_ORDER_APPROVAL_NOT_PENDING',
      'SALES_PAY_SELF_DECISION',
      'ADMIN_REASON_REQUIRED',
      'ADMIN_REASON_TOO_LONG',
      'INVENTORY_CONFIRMATION_FAILED',
    ],
  };

  it('exports exactly the six codes the queue explains itself', () => {
    expect(ORDER_APPROVAL_HANDLED_CODES).toEqual(HANDLED.handledErrorCodes);
  });

  it('reads one page of the queue and unwraps data.data whole', async () => {
    const page = {
      items: [{ order_id: 812, status: 'pending', can_decide: true }],
      meta: { page: 2, per_page: 20, total: 21, pages: 2, has_next: false, has_prev: true },
      statuses: ['pending', 'approved', 'rejected', 'cancelled'],
      pending_count: 21,
    };
    api.get.mockResolvedValue(envelope(page));

    const result = await salesService.listOrderApprovals({ status: 'approved', agentId: 41, page: 2, perPage: 20 });

    // A read names no handled codes: a failed read is shown by the page's inline Alert, and
    // nothing about it is a refusal the queue explains.
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(api.get.mock.calls[0]).toEqual([
      '/admin/sales/order-approvals',
      { params: { status: 'approved', agent_id: 41, page: 2, per_page: 20 } },
    ]);
    // `meta`, `statuses` and `pending_count` ride INSIDE data (the visits-route shape, §5.7).
    expect(result).toEqual(page);
  });

  it('approves with an empty body and names the handled codes', async () => {
    api.post.mockResolvedValue(envelope({ approval: { order_id: 812, status: 'approved' } }));

    const result = await salesService.approveAgentOrder(812);

    // `{}` exactly: `_ApproveAgentOrderPayload` has no fields and forbids extras.
    expect(api.post).toHaveBeenCalledWith('/admin/sales/order-approvals/812/approve', {}, HANDLED);
    expect(result).toEqual({ approval: { order_id: 812, status: 'approved' } });
  });

  it('rejects with the reason as typed and names the handled codes', async () => {
    api.post.mockResolvedValue(envelope({ approval: { order_id: 813, status: 'rejected' } }));

    const result = await salesService.rejectAgentOrder(813, '  Duplicate of SA_000809_26 ');

    // Untrimmed: `require_admin_reason` is the one validator of the reason (strip, blank, length).
    expect(api.post).toHaveBeenCalledWith(
      '/admin/sales/order-approvals/813/reject',
      { reason: '  Duplicate of SA_000809_26 ' },
      HANDLED,
    );
    expect(result).toEqual({ approval: { order_id: 813, status: 'rejected' } });
  });
});
