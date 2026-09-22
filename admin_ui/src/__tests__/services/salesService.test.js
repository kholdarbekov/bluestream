import salesService from '../../services/salesService';
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
      rows: [{ agent_user_id: 41, agent_name: 'Sardor Alimov', day: '2026-09-09', due: 6, completed: 4, unplanned: 1, strike_rate_pct: 75.0, plan_source: 'snapshot' }],
      ...WINDOW,
    }));

    const result = await salesService.getPlanVsFact({ ...WINDOW, agent_id: 41 });

    expect(api.get).toHaveBeenCalledWith('/admin/sales/plan-vs-fact', {
      params: { start_date: '2026-09-07', end_date: '2026-09-13', agent_id: 41 },
    });
    expect(result.rows[0]).toEqual({
      agent_user_id: 41, agent_name: 'Sardor Alimov', day: '2026-09-09', due: 6, completed: 4,
      unplanned: 1, strike_rate_pct: 75.0, plan_source: 'snapshot',
    });
  });

  it('reads the exceptions feed with its type filter', async () => {
    api.get.mockResolvedValue(envelope({
      exceptions: [{ type: 'short_visit', occurred_at: '2026-09-09T05:12:00+00:00', agent_user_id: 41, agent_name: 'Sardor Alimov', outlet_id: 5, outlet_name: 'Bahor market', visit_id: 12, detail: { seconds: 24, threshold_seconds: 60 } }],
      meta: { page: 1, per_page: 20, total: 1, pages: 1, has_next: false, has_prev: false },
      types: ['out_of_range_checkin', 'skipped_checkin', 'short_visit', 'declined_agent_order', 'duplicate_photo', 'unvisited', 'duplicate_open_tryout'],
      ...WINDOW,
    }));

    const result = await salesService.getExceptions({ ...WINDOW, type: 'short_visit', page: 1, per_page: 20 });

    expect(api.get).toHaveBeenCalledWith('/admin/sales/exceptions', {
      params: { start_date: '2026-09-07', end_date: '2026-09-13', type: 'short_visit', page: 1, per_page: 20 },
    });
    // `short_visit`'s detail is SECONDS (R8 as corrected in Task 5): the threshold is 60s, so a
    // minutes shape would render every row of this type as "0.3 min".
    expect(result.exceptions[0].detail).toEqual({ seconds: 24, threshold_seconds: 60 });
    expect(result.types).toHaveLength(7);
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
