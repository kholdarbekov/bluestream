import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import dayjs from 'dayjs';

import Visits from '../../pages/Visits';
import salesService from '../../services/salesService';
import staffService from '../../services/staffService';

vi.mock('../../services/salesService');
vi.mock('../../services/staffService');
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (typeof opts === 'string' ? opts : opts?.defaultValue) || key }),
}));
// The page's contract with the Leaflet band is the `checkins` prop it hands over; the marker
// colours are pinned in src/components/OperationsMap.test.jsx. Stubbing the component (not
// react-leaflet) is the Dispatch.test.jsx:12-28 pattern.
vi.mock('../../components/OperationsMap', () => ({
  default: ({ checkins, visibleLayers }) => (
    <div data-testid="ops-map" data-layers={JSON.stringify(visibleLayers)}>
      <div data-testid="ops-map-checkins">{JSON.stringify(checkins)}</div>
    </div>
  ),
}));

// Every field `serialize_visit` publishes (business_app/serializers/sales_serializers.py:409-444)
// plus the three `serialize_visit_admin_row` adds. Null-first, then the values a case needs, so a
// field the page reads but the serializer never publishes is `null` here, not silently undefined.
const VISIT_ROW_KEYS = new Set([
  'id', 'outlet_id', 'agent_user_id', 'status', 'planned', 'current_step', 'started_at', 'checkin_at',
  'checkin_latitude', 'checkin_longitude', 'checkin_accuracy_m', 'distance_m', 'in_radius', 'checkin_skipped',
  'ended_at', 'outcome', 'no_order_reason', 'dm_present', 'notes', 'next_visit_at', 'order', 'stock_checks',
  'previous_stock', 'outlet_name', 'agent_name', 'order_number',
]);
const visitRow = (overrides) => Object.assign(
  Object.fromEntries([...VISIT_ROW_KEYS].map((k) => [k, null])),
  { status: 'completed', planned: false, checkin_skipped: false, stock_checks: [], previous_stock: [] },
  overrides,
);

const IN_RADIUS_VISIT = visitRow({
  id: 501, outlet_id: 5, agent_user_id: 41, planned: true,
  started_at: '2026-09-14T04:05:00+00:00', checkin_at: '2026-09-14T04:07:00+00:00',
  checkin_latitude: 41.3111, checkin_longitude: 69.2797, distance_m: 12.4, in_radius: true,
  outcome: 'order_placed', outlet_name: 'Bahor market', agent_name: 'Sardor Alimov', order_number: 'SA-000101',
});
const OUT_OF_RANGE_VISIT = visitRow({
  id: 502, outlet_id: 6, agent_user_id: 77, planned: false,
  started_at: '2026-09-14T06:15:00+00:00', checkin_at: '2026-09-14T06:16:00+00:00',
  checkin_latitude: 41.2555, checkin_longitude: 69.1888, distance_m: 640, in_radius: false,
  outcome: 'no_order', outlet_name: 'Yunus shop', agent_name: 'Nodira Karimova',
});
// A skipped check-in has no coordinates at all: nothing for the band to draw, and `in_radius`
// stays NULL — the row still belongs in the table.
const SKIPPED_VISIT = visitRow({
  id: 503, outlet_id: 7, agent_user_id: 41, started_at: '2026-09-14T08:40:00+00:00',
  checkin_skipped: true, outcome: 'owner_absent', outlet_name: 'Chorsu kiosk', agent_name: 'Sardor Alimov',
});

const PLAN_ROWS = [
  { agent_user_id: 41, agent_name: 'Sardor Alimov', day: '2026-09-14', due: 6, completed: 4, unplanned: 1, strike_rate_pct: 74.5, plan_source: 'snapshot' },
  { agent_user_id: 77, agent_name: 'Nodira Karimova', day: '2026-09-13', due: null, completed: 2, unplanned: 2, strike_rate_pct: null, plan_source: 'none' },
];

// The feed's vocabulary as the route publishes it (R8) — the page must read THIS, not a copy.
const EXCEPTION_TYPES = [
  'out_of_range_checkin', 'skipped_checkin', 'short_visit', 'declined_agent_order',
  'duplicate_photo', 'unvisited', 'duplicate_open_tryout',
];
const EXCEPTIONS = [
  { type: 'out_of_range_checkin', occurred_at: '2026-09-14T06:16:00+00:00', agent_user_id: 77, agent_name: 'Nodira Karimova', outlet_id: 6, outlet_name: 'Yunus shop', visit_id: 502, detail: { distance_m: 640, radius_m: 250 } },
  { type: 'duplicate_open_tryout', occurred_at: '2026-09-12T05:00:00+00:00', agent_user_id: 41, agent_name: 'Sardor Alimov', outlet_id: 9, outlet_name: 'Chorsu dokon', visit_id: null, detail: { tryout_ids: [91, 88] } },
];

