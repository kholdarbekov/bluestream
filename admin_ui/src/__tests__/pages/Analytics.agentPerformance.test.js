import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { message } from 'antd';
import dayjs from 'dayjs';

import Analytics from '../../pages/Analytics';
import adminService from '../../services/adminService';
import salesService from '../../services/salesService';
import exportUtils from '../../utils/exportUtils';

vi.mock('../../services/adminService');
vi.mock('../../services/salesService');
// The spies are bound to the REAL instance's methods, not to a hand-written object that happens
// to carry the same two names. A fabricated factory keeps agreeing with itself after
// `exportToCSV` is renamed in exportUtils.js — the CSV assertions below stay green while the
// export button throws in a browser. `vi.spyOn` refuses to spy on a method that is not there, so
// a rename fails HERE, which is the whole point of the mock.
vi.mock('../../utils/exportUtils', async (importOriginal) => {
  const { default: real } = await importOriginal();
  vi.spyOn(real, 'exportToCSV').mockReturnValue({ success: true, message: 'CSV file exported successfully' });
  vi.spyOn(real, 'exportToExcel').mockReturnValue({ success: true, message: 'Excel file exported successfully' });
  return { __esModule: true, default: real };
});
vi.mock('../../components/charts/LineChart', () => ({ default: function MockLineChart() { return <div data-testid="line-chart" />; } }));
vi.mock('../../components/charts/BarChart', () => ({ default: function MockBarChart() { return <div data-testid="bar-chart" />; } }));
vi.mock('../../components/charts/PieChart', () => ({ default: function MockPieChart() { return <div data-testid="pie-chart" />; } }));
// The namespace ARGUMENT is recorded, not just the lookup: the stub resolves every key to its
// inline `defaultValue`, so forgetting to widen `useTranslation('analytics')` to
// `['analytics', 'sales_agents']` would leave all the new keys English in uz and ru with this whole
// suite still green. Recording it is the only thing in the JS layer that can see the omission.
// `vi.hoisted` is load-bearing: `vi.mock` factories are hoisted above every `const`, so a plain
// outer array here throws "Cannot access before initialization".
const { nsCalls } = vi.hoisted(() => ({ nsCalls: [] }));
vi.mock('react-i18next', () => ({
  useTranslation: (ns) => { nsCalls.push(ns); return { t: (key, opts) => (typeof opts === 'string' ? opts : opts?.defaultValue) || key }; },
}));
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return { ...actual, message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn() } };
});

// Pinned by tests/unit/test_admin_ui_payload_fixture_contracts.py against
// AgentMetricsService.rows_for_period(). Build the fixture null-first, then assign, so an unset
// KPI is explicitly null (what the service publishes) rather than silently undefined.
const AGENT_METRICS_ROW_KEYS = new Set([
  'agent_user_id', 'agent_name', 'phone',
  'planned_visits', 'completed_visits', 'plan_vs_fact_pct', 'unplanned_visits', 'visits_per_day',
  'strike_rate_pct', 'assigned_outlets', 'active_outlets', 'active_share_pct',
  'new_outlets_registered', 'new_outlets_activated', 'orders_placed', 'orders_delivered_paid',
  'bottles_delivered_paid', 'revenue_delivered_paid', 'agent_orders_cancelled',
  'suggested_vs_accepted_pct', 'out_of_range_checkins', 'skipped_checkins', 'avg_visit_minutes',
]);

const agentRow = (values) => Object.assign(
  Object.fromEntries([...AGENT_METRICS_ROW_KEYS].map((k) => [k, null])),
  values,
);

// Distinct, typed values in every field: no assertion below can be satisfied by the wrong cell.
const SARDOR = agentRow({
  agent_user_id: 41, agent_name: 'Sardor Alimov', phone: '+998901112233',
  planned_visits: 18, completed_visits: 14, plan_vs_fact_pct: 72.2, unplanned_visits: 3,
  visits_per_day: 2.3, strike_rate_pct: 57.1, assigned_outlets: 26, active_outlets: 19,
  active_share_pct: 73.1, new_outlets_registered: 4, new_outlets_activated: 2,
  orders_placed: 9, orders_delivered_paid: 6, bottles_delivered_paid: 71,
  revenue_delivered_paid: 1250000.5, agent_orders_cancelled: 1, suggested_vs_accepted_pct: 64.5,
  out_of_range_checkins: 2, skipped_checkins: 1, avg_visit_minutes: 12.4,
});

// A brand-new agent: four ratios have no denominator, so the service publishes null for them.
const NODIRA = agentRow({
  agent_user_id: 77, agent_name: 'Nodira Karimova', phone: '+998901112244',
  planned_visits: 0, completed_visits: 0, plan_vs_fact_pct: null, unplanned_visits: 0,
  visits_per_day: 0, strike_rate_pct: null, assigned_outlets: 5, active_outlets: 0,
  active_share_pct: 0, new_outlets_registered: 0, new_outlets_activated: 0,
  orders_placed: 0, orders_delivered_paid: 0, bottles_delivered_paid: 0,
  revenue_delivered_paid: 0, agent_orders_cancelled: 0, suggested_vs_accepted_pct: null,
  out_of_range_checkins: 0, skipped_checkins: 0, avg_visit_minutes: null,
});

