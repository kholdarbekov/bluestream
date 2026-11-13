/**
 * The "Delivery time accuracy" page: the owner's read of the Phase 3 go-live gate. The payload is the one
 * tests/fixtures/admin_eta_accuracy/report.json holds (the backend's own `EtaAccuracyReport.build()` output, held to
 * the endpoint by tests/contract/test_eta_accuracy_payload_matches_admin_fixture.py). Only the axios instance is
 * mocked, so every assertion reads the URL and params the backend gets and the figures the admin sees.
 */
import fs from 'node:fs';
import path from 'node:path';
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import EtaAccuracy from '../../pages/EtaAccuracy';
import api from '../../services/api';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  getCookie: vi.fn(),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    i18n: { language: 'en' },
    // Reads back the inline defaultValue with i18next's `{{token}}` interpolation.
    t: (key, options) => {
      const text = typeof options?.defaultValue === 'string' ? options.defaultValue : key;
      return text.replace(/\{\{(\w+)\}\}/g, (_, token) => (
        options?.[token] !== undefined ? String(options[token]) : `{{${token}}}`
      ));
    },
  }),
}));

const REPO = path.resolve(process.cwd(), '..');
// Repo file named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const REPORT = JSON.parse(fs.readFileSync(path.join(REPO, 'tests/fixtures/admin_eta_accuracy/report.json'), 'utf8'));
const URL = '/admin/eta/accuracy';

const answer = (report) => api.get.mockResolvedValue({ data: { success: true, data: report } });
const copy = (report, patch) => ({ ...report, ...patch });

const renderPage = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <EtaAccuracy />
    </QueryClientProvider>,
  );
};

beforeEach(() => {
  vi.clearAllMocks();
  answer(REPORT);
});

it('renders every report figure from the API payload', async () => {
  renderPage();

  expect(await screen.findByText('Go-live check: 75% kept over 4 orders (target 90%)')).toBeInTheDocument();
  expect(screen.getByText('Not yet')).toBeInTheDocument();
  expect(screen.queryByText('Passing')).toBeNull();
  expect(screen.getByText('Orders placed before 11:00: 75% over 4')).toBeInTheDocument();
  expect(screen.getByText(
    'Not counted: 1 booked for a later date, 1 without a time, 1 cancelled, 1 returned, 1 closed by staff, 1 still open',
  )).toBeInTheDocument();
  expect(screen.getByText('Median window: 120 min overall, 120 min for orders placed 07:00–15:00 (aim: 3 hours)'))
    .toBeInTheDocument();
  expect(screen.getByText('Delivered the same day as promised: 67%')).toBeInTheDocument();
  expect(screen.getByText('Promised tomorrow but delivered the same day: 100%')).toBeInTheDocument();
  expect(screen.getByText('Orders placed before 13:00 and delivered the same day: 75% over 4')).toBeInTheDocument();
  expect(screen.getByText('Arrived before the window opened: 67%, by a median 563 min')).toBeInTheDocument();
  expect(screen.getByText('One driver working: 67% kept, median window 120 min. Two or more: 100%, 120 min'))
    .toBeInTheDocument();
  expect(screen.getByText('Updates per order: none 75%, at most one 100%')).toBeInTheDocument();
  expect(screen.getByText(
    'On the way: kept 50%, within 15 minutes 50%, median error Google 19 min vs our engine 12 min',
  )).toBeInTheDocument();
  expect(screen.getByText(
    'On the way from a position too old for Google (1): kept 100%, within 15 minutes 0%, median error of our engine 10 min',
  )).toBeInTheDocument();
  expect(screen.getByText(
    'Stop time against the curve, by time of day: 11-13: 1.08 (41), 13-15: 1.21 (37). By order size: 0-5: 0.94 (52), 6-10: 1.32 (19)',
  )).toBeInTheDocument();
  expect(screen.getByText('Latest fit: 2026-11-02')).toBeInTheDocument();
});