// An eighth type nobody has written into JS anywhere. The route is free to grow its vocabulary
// without a UI release (R8), so the picker has to be built from the response's `types` — a JS
// mirror would offer seven and could never send this one.
const FUTURE_TYPE = 'fabricated_future_type';

// Dispatch.test.jsx:77's convention for a date the page computes from "today".
const day = (offset) => dayjs().subtract(offset, 'day').format('YYYY-MM-DD');

const createWrapper = (initialEntry = '/sales/visits') => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => (
    <MemoryRouter initialEntries={[initialEntry]}><QueryClientProvider client={queryClient}>{children}</QueryClientProvider></MemoryRouter>
  );
};

const pickOption = async (testId, title) => {
  fireEvent.mouseDown(screen.getByTestId(testId).querySelector('.ant-select-selector'));
  fireEvent.click(await screen.findByTitle(title));
};

beforeEach(() => {
  vi.clearAllMocks();
  salesService.getVisits.mockResolvedValue({
    visits: [IN_RADIUS_VISIT, OUT_OF_RANGE_VISIT, SKIPPED_VISIT],
    meta: { page: 1, per_page: 20, total: 3, pages: 1, has_next: false, has_prev: false },
    start_date: day(6), end_date: day(0),
  });
  salesService.getPlanVsFact.mockResolvedValue({ rows: PLAN_ROWS, start_date: day(6), end_date: day(0) });
  salesService.getExceptions.mockResolvedValue({
    exceptions: EXCEPTIONS, meta: { page: 1, per_page: 20, total: 2, pages: 1, has_next: false, has_prev: false },
    types: EXCEPTION_TYPES, start_date: day(6), end_date: day(0),
  });
  // Two agents with distinct ids so an `agent_id` assertion cannot be satisfied by echoing the
  // one the first row already carries.
  // `meta` carries no `has_next`, which is what STOPS `fetchAllPages` after page 1 (M31):
  // `hasNext = meta ? meta.has_next : …` is undefined → falsy → break. Deliberate — adding
  // `has_next: false` is equivalent, but adding `has_next: true` would loop the mock forever.
  staffService.getSalesAgents.mockResolvedValue({ data: { data: { items: [{ user_id: 41, full_name: 'Sardor Alimov' }, { user_id: 77, full_name: 'Nodira Karimova' }] }, meta: { total: 2 } } });
});

it('lists the period\'s visits with the check-in verdict the backend published', async () => {
  render(<Visits />, { wrapper: createWrapper() });
  const table = await screen.findByTestId('visits-table');

  const planned = await within(table).findByRole('row', { name: /Bahor market/ });
  expect(planned).toHaveTextContent('order_placed');
  expect(planned).toHaveTextContent('Planned');
  expect(planned).toHaveTextContent('In radius (12 m)');
  expect(planned).toHaveTextContent('SA-000101');

  // FALSE is a violation, NULL is "not measurable" — three visits, three different verdicts.
  expect(within(table).getByRole('row', { name: /Yunus shop/ })).toHaveTextContent('Out of range (640 m)');
  expect(within(table).getByRole('row', { name: /Yunus shop/ })).toHaveTextContent('Unplanned');
  expect(within(table).getByRole('row', { name: /Chorsu kiosk/ })).toHaveTextContent('Skipped');

  // R12's default window: the last 7 LOCAL days including today, and `in_radius` absent rather
  // than `false` — the filter's third state is "either".
  await waitFor(() => expect(salesService.getVisits).toHaveBeenCalledWith({
    start_date: day(6), end_date: day(0), agent_id: undefined, outcome: undefined,
    in_radius: undefined, page: 1, per_page: 20,
  }));
  expect(salesService.getPlanVsFact).toHaveBeenCalledWith({ start_date: day(6), end_date: day(0), agent_id: undefined });
});

it('shows a day with no plan snapshot as "No plan", never as a zero due count', async () => {
  render(<Visits />, { wrapper: createWrapper() });
  const planCard = await screen.findByTestId('plan-vs-fact');
  // The Card mounts before its query resolves, so reading the rows straight off the testid
  // reads antd's "No data" placeholder row. Wait for a real day cell first.
  await within(planCard).findByText('2026-09-14');
  const rows = within(planCard).getAllByRole('row');

  // `due: null` + `plan_source: "none"` means the nightly 01:20 snapshot has no row for that
  // day (R1) — the backend ships `plan_source` precisely so this table never has to guess, and
  // rendering a 0 there would read as "nothing was due", which is a different claim.
  expect(within(rows[1]).getAllByRole('cell').map((c) => c.textContent)).toEqual(['2026-09-14', 'Sardor Alimov', '6', '4', '1', '74.5%']);
  expect(within(rows[2]).getAllByRole('cell').map((c) => c.textContent)).toEqual(['2026-09-13', 'Nodira Karimova', 'No plan', '2', '2', '—']);
});

