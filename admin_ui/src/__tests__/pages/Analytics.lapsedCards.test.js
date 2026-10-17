import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import Analytics from '../../pages/Analytics';
import adminService from '../../services/adminService';
import exportUtils from '../../utils/exportUtils';
import { LAPSED_CUSTOMERS_SUMMARY_KEY } from '../../utils/constants';

vi.mock('../../services/adminService');
vi.mock('../../services/salesService');
// The spy is bound to the REAL instance's method (see Analytics.agentPerformance.test.js): a
// rename in exportUtils.js fails here instead of leaving the inactive-tab export test green.
vi.mock('../../utils/exportUtils', async (importOriginal) => {
  const { default: real } = await importOriginal();
  vi.spyOn(real, 'exportToExcel').mockReturnValue({ success: true, message: 'Excel file exported successfully' });
  return { __esModule: true, default: real };
});
const mockAuth = { hasPermission: vi.fn(() => true) };
vi.mock('../../stores/authStore', () => ({ useAuthStore: () => mockAuth }));
vi.mock('../../components/charts/LineChart', () => ({ default: function MockLineChart() { return <div data-testid="line-chart" />; } }));
vi.mock('../../components/charts/BarChart', () => ({ default: function MockBarChart() { return <div data-testid="bar-chart" />; } }));
// The pie's labels and values are the assertion target: the at-risk and inactive slices are gone.
vi.mock('../../components/charts/PieChart', () => ({
  default: function MockPieChart({ data }) {
    return <div data-testid="pie-chart" data-labels={data.labels.join('|')} data-values={data.values.join(',')} />;
  },
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (typeof opts === 'string' ? opts : opts?.defaultValue) || key }),
}));

// The envelope `ReorderReminderWorklist.summary()` publishes under `counts` (COUNTED_TABS), pinned
// by tests/unit/test_lapsed_customers_ui_contract.py.
const summaryWith = (counts) => ({ counts });

// What the pre-24b normaliser used to add: the page must ignore it, not draw it.
const STALE_SEGMENTS = { new: 4, active: 12, loyal: 7, at_risk: 5, inactive: 3 };

const renderPage = (queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) => {
  render(
    <MemoryRouter initialEntries={['/analytics']}>
      <QueryClientProvider client={queryClient}>
        <Analytics />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return queryClient;
};

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
  adminService.getAnalytics.mockResolvedValue({
    total_revenue: 0, total_orders: 0, active_customers: 0, growth_rate: 0,
    revenue_trend: [], order_trend: [],
    top_products: [{ id: 1, name: 'Water 19L', sales: 80000 }],
    customer_segments: STALE_SEGMENTS,
  });
  adminService.getReorderReminderSummary.mockResolvedValue(summaryWith({ follow_up: 4, reminding: 9 }));
});

afterEach(() => {
  vi.useRealTimers();
});

it('shows Being reminded and Needs follow-up from the published counts, each linking to the worklist', async () => {
  renderPage();

  const reminding = await screen.findByTestId('analytics-being-reminded');
  const followUp = screen.getByTestId('analytics-needs-follow-up');
  await waitFor(() => expect(reminding).toHaveTextContent('9'));
  expect(followUp).toHaveTextContent('4');
  expect(reminding).toHaveTextContent('Being reminded');
  expect(followUp).toHaveTextContent('Needs follow-up');
  expect(reminding.closest('a')).toHaveAttribute('href', '/lapsed-customers');
  expect(followUp.closest('a')).toHaveAttribute('href', '/lapsed-customers');
  expect(reminding.closest('a')).toHaveAttribute('title', 'Open Lapsed customers');
  expect(followUp.closest('a')).toHaveAttribute('title', 'Open Lapsed customers');
  expect(adminService.getReorderReminderSummary).toHaveBeenCalledTimes(1);
});

it('shows a real zero as 0, not as an unknown', async () => {
  adminService.getReorderReminderSummary.mockResolvedValue(summaryWith({ follow_up: 0, reminding: 0 }));
  renderPage();

  const reminding = await screen.findByTestId('analytics-being-reminded');
  expect(reminding).toHaveTextContent('0');
  expect(screen.getByTestId('analytics-needs-follow-up')).toHaveTextContent('0');
});

it('draws the overview pie with exactly New, Active and Loyal, from the three published figures', async () => {
  renderPage();

  const pie = await screen.findByTestId('pie-chart');
  expect(pie).toHaveAttribute(
    'data-labels',
    'ui.analytics.segment_new|ui.analytics.segment_active|ui.analytics.segment_loyal',
  );
  expect(pie).toHaveAttribute('data-values', '4,12,7');
});

it('drops the at-risk sentence from the insight list and keeps the other three', async () => {
  renderPage();

  await screen.findByTestId('pie-chart');
  const insights = document.querySelector('.ant-list');
  const titles = [...insights.querySelectorAll('.ant-list-item-meta-title')].map((node) => node.textContent);
  expect(titles).toEqual([
    'ui.analytics.top_products',
    'ui.analytics.growth_rate',
    'ui.analytics.delivery_performance',
  ]);
  expect(within(insights).queryByText(/currently look at risk/)).toBeNull();
  expect(screen.queryByText(/at risk/i)).toBeNull();
});