it('names the thresholds the report publishes in its definitions, never its own', async () => {
  answer(copy(REPORT, {
    definitions: { morning_ends: '10:30', daytime_from: '08:00', daytime_to: '16:00', first_half_ends: '12:30', s2_tolerance_minutes: 20 },
  }));
  renderPage();

  expect(await screen.findByText('Orders placed before 10:30: 75% over 4')).toBeInTheDocument();
  expect(screen.getByText('Orders placed before 12:30 and delivered the same day: 75% over 4')).toBeInTheDocument();
  expect(screen.getByText('Median window: 120 min overall, 120 min for orders placed 08:00–16:00 (aim: 3 hours)'))
    .toBeInTheDocument();
  expect(screen.getByText(
    'On the way: kept 50%, within 20 minutes 50%, median error Google 19 min vs our engine 12 min',
  )).toBeInTheDocument();
});

it('draws the bucket table with its five columns and a row per bucket', async () => {
  renderPage();
  await screen.findByText('Go-live check: 75% kept over 4 orders (target 90%)');

  const table = screen.getByRole('table');
  ['Stops ahead', 'Driver started', 'Promises', 'Kept', 'Median window'].forEach((name) => {
    expect(within(table).getByText(name)).toBeInTheDocument();
  });
  const rows = within(table).getAllByRole('row').slice(1).map((row) => (
    within(row).getAllByRole('cell').map((cell) => cell.textContent)
  ));
  expect(rows).toEqual([
    ['0', 'Yes', '3', '67%', '120 min'],
    ['3-4', 'No', '1', '100%', '180 min'],
  ]);
});

it('shows the Passing tag when the gate passes', async () => {
  answer(copy(REPORT, { gate: { ...REPORT.gate, passes: true, kept_share: 0.95, kept: 19, n: 20 } }));
  renderPage();

  expect(await screen.findByText('Go-live check: 95% kept over 20 orders (target 90%)')).toBeInTheDocument();
  expect(screen.getByText('Passing')).toBeInTheDocument();
  expect(screen.queryByText('Not yet')).toBeNull();
});

it('shows a dash, never NaN or 0%, for a missing figure', async () => {
  answer(copy(REPORT, {
    gate: { ...REPORT.gate, kept_share: null, before_eleven: { kept: 0, kept_share: null, n: 0 } },
    median_window_minutes: { overall: null, placed_7_to_15: null },
    early_share: null,
    median_minutes_early: null,
    first_half_day_same_day: { n: 0, same_day: 0, share: null },
    s2: { ...REPORT.s2, google_median_error_minutes: null, engine_median_error_minutes: null, within_15_share: null },
    s2_stale_estimate: { n: 0, kept_share: null, within_15_share: null, engine_median_error_minutes: null },
    by_bucket: [{ stops_ahead_band: '0', started: true, n: 2, kept_share: null, median_window_minutes: null }],
    latest_fit: null,
  }));
  renderPage();

  expect(await screen.findByText('Go-live check: — kept over 4 orders (target 90%)')).toBeInTheDocument();
  expect(screen.getByText('Orders placed before 11:00: — over 0')).toBeInTheDocument();
  expect(screen.getByText('Median window: — overall, — for orders placed 07:00–15:00 (aim: 3 hours)'))
    .toBeInTheDocument();
  expect(screen.getByText('Orders placed before 13:00 and delivered the same day: — over 0')).toBeInTheDocument();
  expect(screen.getByText('Arrived before the window opened: —, by a median —')).toBeInTheDocument();
  expect(screen.getByText('On the way: kept 50%, within 15 minutes —, median error Google — vs our engine —'))
    .toBeInTheDocument();
  expect(screen.getByText(
    'On the way from a position too old for Google (0): kept —, within 15 minutes —, median error of our engine —',
  )).toBeInTheDocument();
  expect(screen.getByText('Latest fit: —')).toBeInTheDocument();
  expect(screen.queryByText('Stop time against the curve', { exact: false })).toBeNull();
  const cells = within(screen.getByRole('table')).getAllByRole('row')[1];
  expect(within(cells).getAllByRole('cell').map((cell) => cell.textContent)).toEqual(['0', 'Yes', '2', '—', '—']);
  expect(document.body.textContent).not.toMatch(/NaN|undefined|null/);
});

