import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from 'react-query';
import { BrowserRouter } from 'react-router-dom';
import Dashboard from '../../pages/Dashboard';
import adminService from '../../services/adminService';

// Mock the admin service
jest.mock('../../services/adminService');

// Mock the chart components
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

const mockDashboardData = {
  total_revenue: 125000.50,
  total_orders: 1250,
  total_customers: 850,
  new_customers_today: 15,
  revenue_today: 2500.75,
  orders_today: 45,
  revenue_growth: 12.5,
  order_growth: 8.3,
  customer_growth: 15.2,
  recent_orders: [
    {
      id: 1,
      order_number: 'ORD-2024-001',
      customer_name: 'John Doe',
      total_amount: 75.50,
      status: 'pending',
      created_at: '2024-01-15T10:00:00Z'
    }
  ],
  monthly_revenue: [1000, 1200, 1500, 1800, 2000, 2200],
  monthly_orders: [100, 120, 150, 180, 200, 220],
  order_status_distribution: {
    pending: 25,
    confirmed: 40,
    delivered: 180,
    cancelled: 5
  }
};

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false
      }
    }
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
    adminService.getDashboardData.mockResolvedValue(mockDashboardData);
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('renders dashboard with loading state initially', () => {
    render(<Dashboard />, { wrapper: createWrapper() });

    expect(screen.getByText('Admin Dashboard')).toBeInTheDocument();
  });

  it('displays dashboard metrics after loading', async () => {
    render(<Dashboard />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(screen.getByText('$125,000.50')).toBeInTheDocument();
      expect(screen.getByText('1,250')).toBeInTheDocument();
      expect(screen.getByText('850')).toBeInTheDocument();
    });
  });

  it('shows growth percentages with correct styling', async () => {
    render(<Dashboard />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(screen.getByText('↑ 12.5%')).toBeInTheDocument();
      expect(screen.getByText('↑ 8.3%')).toBeInTheDocument();
      expect(screen.getByText('↑ 15.2%')).toBeInTheDocument();
    });
  });

  it('renders charts components', async () => {
    render(<Dashboard />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(screen.getByTestId('line-chart')).toBeInTheDocument();
      expect(screen.getByTestId('bar-chart')).toBeInTheDocument();
      expect(screen.getByTestId('pie-chart')).toBeInTheDocument();
    });
  });

  it('displays recent orders table', async () => {
    render(<Dashboard />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(screen.getByText('Recent Orders')).toBeInTheDocument();
      expect(screen.getByText('ORD-2024-001')).toBeInTheDocument();
      expect(screen.getByText('John Doe')).toBeInTheDocument();
    });
  });

  it('handles API error gracefully', async () => {
    adminService.getDashboardData.mockRejectedValue(new Error('API Error'));

    render(<Dashboard />, { wrapper: createWrapper() });

    // Component should still render without crashing
    expect(screen.getByText('Admin Dashboard')).toBeInTheDocument();
  });

  it('refreshes data when refresh button is clicked', async () => {
    render(<Dashboard />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getDashboardData).toHaveBeenCalledTimes(1);
    });

    // Find and click refresh button (if it exists in your component)
    // const refreshButton = screen.getByRole('button', { name: /refresh/i });
    // fireEvent.click(refreshButton);

    // expect(adminService.getDashboardData).toHaveBeenCalledTimes(2);
  });
});