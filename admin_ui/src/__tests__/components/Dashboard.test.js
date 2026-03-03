import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from 'react-query';
import { BrowserRouter } from 'react-router-dom';

import Dashboard from '../../pages/Dashboard';
import adminService from '../../services/adminService';

jest.mock('../../services/adminService');

jest.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key) => {
      const translations = {
        'ui.dashboard.title': 'Admin Dashboard',
        'ui.dashboard.refresh': 'Refresh',
        'ui.dashboard.this_week': 'this week',
        'ui.dashboard.today': 'today',
        'ui.dashboard.failed_today': 'failed today',
        'ui.dashboard.total_users': 'Total Users',
        'ui.dashboard.total_orders': 'Total Orders',
        'ui.dashboard.monthly_revenue': 'Monthly Revenue',
        'ui.dashboard.active_deliveries': 'Active Deliveries',
        'ui.dashboard.revenue_trend': 'Revenue Trend',
        'ui.dashboard.order_status_distribution': 'Order Status Distribution',
        'ui.dashboard.sales_performance': 'Sales Performance',
        'ui.dashboard.top_products': 'Top Products',
        'ui.dashboard.pending': 'Pending',
        'ui.dashboard.orders': 'Orders',
        'ui.dashboard.refresh_10s': '10s',
        'ui.dashboard.refresh_30s': '30s',
        'ui.dashboard.refresh_1m': '1m',
        'ui.dashboard.refresh_off': 'Off',
        'ui.dashboard.jan': 'Jan',
        'ui.dashboard.feb': 'Feb',
        'ui.dashboard.mar': 'Mar',
        'ui.dashboard.apr': 'Apr',
        'ui.dashboard.may': 'May',
        'ui.dashboard.jun': 'Jun',
        'ui.dashboard.revenue': 'Revenue',
        'ui.dashboard.processing': 'Processing',
        'ui.dashboard.delivered': 'Delivered',
        'ui.dashboard.cancelled': 'Cancelled',
        'ui.dashboard.week': 'Week',
        'ui.dashboard.units_sold': 'Units Sold',
      };
      return translations[key] || key;
    },
  }),
}));

jest.mock('../../hooks/useResponsive', () => () => ({
  isMobileDevice: false,
  isTabletDevice: false,
  isTouchDevice: false,
  getFontSize: (mobile, _tablet, desktop) => desktop || mobile,
}));

jest.mock('../../components/charts/StatCard', () => {
  return function MockStatCard({ title, value, trendValue, prefix, loading }) {
    return (
      <div data-testid="stat-card">
        <div>{title}</div>
        <div>{prefix ? `${prefix}${value}` : String(value)}</div>
        <div>{trendValue}</div>
        <div>{loading ? 'loading' : 'loaded'}</div>
      </div>
    );
  };
});

jest.mock('../../components/charts/LineChart', () => {
  return function MockLineChart() {
    return <div data-testid="line-chart">Line Chart</div>;
  };
});

jest.mock('../../components/charts/BarChart', () => {
  return function MockBarChart() {
    return <div data-testid="bar-chart">Bar Chart</div>;
  };
});

jest.mock('../../components/charts/PieChart', () => {
  return function MockPieChart() {
    return <div data-testid="pie-chart">Pie Chart</div>;
  };
});

const mockDashboardResponse = {
  dashboard: {
    users: {
      total: 850,
      new_this_week: 15,
    },
    orders: {
      total: 1250,
      today: 45,
      revenue_month: 125000.5,
      revenue_today: 2500.75,
      pending: 25,
    },
    delivery: {
      active_deliveries: 12,
      failed_today: 2,
    },
    products: {
      low_stock: 7,
    },
    subscriptions: {
      active: 42,
    },
  },
};

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        cacheTime: 0,
      },
    },
  });

  return ({ children }) => (
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    </BrowserRouter>
  );
};

describe('Dashboard Component', () => {
  beforeEach(() => {
    adminService.getDashboardData.mockResolvedValue(mockDashboardResponse);
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('renders the dashboard header immediately', () => {
    render(<Dashboard />, { wrapper: createWrapper() });

    expect(screen.getByText('Admin Dashboard')).toBeInTheDocument();
    expect(screen.getByText('Refresh')).toBeInTheDocument();
  });

  it('loads and displays stat cards from the current dashboard payload shape', async () => {
    render(<Dashboard />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getDashboardData).toHaveBeenCalled();
    });

    const statCards = await screen.findAllByTestId('stat-card');

    expect(statCards).toHaveLength(4);
    await waitFor(() => {
      expect(within(statCards[0]).getByText('Total Users')).toBeInTheDocument();
      expect(within(statCards[0]).getByText('850')).toBeInTheDocument();
      expect(within(statCards[1]).getByText('Total Orders')).toBeInTheDocument();
      expect(within(statCards[1]).getByText('1250')).toBeInTheDocument();
      expect(within(statCards[2]).getByText('Monthly Revenue')).toBeInTheDocument();
      expect(within(statCards[2]).getByText('$125000.5')).toBeInTheDocument();
      expect(within(statCards[3]).getByText('Active Deliveries')).toBeInTheDocument();
      expect(within(statCards[3]).getByText('12')).toBeInTheDocument();
    });
  });

  it('renders all dashboard chart sections', async () => {
    render(<Dashboard />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(screen.getAllByTestId('line-chart')).toHaveLength(2);
      expect(screen.getAllByTestId('bar-chart')).toHaveLength(1);
      expect(screen.getAllByTestId('pie-chart')).toHaveLength(1);
      expect(screen.getByText('Revenue Trend')).toBeInTheDocument();
      expect(screen.getByText('Order Status Distribution')).toBeInTheDocument();
      expect(screen.getByText('Sales Performance')).toBeInTheDocument();
      expect(screen.getByText('Top Products')).toBeInTheDocument();
    });
  });

  it('keeps the page shell rendered when the dashboard query fails', async () => {
    adminService.getDashboardData.mockRejectedValue(new Error('API Error'));

    render(<Dashboard />, { wrapper: createWrapper() });

    expect(screen.getByText('Admin Dashboard')).toBeInTheDocument();
    await waitFor(() => {
      expect(adminService.getDashboardData).toHaveBeenCalled();
    });
  });

  it('refetches dashboard data when refresh is clicked', async () => {
    const user = userEvent.setup();
    render(<Dashboard />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getDashboardData).toHaveBeenCalled();
    });

    const initialCallCount = adminService.getDashboardData.mock.calls.length;
    const refreshButton = screen.getByText('Refresh').closest('button');

    expect(refreshButton).not.toBeNull();
    await user.click(refreshButton);

    await waitFor(() => {
      expect(adminService.getDashboardData.mock.calls.length).toBeGreaterThan(initialCallCount);
    });
  });
});