it('narrows to one agent and to out-of-range check-ins, returning to page 1', async () => {
  salesService.getVisits.mockResolvedValue({
    visits: [OUT_OF_RANGE_VISIT], meta: { page: 1, per_page: 20, total: 45, pages: 3, has_next: true, has_prev: false },
    start_date: day(6), end_date: day(0),
  });
  render(<Visits />, { wrapper: createWrapper() });
  await screen.findByText('Yunus shop');

  fireEvent.click(screen.getByTitle('2'));
  await waitFor(() => expect(salesService.getVisits).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 })));

  // A narrower filter usually has fewer pages than the one being viewed (Outlets.js:51).
  await pickOption('filter-agent', 'Nodira Karimova');
  await waitFor(() => expect(salesService.getVisits).toHaveBeenLastCalledWith(expect.objectContaining({ page: 1, agent_id: 77 })));

  // The boolean FALSE has to survive to the query string: `in_radius: false` is a filter, and a
  // `value || undefined` anywhere on the way would erase it into "either".
  await pickOption('filter-in-radius', 'Out of range');
  await waitFor(() => expect(salesService.getVisits).toHaveBeenLastCalledWith({
    start_date: day(6), end_date: day(0), agent_id: 77, outcome: undefined,
    in_radius: false, page: 1, per_page: 20,
  }));
});

it('hands the map band exactly the check-ins on the current page', async () => {
  render(<Visits />, { wrapper: createWrapper() });
  await screen.findByText('Bahor market');

  // Two of the three rows carry coordinates; the skipped check-in has nothing to draw. The band
  // is the CURRENT PAGE only (R11) — an unpaginated variant of this query does not exist.
  expect(JSON.parse(screen.getByTestId('ops-map-checkins').textContent)).toEqual([
    { visit_id: 501, lat: 41.3111, lng: 69.2797, in_radius: true, distance_m: 12.4, outlet_name: 'Bahor market', agent_name: 'Sardor Alimov' },
    { visit_id: 502, lat: 41.2555, lng: 69.1888, in_radius: false, distance_m: 640, outlet_name: 'Yunus shop', agent_name: 'Nodira Karimova' },
  ]);
  expect(JSON.parse(screen.getByTestId('ops-map').dataset.layers)).toEqual({ customers: false, orders: false, drivers: false, checkins: true });
});

it('switches to the exceptions feed and filters it by a type the route published', async () => {
  salesService.getExceptions.mockResolvedValue({
    exceptions: EXCEPTIONS, meta: { page: 1, per_page: 20, total: 2, pages: 1, has_next: false, has_prev: false },
    types: [...EXCEPTION_TYPES, FUTURE_TYPE], start_date: day(6), end_date: day(0),
  });
  render(<Visits />, { wrapper: createWrapper() });
  await screen.findByText('Bahor market');

  fireEvent.click(screen.getByRole('tab', { name: 'Exceptions' }));
  const table = await screen.findByTestId('exceptions-table');
  const row = await within(table).findByRole('row', { name: /Yunus shop/ });
  expect(row).toHaveTextContent('out_of_range_checkin');
  // `detail` is rendered generically — the page owns no per-type branch, so the day the feed
  // gains an eighth type it renders instead of going blank.
  expect(row).toHaveTextContent('distance_m: 640 · radius_m: 250');
  expect(within(table).getByRole('row', { name: /duplicate_open_tryout/ })).toHaveTextContent('tryout_ids: 91,88');

  await waitFor(() => expect(salesService.getExceptions).toHaveBeenCalledWith({
    start_date: day(6), end_date: day(0), agent_id: undefined, type: undefined, page: 1, per_page: 20,
  }));

  // The picker is built from the response's `types`, not from a JS copy of EXCEPTION_TYPES — so
  // it offers the EIGHTH type this page has never heard of, and can send it.
  fireEvent.mouseDown(screen.getByTestId('filter-exception-type').querySelector('.ant-select-selector'));
  expect(document.querySelectorAll('.ant-select-item-option')).toHaveLength(8);
  fireEvent.click(await screen.findByTitle(FUTURE_TYPE));
  await waitFor(() => expect(salesService.getExceptions).toHaveBeenLastCalledWith(expect.objectContaining({ type: FUTURE_TYPE, page: 1 })));
});

