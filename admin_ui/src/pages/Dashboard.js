import React, { useState } from 'react';
import { Row, Col, Card, Typography, Space, Button, DatePicker, Select } from 'antd';
import {
  UserOutlined,
  ShoppingCartOutlined,
  DollarOutlined,
  TruckOutlined,
  ReloadOutlined
} from '@ant-design/icons';
import { useQuery } from 'react-query';
import moment from 'moment';
import StatCard from '../components/charts/StatCard';
import LineChart from '../components/charts/LineChart';
import BarChart from '../components/charts/BarChart';
import PieChart from '../components/charts/PieChart';
import adminService from '../services/adminService';

const { Title } = Typography;
const { RangePicker } = DatePicker;
const { Option } = Select;

const Dashboard = () => {
  const [dateRange, setDateRange] = useState([
    moment().subtract(30, 'days'),
    moment()
  ]);
  const [refreshInterval, setRefreshInterval] = useState(30000); // 30 seconds

  // Fetch dashboard data
  const { data: dashboardData, isLoading, refetch } = useQuery(
    ['dashboard', dateRange],
    () => adminService.getDashboardData({
      start_date: dateRange[0].format('YYYY-MM-DD'),
      end_date: dateRange[1].format('YYYY-MM-DD')
    }),
    {
      refetchInterval: refreshInterval,
      refetchIntervalInBackground: true
    }
  );

  // Sample data for charts (replace with real API data)
  const revenueData = {
    labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
    datasets: [{
      label: 'Revenue',
      data: [12000, 19000, 15000, 25000, 22000, 30000]
    }]
  };

  const orderStatusData = {
    labels: ['Pending', 'Processing', 'Delivered', 'Cancelled'],
    values: [45, 120, 300, 15]
  };

  const salesTrendData = {
    labels: ['Week 1', 'Week 2', 'Week 3', 'Week 4'],
    datasets: [
      {
        label: 'Orders',
        data: [65, 85, 95, 120]
      },
      {
        label: 'Revenue',
        data: [28, 48, 40, 60]
      }
    ]
  };

  const topProductsData = {
    labels: ['19L Bottle', '5L Bottle', '1L Bottle', 'Water Cooler', 'Accessories'],
    datasets: [{
      label: 'Units Sold',
      data: [320, 280, 150, 45, 25]
    }]
  };

  const handleRefresh = () => {
    refetch();
  };

  const handleDateRangeChange = (dates) => {
    if (dates) {
      setDateRange(dates);
    }
  };

  const dashboard = dashboardData?.dashboard || {};

  return (
    <div>
      {/* Header Controls */}
      <Row justify="space-between" align="top" style={{ marginBottom: 24 }} gutter={[16, 16]}>
        <Col xs={24} sm={24} md={12} lg={8}>
          <Title level={3} style={{ margin: 0 }}>Dashboard Overview</Title>
        </Col>
        <Col xs={24} sm={24} md={12} lg={16}>
          <Space direction="vertical" size="small" style={{ width: '100%' }}>
            <Space wrap size="small" style={{ width: '100%', justifyContent: 'flex-end' }}>
              <RangePicker
                value={dateRange}
                onChange={handleDateRangeChange}
                format="YYYY-MM-DD"
                style={{ width: '100%', minWidth: 200 }}
              />
              <Select
                value={refreshInterval}
                onChange={setRefreshInterval}
                style={{ width: 120 }}
              >
                <Option value={10000}>10s</Option>
                <Option value={30000}>30s</Option>
                <Option value={60000}>1m</Option>
                <Option value={0}>Off</Option>
              </Select>
              <Button
                type="primary"
                icon={<ReloadOutlined />}
                onClick={handleRefresh}
                loading={isLoading}
              >
                Refresh
              </Button>
            </Space>
          </Space>
        </Col>
      </Row>

      {/* Key Metrics */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={12} lg={6}>
          <StatCard
            title="Total Users"
            value={dashboard.users?.total || 0}
            icon={<UserOutlined />}
            color="#1890ff"
            trend="up"
            trendValue={`+${dashboard.users?.new_this_week || 0} this week`}
            loading={isLoading}
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard
            title="Total Orders"
            value={dashboard.orders?.total || 0}
            icon={<ShoppingCartOutlined />}
            color="#52c41a"
            trend="up"
            trendValue={`${dashboard.orders?.today || 0} today`}
            loading={isLoading}
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard
            title="Monthly Revenue"
            value={dashboard.orders?.revenue_month || 0}
            prefix="$"
            icon={<DollarOutlined />}
            color="#faad14"
            trend="up"
            trendValue={`$${dashboard.orders?.revenue_today || 0} today`}
            loading={isLoading}
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard
            title="Active Deliveries"
            value={dashboard.delivery?.active_deliveries || 0}
            icon={<TruckOutlined />}
            color="#722ed1"
            trend={dashboard.delivery?.failed_today > 0 ? 'down' : 'up'}
            trendValue={`${dashboard.delivery?.failed_today || 0} failed today`}
            loading={isLoading}
          />
        </Col>
      </Row>

      {/* Charts Section */}
      <Row gutter={[16, 16]}>
        {/* Revenue Trend */}
        <Col xs={24} lg={12} xl={12}>
          <Card title="Revenue Trend" className="chart-container">
            <LineChart
              data={revenueData}
              height={window.innerWidth < 768 ? 250 : 300}
              fill={true}
            />
          </Card>
        </Col>

        {/* Order Status Distribution */}
        <Col xs={24} lg={12} xl={12}>
          <Card title="Order Status Distribution" className="chart-container">
            <PieChart
              data={orderStatusData}
              height={window.innerWidth < 768 ? 250 : 300}
              doughnut={true}
            />
          </Card>
        </Col>

        {/* Sales Performance */}
        <Col xs={24}>
          <Card title="Sales Performance" className="chart-container">
            <LineChart
              data={salesTrendData}
              height={window.innerWidth < 768 ? 250 : 350}
            />
          </Card>
        </Col>

        {/* Top Products */}
        <Col xs={24}>
          <Card title="Top Products" className="chart-container">
            <BarChart
              data={topProductsData}
              height={window.innerWidth < 768 ? 200 : 300}
            />
          </Card>
        </Col>
      </Row>

      {/* Quick Stats */}
      <Row gutter={[16, 16]} style={{ marginTop: 24 }}>
        <Col xs={24} sm={12} md={8}>
          <Card>
            <div style={{ textAlign: 'center' }}>
              <Title level={4}>Pending Orders</Title>
              <Title level={2} style={{ color: '#faad14', margin: 0 }}>
                {dashboard.orders?.pending || 0}
              </Title>
            </div>
          </Card>
        </Col>
        <Col xs={24} sm={12} md={8}>
          <Card>
            <div style={{ textAlign: 'center' }}>
              <Title level={4}>Low Stock Products</Title>
              <Title level={2} style={{ color: '#ff4d4f', margin: 0 }}>
                {dashboard.products?.low_stock || 0}
              </Title>
            </div>
          </Card>
        </Col>
        <Col xs={24} sm={24} md={8}>
          <Card>
            <div style={{ textAlign: 'center' }}>
              <Title level={4}>Active Subscriptions</Title>
              <Title level={2} style={{ color: '#52c41a', margin: 0 }}>
                {dashboard.subscriptions?.active || 0}
              </Title>
            </div>
          </Card>
        </Col>
      </Row>
    </div>
  );
};

export default Dashboard;