const TODAY = dayjs().format('YYYY-MM-DD');
const THIRTY_DAYS_AGO = dayjs().subtract(30, 'day').format('YYYY-MM-DD');
const SEVEN_DAYS_AGO = dayjs().subtract(7, 'day').format('YYYY-MM-DD');

const CSV_HEADERS = [
  'Agent', 'Phone',
  'Planned visits', 'Completed visits', 'Plan vs fact %', 'Unplanned visits', 'Visits / day',
  'Strike rate %', 'Assigned outlets', 'Active outlets', 'Active share %',
  'New outlets registered', 'New outlets activated', 'Orders placed', 'Orders delivered & paid',
  'Bottles delivered & paid', 'Revenue delivered & paid', 'Orders cancelled',
  'Suggested vs accepted %', 'Out-of-range check-ins', 'Skipped check-ins', 'Avg visit minutes',
];

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => (
    <MemoryRouter initialEntries={['/analytics']}><QueryClientProvider client={queryClient}>{children}</QueryClientProvider></MemoryRouter>
  );
};

const openAgentTab = async () => {
  fireEvent.click(await screen.findByRole('tab', { name: 'Agent Performance' }));
  return screen.findByRole('row', { name: /Sardor Alimov/ });
};

beforeEach(() => {
  vi.clearAllMocks();
  adminService.getAnalytics.mockResolvedValue({
    total_revenue: 0, total_orders: 0, active_customers: 0, growth_rate: 0,
    revenue_trend: [], order_trend: [],
  });
  salesService.getAgentsMetrics.mockResolvedValue({
    agents: [SARDOR, NODIRA], start_date: THIRTY_DAYS_AGO, end_date: TODAY,
  });
});

it('leaves the KPI query alone until the Agent Performance tab is opened', async () => {
  render(<Analytics />, { wrapper: createWrapper() });
  await screen.findByTestId('line-chart');
  expect(salesService.getAgentsMetrics).not.toHaveBeenCalled();

  await openAgentTab();

  // ALL agents in ONE call — never one request per row.
  expect(salesService.getAgentsMetrics).toHaveBeenCalledTimes(1);
  expect(salesService.getAgentsMetrics).toHaveBeenCalledWith({ start_date: THIRTY_DAYS_AGO, end_date: TODAY });
});

it('renders every KPI for both agents, with an em dash where the service published null', async () => {
  render(<Analytics />, { wrapper: createWrapper() });
  const sardor = await openAgentTab();

  expect(sardor).toHaveTextContent('+998901112233');
  expect(sardor).toHaveTextContent('18');
  expect(sardor).toHaveTextContent('72.2%');
  expect(sardor).toHaveTextContent('2.3');
  expect(sardor).toHaveTextContent('71');
  // formatMoneyUZS is the admin UI's whole-UZS convention, so 1250000.5 reads as 1,250,001 here;
  // the CSV below carries the raw 1250000.5.
  expect(sardor).toHaveTextContent('1,250,001 UZS');
  expect(sardor).toHaveTextContent('12.4');

  const nodira = await screen.findByRole('row', { name: /Nodira Karimova/ });
  // plan_vs_fact_pct, strike_rate_pct, suggested_vs_accepted_pct, avg_visit_minutes are null:
  // exactly four em dashes, and never a fabricated 0%.
  expect(nodira.textContent.match(/—/g)).toHaveLength(4);
  expect(nodira).toHaveTextContent('0%');

  // The four column groups and a label from each of the outer two.
  expect(screen.getByText('Visits')).toBeInTheDocument();
  expect(screen.getByText('Outlets')).toBeInTheDocument();
  expect(screen.getByText('Orders')).toBeInTheDocument();
  expect(screen.getByText('Discipline')).toBeInTheDocument();
  // By ROLE, not by text: `scroll={{ x }}` makes antd render a hidden `ant-table-measure-cell-content`
  // copy of every LEAF column title (the group titles are not measured), so `getByText` finds two.
  expect(screen.getByRole('columnheader', { name: 'Planned visits' })).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'Avg visit minutes' })).toBeInTheDocument();

  // The window the SERVER answered for, echoed back so the owner reads the real period.
  expect(screen.getByText(`${THIRTY_DAYS_AGO} — ${TODAY}`)).toBeInTheDocument();

  // Every label above is a `sales_agents:`-prefixed key; the page must have DECLARED that
  // namespace or all of them stay English in uz and ru while this file stays green.
  expect(nsCalls).toContainEqual(['analytics', 'sales_agents']);
});