it('opens on the exceptions feed when the managers\' daily summary deep-links to it', async () => {
  // The 08:00 IN_APP push (R9) carries `/sales/visits?tab=exceptions`. Landing on the visits
  // list and flipping a tab a frame later would fetch a list nobody asked for.
  render(<Visits />, { wrapper: createWrapper('/sales/visits?tab=exceptions') });

  expect(await within(await screen.findByTestId('exceptions-table')).findByRole('row', { name: /Yunus shop/ })).toBeInTheDocument();
  await waitFor(() => expect(salesService.getExceptions).toHaveBeenCalledTimes(1));
  expect(salesService.getVisits).not.toHaveBeenCalled();
  expect(salesService.getPlanVsFact).not.toHaveBeenCalled();
});

it('shows the backend\'s refusal instead of an empty table when the range is refused', async () => {
  // R12 caps a range at SALES_METRICS_MAX_RANGE_DAYS and the picker deliberately does NOT
  // re-check it here (one expression of the rule, server-side). An empty table would read as
  // "no visits in six months"; the 400's message is the only honest thing to show.
  salesService.getVisits.mockRejectedValue({ response: { data: { message: 'Date range may not exceed 92 days' } } });
  render(<Visits />, { wrapper: createWrapper() });

  expect(await screen.findByText('Date range may not exceed 92 days')).toBeInTheDocument();
});

it('surfaces a plan-vs-fact refusal instead of an empty plan table', async () => {
  // The two tables on this tab are two independent queries. When only plan-vs-fact fails, an
  // empty plan table beside a full visits table reads as "nobody had a plan that week" — a claim
  // about the field, not about the request that failed.
  salesService.getPlanVsFact.mockRejectedValue({ response: { data: { message: 'Date range may not exceed 92 days' } } });
  render(<Visits />, { wrapper: createWrapper() });

  // The visits half still answered, so nothing else on the tab explains the empty plan table.
  await screen.findByText('Bahor market');
  expect(await screen.findByText('Date range may not exceed 92 days')).toBeInTheDocument();
});

it('offers the visit-only filters on the visits tab alone', async () => {
  render(<Visits />, { wrapper: createWrapper() });
  await screen.findByText('Bahor market');
  expect(screen.getByTestId('filter-outcome')).toBeInTheDocument();
  expect(screen.getByTestId('filter-in-radius')).toBeInTheDocument();

  fireEvent.click(screen.getByRole('tab', { name: 'Exceptions' }));
  await screen.findByTestId('exceptions-table');

  // Neither narrows the exceptions feed — `GET /admin/sales/exceptions` takes no `outcome` and no
  // `in_radius`. Left on screen they are controls that do nothing except silently reset the feed
  // to page 1 through `narrow`.
  expect(screen.queryByTestId('filter-outcome')).toBeNull();
  expect(screen.queryByTestId('filter-in-radius')).toBeNull();
  // The period and the agent DO narrow both feeds, so they stay on both tabs.
  expect(screen.getByTestId('filter-agent')).toBeInTheDocument();
});

it('renders every duplicate-photo row on one visit, not just the first', async () => {
  // `duplicate_photo` is one row PER PHOTO (`duplicate_of_photo_id IS NOT NULL`), so two rows
  // legitimately share type, outlet, visit AND `occurred_at` — `received_at` has no per-row
  // uniqueness. A key built from those four alone is the same string twice, which React
  // reconciles as one row and warns about.
  const photoRow = (photoId) => ({
    type: 'duplicate_photo', occurred_at: '2026-09-14T07:00:00+00:00', agent_user_id: 41,
    agent_name: 'Sardor Alimov', outlet_id: 5, outlet_name: 'Bahor market', visit_id: 501,
    detail: { photo_id: photoId },
  });
  salesService.getExceptions.mockResolvedValue({
    exceptions: [photoRow(11), photoRow(12)],
    meta: { page: 1, per_page: 20, total: 2, pages: 1, has_next: false, has_prev: false },
    types: EXCEPTION_TYPES, start_date: day(6), end_date: day(0),
  });
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
  render(<Visits />, { wrapper: createWrapper('/sales/visits?tab=exceptions') });

  const table = await screen.findByTestId('exceptions-table');
  expect(await within(table).findByText('photo_id: 11')).toBeInTheDocument();
  expect(within(table).getByText('photo_id: 12')).toBeInTheDocument();
  // The duplicate-key warning is the mechanism: React still paints both rows on a first render,
  // so only the warning distinguishes a correct key from a colliding one.
  expect(consoleError.mock.calls.map((c) => c.join(' ')).join('\n')).not.toMatch(/same key/);
  consoleError.mockRestore();
});
