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
import useResponsive from '../hooks/useResponsive';
import { useTranslation } from 'react-i18next';

const { Title } = Typography;
const { RangePicker } = DatePicker;
const { Option } = Select;

const Dashboard = () => {
  const { t } = useTranslation();
  const [dateRange, setDateRange] = useState([
    moment().subtract(30, 'days'),
    moment()
  ]);
  const [refreshInterval, setRefreshInterval] = useState(30000); // 30 seconds
  const responsive = useResponsive();

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
    labels: [
      t('ui.dashboard.jan'),
      t('ui.dashboard.feb'),
      t('ui.dashboard.mar'),
      t('ui.dashboard.apr'),
      t('ui.dashboard.may'),
      t('ui.dashboard.jun')
    ],
    datasets: [{
      label: t('ui.dashboard.revenue'),
      data: [12000, 19000, 15000, 25000, 22000, 30000]
    }]
  };

  const orderStatusData = {
    labels: [
      t('ui.dashboard.pending'),
      t('ui.dashboard.processing'),
      t('ui.dashboard.delivered'),
      t('ui.dashboard.cancelled')
    ],
    values: [45, 120, 300, 15]
  };

  const salesTrendData = {
    labels: [
      `${t('ui.dashboard.week')} 1`,
      `${t('ui.dashboard.week')} 2`,
      `${t('ui.dashboard.week')} 3`,
      `${t('ui.dashboard.week')} 4`
    ],
    datasets: [
      {
        label: t('ui.dashboard.orders'),
        data: [65, 85, 95, 120]
      },
      {
        label: t('ui.dashboard.revenue'),
        data: [28, 48, 40, 60]
      }
    ]
  };

  const topProductsData = {
    labels: ['19L Bottle', '5L Bottle', '1L Bottle', 'Water Cooler', 'Accessories'],
    datasets: [{
      label: t('ui.dashboard.units_sold'),
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

  // Get chart height based on device
  const getChartHeight = (mobileHeight = 200, tabletHeight = 250, desktopHeight = 300) => {
    if (responsive.isMobileDevice) return mobileHeight;
    if (responsive.isTabletDevice) return tabletHeight;
    return desktopHeight;
  };

  const dashboard = dashboardData?.dashboard || {};

  return (
    <div>
      {/* Header Controls - Universal Responsive Layout */}
      <Row 
        justify="space-between" 
        align="middle" 
        style={{ marginBottom: 24 }} 
        gutter={[16, 16]}
      >
        {/* Title Section */}
        <Col xs={24} sm={24} md={8} lg={6}>
          <Title
            level={3}
            style={{
              margin: 0,
              fontSize: responsive.getFontSize('18px', '20px', '24px')
            }}
          >
            {t('ui.dashboard.title')}
          </Title>
        </Col>
        
        {/* Controls Section */}
        <Col xs={24} sm={24} md={16} lg={18}>
          <Space 
            wrap 
            size="middle" 
            style={{ 
              width: '100%',
              justifyContent: responsive.isMobileDevice ? 'center' : 'flex-end'
            }}
          >
            <RangePicker
              value={dateRange}
              onChange={handleDateRangeChange}
              format="YYYY-MM-DD"
              style={{ 
                minWidth: responsive.isMobileDevice ? '200px' : '220px',
                minHeight: responsive.isTouchDevice ? '40px' : '32px'
              }}
            />
            <Select
              value={refreshInterval}
              onChange={setRefreshInterval}
              style={{
                width: '120px',
                minHeight: responsive.isTouchDevice ? '40px' : '32px'
              }}
            >
              <Option value={10000}>{t('ui.dashboard.refresh_10s')}</Option>
              <Option value={30000}>{t('ui.dashboard.refresh_30s')}</Option>
              <Option value={60000}>{t('ui.dashboard.refresh_1m')}</Option>
              <Option value={0}>{t('ui.dashboard.refresh_off')}</Option>
            </Select>
            <Button
              type="primary"
              icon={<ReloadOutlined />}
              onClick={handleRefresh}
              loading={isLoading}
              style={{
                minHeight: responsive.isTouchDevice ? '40px' : '32px'
              }}
            >
              {t('ui.dashboard.refresh')}
            </Button>
          </Space>
        </Col>
      </Row>

      {/* Key Metrics - Responsive Grid */}
      <Row 
        gutter={[
          responsive.isMobileDevice ? 8 : 16, 
          responsive.isMobileDevice ? 8 : 16
        ]} 
        style={{ 
          marginBottom: responsive.isMobileDevice ? 16 : 24 
        }}
      >
        <Col xs={24} sm={12} lg={6}>
          <StatCard
            title={t('ui.dashboard.total_users')}
            value={dashboard.users?.total || 0}
            icon={<UserOutlined />}
            color="#1890ff"
            trend="up"
            trendValue={`+${dashboard.users?.new_this_week || 0} ${t('ui.dashboard.this_week')}`}
            loading={isLoading}
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard
            title={t('ui.dashboard.total_orders')}
            value={dashboard.orders?.total || 0}
            icon={<ShoppingCartOutlined />}
            color="#52c41a"
            trend="up"
            trendValue={`${dashboard.orders?.today || 0} ${t('ui.dashboard.today')}`}
            loading={isLoading}
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard
            title={t('ui.dashboard.monthly_revenue')}
            value={dashboard.orders?.revenue_month || 0}
            prefix="$"
            icon={<DollarOutlined />}
            color="#faad14"
            trend="up"
            trendValue={`$${dashboard.orders?.revenue_today || 0} ${t('ui.dashboard.today')}`}
            loading={isLoading}
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard
            title={t('ui.dashboard.active_deliveries')}
            value={dashboard.delivery?.active_deliveries || 0}
            icon={<TruckOutlined />}
            color="#722ed1"
            trend={dashboard.delivery?.failed_today > 0 ? 'down' : 'up'}
            trendValue={`${dashboard.delivery?.failed_today || 0} ${t('ui.dashboard.failed_today')}`}
            loading={isLoading}
          />
        </Col>
      </Row>

      {/* Charts Section - Responsive Layout */}
      <Row gutter={[
        responsive.isMobileDevice ? 8 : 16, 
        responsive.isMobileDevice ? 8 : 16
      ]}>
        {/* Revenue Trend */}
        <Col xs={24} lg={12}>
          <Card
            title={t('ui.dashboard.revenue_trend')}
            className="chart-container"
            headStyle={{
              fontSize: responsive.getFontSize('14px', '16px', '16px'),
              padding: responsive.isMobileDevice ? '12px' : '16px 24px'
            }}
          >
            <LineChart
              data={revenueData}
              height={getChartHeight(200, 250, 300)}
              fill={true}
            />
          </Card>
        </Col>

        {/* Order Status Distribution */}
        <Col xs={24} lg={12}>
          <Card
            title={t('ui.dashboard.order_status_distribution')}
            className="chart-container"
            headStyle={{
              fontSize: responsive.getFontSize('14px', '16px', '16px'),
              padding: responsive.isMobileDevice ? '12px' : '16px 24px'
            }}
          >
            <PieChart
              data={orderStatusData}
              height={getChartHeight(200, 250, 300)}
              doughnut={true}
            />
          </Card>
        </Col>

        {/* Sales Performance - Full Width */}
        <Col xs={24}>
          <Card
            title={t('ui.dashboard.sales_performance')}
            className="chart-container"
            headStyle={{
              fontSize: responsive.getFontSize('14px', '16px', '16px'),
              padding: responsive.isMobileDevice ? '12px' : '16px 24px'
            }}
          >
            <LineChart
              data={salesTrendData}
              height={getChartHeight(220, 280, 350)}
            />
          </Card>
        </Col>

        {/* Top Products - Full Width */}
        <Col xs={24}>
          <Card
            title={t('ui.dashboard.top_products')}
            className="chart-container"
            headStyle={{
              fontSize: responsive.getFontSize('14px', '16px', '16px'),
              padding: responsive.isMobileDevice ? '12px' : '16px 24px'
            }}
          >
            <BarChart
              data={topProductsData}
              height={getChartHeight(180, 220, 300)}
            />
          </Card>
        </Col>
      </Row>

      {/* Quick Stats - Responsive Three Column Layout */}
      <Row 
        gutter={[
          responsive.isMobileDevice ? 8 : 16, 
          responsive.isMobileDevice ? 8 : 16
        ]} 
        style={{ 
          marginTop: responsive.isMobileDevice ? 16 : 24 
        }}
      >
        <Col xs={24} sm={12} md={8}>
          <Card bodyStyle={{
            textAlign: 'center',
            padding: responsive.isMobileDevice ? '16px' : '20px'
          }}>
            <Title
              level={responsive.isMobileDevice ? 5 : 4}
              style={{
                marginBottom: responsive.isMobileDevice ? 8 : 16,
                fontSize: responsive.getFontSize('14px', '16px', '18px')
              }}
            >
              {t('ui.dashboard.pending')} {t('ui.dashboard.orders')}
            </Title>
            <Title
              level={responsive.isMobileDevice ? 3 : 2}
              style={{
                color: '#faad14',
                margin: 0,
                fontSize: responsive.getFontSize('24px', '32px', '36px')
              }}
            >
              {dashboard.orders?.pending || 0}
            </Title>
          </Card>
        </Col>

        <Col xs={24} sm={12} md={8}>
          <Card bodyStyle={{
            textAlign: 'center',
            padding: responsive.isMobileDevice ? '16px' : '20px'
          }}>
            <Title
              level={responsive.isMobileDevice ? 5 : 4}
              style={{
                marginBottom: responsive.isMobileDevice ? 8 : 16,
                fontSize: responsive.getFontSize('14px', '16px', '18px')
              }}
            >
              Low Stock Products
            </Title>
            <Title
              level={responsive.isMobileDevice ? 3 : 2}
              style={{
                color: '#ff4d4f',
                margin: 0,
                fontSize: responsive.getFontSize('24px', '32px', '36px')
              }}
            >
              {dashboard.products?.low_stock || 0}
            </Title>
          </Card>
        </Col>

        <Col xs={24} sm={24} md={8}>
          <Card bodyStyle={{
            textAlign: 'center',
            padding: responsive.isMobileDevice ? '16px' : '20px'
          }}>
            <Title
              level={responsive.isMobileDevice ? 5 : 4}
              style={{
                marginBottom: responsive.isMobileDevice ? 8 : 16,
                fontSize: responsive.getFontSize('14px', '16px', '18px')
              }}
            >
              Active Subscriptions
            </Title>
            <Title
              level={responsive.isMobileDevice ? 3 : 2}
              style={{
                color: '#52c41a',
                margin: 0,
                fontSize: responsive.getFontSize('24px', '32px', '36px')
              }}
            >
              {dashboard.subscriptions?.active || 0}
            </Title>
          </Card>
        </Col>
      </Row>
    </div>
  );
};

export default Dashboard;