it('shows dashes for a report from an older backend', async () => {
  const { first_half_day_same_day: _omitted, ...older } = REPORT;
  const { first_half_ends: _omittedHour, ...definitions } = REPORT.definitions;
  answer({ ...older, definitions });
  renderPage();

  expect(await screen.findByText('Orders placed before — and delivered the same day: — over —')).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/NaN|undefined|null/);
});

it('shows the fit date on the Tashkent calendar day', async () => {
  answer(copy(REPORT, { latest_fit: { ...REPORT.latest_fit, fitted_at: '2026-11-02T20:00:00+00:00' } }));
  renderPage();

  expect(await screen.findByText('Latest fit: 2026-11-03')).toBeInTheDocument();
});

it('asks for the backend default window, then reads the answered range into the picker', async () => {
  renderPage();
  await screen.findByText('Latest fit: 2026-11-02');

  expect(api.get).toHaveBeenCalledWith(URL, { params: { start_date: undefined, end_date: undefined } });
  const inputs = document.querySelectorAll('.ant-picker-input input');
  expect(inputs[0]).toHaveValue('2026-11-03');
  expect(inputs[1]).toHaveValue('2026-11-03');
});

it('sends the picked range as start_date and end_date', async () => {
  renderPage();
  await screen.findByText('Latest fit: 2026-11-02');

  // The picker opens on the answered range's month (November 2026); pick two days from its panels.
  const startInput = document.querySelector('.ant-picker-input input');
  fireEvent.mouseDown(startInput);
  fireEvent.click(startInput);
  const cell = (isoDate) => document.querySelector(`.ant-picker-dropdown td[title="${isoDate}"]`);
  await waitFor(() => expect(cell('2026-11-05')).not.toBeNull());
  fireEvent.click(cell('2026-11-05'));
  await waitFor(() => expect(cell('2026-11-20')).not.toBeNull());
  fireEvent.click(cell('2026-11-20'));

  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith(URL, {
    params: { start_date: '2026-11-05', end_date: '2026-11-20' },
  }));
});

it('shows the empty state before any promise is recorded', async () => {
  answer({
    start_date: '2026-11-03',
    end_date: '2026-11-03',
    gate: {
      sample: 0, n: 0, kept: 0, kept_share: null, target: 0.9, passes: false,
      before_eleven: { n: 0, kept: 0, kept_share: null },
      excluded: { dated: 0, no_window: 0, cancelled: 0, returned: 0, non_driver_close: 0, open: 0 },
    },
    by_bucket: [],
    median_window_minutes: { overall: null, placed_7_to_15: null },
    today_delivered_today_share: null,
    tomorrow_delivered_same_day_share: null,
    early_share: null,
    median_minutes_early: null,
    by_drivers_working: {
      one: { n: 0, kept_share: null, median_window_minutes: null },
      two_or_more: { n: 0, kept_share: null, median_window_minutes: null },
    },
    change_messages: { n: 0, none_share: null, at_most_one_share: null },
    s2: {
      n: 0, engine_n: 0, kept_share: null, within_15_share: null,
      google_median_error_minutes: null, engine_median_error_minutes: null,
    },
    latest_fit: null,
  });
  renderPage();

  expect(await screen.findByText('No delivery promises were recorded in this period yet')).toBeInTheDocument();
  expect(screen.queryByText('Not yet')).toBeNull();
  expect(screen.queryByRole('table')).toBeNull();
  expect(document.body.textContent).not.toMatch(/NaN|undefined|null/);
});