it('shares the summary cache entry with the nav badge and the page, and never polls it itself', async () => {
  vi.useFakeTimers();
  const queryClient = renderPage();

  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(adminService.getReorderReminderSummary).toHaveBeenCalledTimes(1);
  expect(queryClient.getQueryData(LAPSED_CUSTOMERS_SUMMARY_KEY)).toEqual(summaryWith({ follow_up: 4, reminding: 9 }));

  // The nav badge polls this key every 60 s; the cards must add no timer of their own.
  await act(async () => { await vi.advanceTimersByTimeAsync(300_000); });
  expect(adminService.getReorderReminderSummary).toHaveBeenCalledTimes(1);
});

it('reads the counts another surface already cached under the shared key, without asking again', async () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(LAPSED_CUSTOMERS_SUMMARY_KEY, summaryWith({ follow_up: 3, reminding: 8 }));
  renderPage(queryClient);

  await waitFor(() => expect(screen.getByTestId('analytics-being-reminded')).toHaveTextContent('8'));
  expect(screen.getByTestId('analytics-needs-follow-up')).toHaveTextContent('3');
  expect(adminService.getReorderReminderSummary).not.toHaveBeenCalled();
});

it('shows no cards and sends no request without can_manage_lapsed_customers', async () => {
  mockAuth.hasPermission.mockImplementation((permission) => permission !== 'can_manage_lapsed_customers');
  renderPage();

  await screen.findByTestId('pie-chart');
  await new Promise((resolve) => { setTimeout(resolve, 0); });
  expect(screen.queryByTestId('analytics-being-reminded')).toBeNull();
  expect(screen.queryByTestId('analytics-needs-follow-up')).toBeNull();
  expect(screen.queryByText('Being reminded')).toBeNull();
  expect(adminService.getReorderReminderSummary).not.toHaveBeenCalled();
  expect(mockAuth.hasPermission).toHaveBeenCalledWith('can_manage_lapsed_customers');
});

it('hides the cards, not fakes a 0, when the summary cannot be read, and the rest of Analytics renders', async () => {
  adminService.getReorderReminderSummary.mockRejectedValue(new Error('summary unavailable'));
  const queryClient = renderPage();

  await waitFor(() => expect(queryClient.getQueryState(LAPSED_CUSTOMERS_SUMMARY_KEY)?.status).toBe('error'));
  expect(adminService.getReorderReminderSummary).toHaveBeenCalledTimes(1);
  expect(screen.queryByTestId('analytics-being-reminded')).toBeNull();
  expect(screen.queryByTestId('analytics-needs-follow-up')).toBeNull();
  expect(await screen.findByTestId('pie-chart')).toHaveAttribute('data-values', '4,12,7');
  expect(screen.getByTestId('line-chart')).toBeInTheDocument();
  expect(screen.getByText('ui.analytics.total_revenue')).toBeInTheDocument();
  expect(screen.getByText('ui.analytics.growth_rate', { selector: '.ant-statistic-title' })).toBeInTheDocument();
});

describe('inactive customers tab', () => {
  const NEVER_DELIVERED = {
    user_id: 22, full_name: 'Madina Qodirova', phone: '+998901112244', customer_type: 'individual',
    entity_subtype: null, total_orders: 0, total_spent: 0, last_order_date: null,
    days_since_last_order: 12, never_ordered: true,
  };
  const DELIVERED = {
    user_id: 21, full_name: 'Dilshod Karimov', phone: '+998901112233', customer_type: 'individual',
    entity_subtype: null, total_orders: 3, total_spent: 90000, last_order_date: '2026-08-25T10:00:00+00:00',
    days_since_last_order: 51, never_ordered: false,
  };

  const openInactiveTab = async () => {
    adminService.getInactiveCustomers.mockResolvedValue({ items: [DELIVERED, NEVER_DELIVERED], meta: { total: 2 } });
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'Inactive Customers' }));
    return screen.findByRole('row', { name: /Dilshod Karimov/ });
  };

  it('names the two columns after the last delivery, which is what the backend now measures', async () => {
    const row = await openInactiveTab();

    expect(screen.getByRole('columnheader', { name: 'Last Delivery' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Days Since Last Delivery' })).toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'ui.analytics.last_order' })).toBeNull();
    expect(screen.queryByText('Days Since Last Order')).toBeNull();
    expect(row).toHaveTextContent('51');
    expect(screen.getByRole('row', { name: /Madina Qodirova/ })).toHaveTextContent('ui.analytics.never');
  });

  it('exports the same two columns under the same names', async () => {
    await openInactiveTab();

    fireEvent.click(screen.getByRole('button', { name: /export_report/i }));

    expect(exportUtils.exportToExcel).toHaveBeenCalledTimes(1);
    const [rows] = exportUtils.exportToExcel.mock.calls[0];
    expect(Object.keys(rows[0])).toEqual([
      'Name', 'Phone', 'Customer Type', 'Total Orders', 'Total Spent', 'Last Delivery', 'Days Since Last Delivery',
    ]);
    expect(rows.map((row) => [row['Last Delivery'], row['Days Since Last Delivery']])).toEqual([
      ['2026-08-25T10:00:00+00:00', 51],
      ['Never', 12],
    ]);
  });
});
