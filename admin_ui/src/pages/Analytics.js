import React, { useState } from 'react';
import {
  Card,
  Row,
  Col,
  Statistic,
  Select,
  DatePicker,
  Button,
  Space,
  Tabs,
  Table,
  Progress,
  Tag,
  List,
  Avatar,
} from 'antd';
import {
  BarChartOutlined,
  RiseOutlined,
  DollarOutlined,
  UserOutlined,
  ShoppingCartOutlined,
  WarningOutlined,
  EnvironmentOutlined,
  LineChartOutlined,
  ExportOutlined,
  CalendarOutlined
} from '@ant-design/icons';
import { useQuery } from 'react-query';
import moment from 'moment';
import LineChart from '../components/charts/LineChart';
import BarChart from '../components/charts/BarChart';
import PieChart from '../components/charts/PieChart';
import adminService from '../services/adminService';

const { Option } = Select;
const { RangePicker } = DatePicker;

const Analytics = () => {
  const [activeTab, setActiveTab] = useState('overview');
  const [dateRange, setDateRange] = useState([
    moment().subtract(30, 'days'),
    moment()
  ]);
  const [timeframe, setTimeframe] = useState('30d');

  // Fetch analytics data
  const { data: analyticsData, isLoading } = useQuery(
    ['analytics', timeframe, dateRange],
    () => adminService.getAnalytics({
      timeframe,
      start_date: dateRange[0]?.format('YYYY-MM-DD'),
      end_date: dateRange[1]?.format('YYYY-MM-DD')
    }),
    {
      keepPreviousData: true
    }
  );

  // Fetch sales trends
  const { data: salesTrends } = useQuery(
    ['sales-trends', timeframe],
    () => adminService.getSalesTrends({ timeframe }),
    {
      keepPreviousData: true,
      enabled: activeTab === 'sales'
    }
  );

  // Fetch churn prediction data
  const { data: churnData } = useQuery(
    ['customer-churn', timeframe],
    () => adminService.getChurnPrediction({ timeframe }),
    {
      keepPreviousData: true,
      enabled: activeTab === 'churn'
    }
  );

  // Fetch delivery heatmap data
  const { data: deliveryHeatmap } = useQuery(
    ['delivery-heatmap', timeframe],
    () => adminService.getDeliveryHeatmap({ timeframe }),
    {
      keepPreviousData: true,
      enabled: activeTab === 'delivery'
    }
  );

  // Fetch revenue forecast
  const { data: revenueForecast } = useQuery(
    ['revenue-forecast'],
    () => adminService.getRevenueForecast(),
    {
      keepPreviousData: true,
      enabled: activeTab === 'forecast'
    }
  );

  const overviewData = analyticsData || {};

  const salesTrendChartData = {
    labels: salesTrends?.labels || [],
    datasets: [
      {
        label: 'Revenue',
        data: salesTrends?.revenue || [],
        borderColor: '#1890ff',
        backgroundColor: 'rgba(24, 144, 255, 0.1)',
        tension: 0.4
      },
      {
        label: 'Orders',
        data: salesTrends?.orders || [],
        borderColor: '#52c41a',
        backgroundColor: 'rgba(82, 196, 26, 0.1)',
        tension: 0.4,
        yAxisID: 'y1'
      }
    ]
  };

  const productPerformanceData = {
    labels: overviewData.top_products?.map(p => p.name) || [],
    datasets: [{
      label: 'Sales',
      data: overviewData.top_products?.map(p => p.sales) || [],
      backgroundColor: ['#1890ff', '#52c41a', '#faad14', '#f5222d', '#722ed1']
    }]
  };

  const customerSegmentData = {
    labels: ['New', 'Active', 'Loyal', 'At Risk', 'Inactive'],
    datasets: [{
      data: [
        overviewData.customer_segments?.new || 0,
        overviewData.customer_segments?.active || 0,
        overviewData.customer_segments?.loyal || 0,
        overviewData.customer_segments?.at_risk || 0,
        overviewData.customer_segments?.inactive || 0
      ],
      backgroundColor: ['#52c41a', '#1890ff', '#722ed1', '#faad14', '#f5222d']
    }]
  };

  const churnColumns = [
    {
      title: 'Customer',
      dataIndex: 'customer_name',
      key: 'customer_name',
      render: (text, record) => (
        <div>
          <div style={{ fontWeight: 'bold' }}>{text}</div>
          <small style={{ color: '#666' }}>{record.customer_email}</small>
        </div>
      )
    },
    {
      title: 'Risk Score',
      dataIndex: 'risk_score',
      key: 'risk_score',
      width: 120,
      render: (score) => (
        <div>
          <Progress
            percent={score}
            size="small"
            strokeColor={score > 70 ? '#f5222d' : score > 40 ? '#faad14' : '#52c41a'}
          />
          <span style={{ fontSize: '12px' }}>{score}%</span>
        </div>
      )
    },
    {
      title: 'Risk Level',
      dataIndex: 'risk_level',
      key: 'risk_level',
      width: 100,
      render: (level) => (
        <Tag color={level === 'high' ? 'red' : level === 'medium' ? 'orange' : 'green'}>
          {level?.toUpperCase()}
        </Tag>
      )
    },
    {
      title: 'Last Order',
      dataIndex: 'last_order_date',
      key: 'last_order_date',
      width: 120,
      render: (date) => (date ? moment(date).format('MMM DD, YYYY') : 'Never')
    },
    {
      title: 'Total Spent',
      dataIndex: 'total_spent',
      key: 'total_spent',
      width: 120,
      render: (amount) => `$${amount?.toFixed(2) || '0.00'}`
    }
  ];

  const deliveryPerformanceColumns = [
    {
      title: 'Region',
      dataIndex: 'region',
      key: 'region'
    },
    {
      title: 'Deliveries',
      dataIndex: 'total_deliveries',
      key: 'total_deliveries',
      width: 100
    },
    {
      title: 'On Time Rate',
      dataIndex: 'on_time_rate',
      key: 'on_time_rate',
      width: 120,
      render: (rate) => (
        <div>
          <Progress
            percent={rate}
            size="small"
            strokeColor={rate > 90 ? '#52c41a' : rate > 70 ? '#faad14' : '#f5222d'}
          />
          <span style={{ fontSize: '12px' }}>{rate}%</span>
        </div>
      )
    },
    {
      title: 'Avg Delivery Time',
      dataIndex: 'avg_delivery_time',
      key: 'avg_delivery_time',
      width: 140,
      render: (time) => `${time} hours`
    },
    {
      title: 'Performance',
      dataIndex: 'performance',
      key: 'performance',
      width: 100,
      render: (performance) => (
        <Tag color={performance === 'excellent' ? 'green' : performance === 'good' ? 'blue' : performance === 'average' ? 'orange' : 'red'}>
          {performance?.toUpperCase()}
        </Tag>
      )
    }
  ];

  const tabItems = [
    {
      key: 'overview',
      label: 'Overview',
      children: (
        <div>
          {/* Key Metrics */}
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Total Revenue"
                  value={overviewData.total_revenue || 0}
                  precision={2}
                  prefix={<DollarOutlined />}
                  valueStyle={{ color: '#52c41a' }}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Total Orders"
                  value={overviewData.total_orders || 0}
                  prefix={<ShoppingCartOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Active Customers"
                  value={overviewData.active_customers || 0}
                  prefix={<UserOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Growth Rate"
                  value={overviewData.growth_rate || 0}
                  precision={1}
                  suffix="%"
                  prefix={<RiseOutlined />}
                  valueStyle={{ color: (overviewData.growth_rate || 0) > 0 ? '#52c41a' : '#f5222d' }}
                />
              </Card>
            </Col>
          </Row>

          {/* Charts */}
          <Row gutter={[16, 16]}>
            <Col xs={24} lg={16}>
              <Card title="Revenue Trend" loading={isLoading}>
                <LineChart data={salesTrendChartData} height={300} />
              </Card>
            </Col>
            <Col xs={24} lg={8}>
              <Card title="Customer Segments" loading={isLoading}>
                <PieChart data={customerSegmentData} height={300} />
              </Card>
            </Col>
          </Row>

          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} lg={12}>
              <Card title="Top Products" loading={isLoading}>
                <BarChart data={productPerformanceData} height={300} />
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title="Recent Insights" loading={isLoading}>
                <List
                  size="small"
                  dataSource={overviewData.insights || []}
                  renderItem={item => (
                    <List.Item>
                      <List.Item.Meta
                        avatar={<Avatar icon={<BarChartOutlined />} />}
                        title={item.title}
                        description={item.description}
                      />
                    </List.Item>
                  )}
                />
              </Card>
            </Col>
          </Row>
        </div>
      )
    },
    {
      key: 'sales',
      label: 'Sales Trends',
      children: (
        <div>
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Monthly Revenue"
                  value={salesTrends?.monthly_revenue || 0}
                  precision={2}
                  prefix="$"
                  valueStyle={{ color: '#52c41a' }}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Monthly Orders"
                  value={salesTrends?.monthly_orders || 0}
                  prefix={<ShoppingCartOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Avg Order Value"
                  value={salesTrends?.avg_order_value || 0}
                  precision={2}
                  prefix="$"
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Conversion Rate"
                  value={salesTrends?.conversion_rate || 0}
                  precision={1}
                  suffix="%"
                  valueStyle={{ color: '#1890ff' }}
                />
              </Card>
            </Col>
          </Row>

          <Card title="Sales Performance Over Time">
            <LineChart data={salesTrendChartData} height={400} />
          </Card>
        </div>
      )
    },
    {
      key: 'churn',
      label: 'Customer Churn',
      children: (
        <div>
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Churn Rate"
                  value={churnData?.churn_rate || 0}
                  precision={1}
                  suffix="%"
                  valueStyle={{ color: '#f5222d' }}
                  prefix={<WarningOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="At Risk Customers"
                  value={churnData?.at_risk_count || 0}
                  valueStyle={{ color: '#faad14' }}
                  prefix={<UserOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="High Risk Customers"
                  value={churnData?.high_risk_count || 0}
                  valueStyle={{ color: '#f5222d' }}
                  prefix={<WarningOutlined />}
                />
              </Card>
            </Col>
          </Row>

          <Card title="Customer Churn Risk Analysis">
            <Table
              columns={churnColumns}
              dataSource={churnData?.customers || []}
              rowKey="id"
              pagination={{ pageSize: 10 }}
            />
          </Card>
        </div>
      )
    },
    {
      key: 'delivery',
      label: 'Delivery Performance',
      children: (
        <div>
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Overall On-Time Rate"
                  value={deliveryHeatmap?.overall_on_time_rate || 0}
                  precision={1}
                  suffix="%"
                  valueStyle={{ color: '#52c41a' }}
                  prefix={<EnvironmentOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Avg Delivery Time"
                  value={deliveryHeatmap?.avg_delivery_time || 0}
                  precision={1}
                  suffix=" hrs"
                  prefix={<CalendarOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Failed Deliveries"
                  value={deliveryHeatmap?.failed_deliveries || 0}
                  valueStyle={{ color: '#f5222d' }}
                  prefix={<WarningOutlined />}
                />
              </Card>
            </Col>
          </Row>

          <Card title="Regional Delivery Performance">
            <Table
              columns={deliveryPerformanceColumns}
              dataSource={deliveryHeatmap?.regions || []}
              rowKey="region"
              pagination={{ pageSize: 10 }}
            />
          </Card>
        </div>
      )
    },
    {
      key: 'forecast',
      label: 'Revenue Forecast',
      children: (
        <div>
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Next Month Forecast"
                  value={revenueForecast?.next_month || 0}
                  precision={2}
                  prefix="$"
                  valueStyle={{ color: '#1890ff' }}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Next Quarter Forecast"
                  value={revenueForecast?.next_quarter || 0}
                  precision={2}
                  prefix="$"
                  valueStyle={{ color: '#52c41a' }}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Confidence Level"
                  value={revenueForecast?.confidence_level || 0}
                  precision={1}
                  suffix="%"
                  valueStyle={{ color: '#722ed1' }}
                />
              </Card>
            </Col>
          </Row>

          <Card title="Revenue Forecast Analysis">
            <LineChart
              data={{
                labels: revenueForecast?.labels || [],
                datasets: [
                  {
                    label: 'Historical Revenue',
                    data: revenueForecast?.historical || [],
                    borderColor: '#1890ff',
                    backgroundColor: 'rgba(24, 144, 255, 0.1)'
                  },
                  {
                    label: 'Forecasted Revenue',
                    data: revenueForecast?.forecast || [],
                    borderColor: '#52c41a',
                    backgroundColor: 'rgba(82, 196, 26, 0.1)',
                    borderDash: [5, 5]
                  }
                ]
              }}
              height={400}
            />
          </Card>

          <Card title="Forecast Factors" style={{ marginTop: 16 }}>
            <List
              size="small"
              dataSource={revenueForecast?.factors || []}
              renderItem={item => (
                <List.Item>
                  <List.Item.Meta
                    avatar={<Avatar icon={<LineChartOutlined />} />}
                    title={item.factor}
                    description={item.impact}
                  />
                  <div>
                    <Tag color={item.trend === 'positive' ? 'green' : item.trend === 'negative' ? 'red' : 'blue'}>
                      {item.weight}% impact
                    </Tag>
                  </div>
                </List.Item>
              )}
            />
          </Card>
        </div>
      )
    }
  ];

  return (
    <div>
      {/* Control Bar */}
      <Card style={{ marginBottom: 16 }}>
        <Row justify="space-between" align="middle">
          <Col>
            <Space>
              <Select
                value={timeframe}
                onChange={setTimeframe}
                style={{ width: 120 }}
              >
                <Option value="7d">Last 7 days</Option>
                <Option value="30d">Last 30 days</Option>
                <Option value="90d">Last 90 days</Option>
                <Option value="1y">Last year</Option>
              </Select>
              <RangePicker
                value={dateRange}
                onChange={setDateRange}
                format="YYYY-MM-DD"
              />
            </Space>
          </Col>
          <Col>
            <Button type="primary" icon={<ExportOutlined />}>
              Export Report
            </Button>
          </Col>
        </Row>
      </Card>

      {/* Analytics Tabs */}
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={tabItems}
      />
    </div>
  );
};

export default Analytics;