it('exports the visible rows as CSV with translated headers and raw values', async () => {
  render(<Analytics />, { wrapper: createWrapper() });
  await openAgentTab();

  fireEvent.click(screen.getByRole('button', { name: /export_report/i }));

  // The page-level button is xlsx everywhere else; on this tab it is the spec's CSV.
  expect(exportUtils.exportToExcel).not.toHaveBeenCalled();
  expect(exportUtils.exportToCSV).toHaveBeenCalledTimes(1);

  const [rows, filename] = exportUtils.exportToCSV.mock.calls[0];
  expect(filename).toBe(`agent_performance_${THIRTY_DAYS_AGO}_${TODAY}`);
  // Header order is the backend's METRIC_KEYS order, agent identity first.
  expect(Object.keys(rows[0])).toEqual(CSV_HEADERS);
  expect(rows).toEqual([
    {
      'Agent': 'Sardor Alimov', 'Phone': '+998901112233',
      'Planned visits': 18, 'Completed visits': 14, 'Plan vs fact %': 72.2,
      'Unplanned visits': 3, 'Visits / day': 2.3, 'Strike rate %': 57.1,
      'Assigned outlets': 26, 'Active outlets': 19, 'Active share %': 73.1,
      'New outlets registered': 4, 'New outlets activated': 2, 'Orders placed': 9,
      'Orders delivered & paid': 6, 'Bottles delivered & paid': 71,
      'Revenue delivered & paid': 1250000.5, 'Orders cancelled': 1,
      'Suggested vs accepted %': 64.5, 'Out-of-range check-ins': 2,
      'Skipped check-ins': 1, 'Avg visit minutes': 12.4,
    },
    {
      'Agent': 'Nodira Karimova', 'Phone': '+998901112244',
      'Planned visits': 0, 'Completed visits': 0, 'Plan vs fact %': null,
      'Unplanned visits': 0, 'Visits / day': 0, 'Strike rate %': null,
      'Assigned outlets': 5, 'Active outlets': 0, 'Active share %': 0,
      'New outlets registered': 0, 'New outlets activated': 0, 'Orders placed': 0,
      'Orders delivered & paid': 0, 'Bottles delivered & paid': 0,
      'Revenue delivered & paid': 0, 'Orders cancelled': 0,
      'Suggested vs accepted %': null, 'Out-of-range check-ins': 0,
      'Skipped check-ins': 0, 'Avg visit minutes': null,
    },
  ]);
});

it('refetches the KPI rows when the period changes', async () => {
  render(<Analytics />, { wrapper: createWrapper() });
  await openAgentTab();
  expect(salesService.getAgentsMetrics).toHaveBeenCalledTimes(1);

  fireEvent.mouseDown(screen.getByTestId('analytics-timeframe').querySelector('.ant-select-selector'));
  fireEvent.click(await screen.findByTitle('ui.analytics.last_7_days'));

  await waitFor(() => expect(salesService.getAgentsMetrics).toHaveBeenLastCalledWith({ start_date: SEVEN_DAYS_AGO, end_date: TODAY }));
  expect(salesService.getAgentsMetrics).toHaveBeenCalledTimes(2);
});

it("shows the backend's refusal instead of an empty table when the range is refused", async () => {
  // R12 caps a range at SALES_METRICS_MAX_RANGE_DAYS server-side, and this tab shares the page's own
  // period controls — whose "Last year" option is a 365-day span, and whose RangePicker can produce
  // any span at all. The tab deliberately does NOT re-check the cap (one expression of the rule,
  // server-side), so an empty table under a header that still names the refused window would read as
  // "no agent did anything all year" — a claim about the field rather than about the request.
  salesService.getAgentsMetrics.mockRejectedValue({ response: { data: { message: 'Date range may not exceed 92 days' } } });
  render(<Analytics />, { wrapper: createWrapper() });
  fireEvent.click(await screen.findByRole('tab', { name: 'Agent Performance' }));

  expect(await screen.findByText('Date range may not exceed 92 days')).toBeInTheDocument();
});

it('refuses to export a blank CSV when the period answered no rows', async () => {
  // `exportToCSV([])` saves a 0-byte file and still returns `{ success: true }`, so the `!success`
  // guard can never fire. This is the button an owner presses right after a refused range.
  salesService.getAgentsMetrics.mockResolvedValue({ agents: [], start_date: THIRTY_DAYS_AGO, end_date: TODAY });
  render(<Analytics />, { wrapper: createWrapper() });
  fireEvent.click(await screen.findByRole('tab', { name: 'Agent Performance' }));
  // antd's own empty state: proof the query RESOLVED with zero rows, not that it is still loading.
  // findAll, because `scroll={{ x }}` gives the placeholder the same duplicated-node treatment as
  // the leaf column titles.
  await screen.findAllByText('No data');

  fireEvent.click(screen.getByRole('button', { name: /export_report/i }));

  expect(exportUtils.exportToCSV).not.toHaveBeenCalled();
  expect(message.warning).toHaveBeenCalledWith('No data to export');
});
