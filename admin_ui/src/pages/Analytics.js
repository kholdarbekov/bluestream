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
import { useTranslation } from 'react-i18next';
import moment from 'moment';
import LineChart from '../components/charts/LineChart';
import BarChart from '../components/charts/BarChart';
import PieChart from '../components/charts/PieChart';
import adminService from '../services/adminService';

const { Option } = Select;
const { RangePicker } = DatePicker;

const Analytics = () => {
  const { t } = useTranslation();
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
        label: t('ui.analytics.revenue'),
        data: salesTrends?.revenue || [],
        borderColor: '#1890ff',
        backgroundColor: 'rgba(24, 144, 255, 0.1)',
        tension: 0.4
      },
      {
        label: t('ui.analytics.orders'),
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
      label: t('ui.analytics.sales'),
      data: overviewData.top_products?.map(p => p.sales) || [],
      backgroundColor: ['#1890ff', '#52c41a', '#faad14', '#f5222d', '#722ed1']
    }]
  };

  const customerSegmentData = {
    labels: [
      t('ui.analytics.segment_new'),
      t('ui.analytics.segment_active'),
      t('ui.analytics.segment_loyal'),
      t('ui.analytics.segment_at_risk'),
      t('ui.analytics.segment_inactive')
    ],
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
      title: t('ui.analytics.customer'),
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
      title: t('ui.analytics.risk_score'),
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
      title: t('ui.analytics.risk_level'),
      dataIndex: 'risk_level',
      key: 'risk_level',
      width: 100,
      render: (level) => (
        <Tag color={level === 'high' ? 'red' : level === 'medium' ? 'orange' : 'green'}>
          {t(`ui.analytics.risk_${level}`).toUpperCase()}
        </Tag>
      )
    },
    {
      title: t('ui.analytics.last_order'),
      dataIndex: 'last_order_date',
      key: 'last_order_date',
      width: 120,
      render: (date) => (date ? moment(date).format('MMM DD, YYYY') : t('ui.analytics.never'))
    },
    {
      title: t('ui.analytics.total_spent'),
      dataIndex: 'total_spent',
      key: 'total_spent',
      width: 120,
      render: (amount) => `$${amount?.toFixed(2) || '0.00'}`
    }
  ];

  const deliveryPerformanceColumns = [
    {
      title: t('ui.analytics.region'),
      dataIndex: 'region',
      key: 'region'
    },
    {
      title: t('ui.analytics.deliveries'),
      dataIndex: 'total_deliveries',
      key: 'total_deliveries',
      width: 100
    },
    {
      title: t('ui.analytics.on_time_rate'),
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
      title: t('ui.analytics.avg_delivery_time'),
      dataIndex: 'avg_delivery_time',
      key: 'avg_delivery_time',
      width: 140,
      render: (time) => `${time} ${t('ui.analytics.hours')}`
    },
    {
      title: t('ui.analytics.performance'),
      dataIndex: 'performance',
      key: 'performance',
      width: 100,
      render: (performance) => (
        <Tag color={performance === 'excellent' ? 'green' : performance === 'good' ? 'blue' : performance === 'average' ? 'orange' : 'red'}>
          {t(`ui.analytics.performance_${performance}`).toUpperCase()}
        </Tag>
      )
    }
  ];

  const tabItems = [
    {
      key: 'overview',
      label: t('ui.analytics.overview'),
      children: (
        <div>
          {/* Key Metrics */}
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.total_revenue')}
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
                  title={t('ui.analytics.total_orders')}
                  value={overviewData.total_orders || 0}
                  prefix={<ShoppingCartOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.active_customers')}
                  value={overviewData.active_customers || 0}
                  prefix={<UserOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.growth_rate')}
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
              <Card title={t('ui.analytics.revenue_trend')} loading={isLoading}>
                <LineChart data={salesTrendChartData} height={300} />
              </Card>
            </Col>
            <Col xs={24} lg={8}>
              <Card title={t('ui.analytics.customer_segments')} loading={isLoading}>
                <PieChart data={customerSegmentData} height={300} />
              </Card>
            </Col>
          </Row>

          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} lg={12}>
              <Card title={t('ui.analytics.top_products')} loading={isLoading}>
                <BarChart data={productPerformanceData} height={300} />
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title={t('ui.analytics.recent_insights')} loading={isLoading}>
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
      label: t('ui.analytics.sales_trends'),
      children: (
        <div>
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.monthly_revenue')}
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
                  title={t('ui.analytics.monthly_orders')}
                  value={salesTrends?.monthly_orders || 0}
                  prefix={<ShoppingCartOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.avg_order_value')}
                  value={salesTrends?.avg_order_value || 0}
                  precision={2}
                  prefix="$"
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.conversion_rate')}
                  value={salesTrends?.conversion_rate || 0}
                  precision={1}
                  suffix="%"
                  valueStyle={{ color: '#1890ff' }}
                />
              </Card>
            </Col>
          </Row>

          <Card title={t('ui.analytics.sales_performance_over_time')}>
            <LineChart data={salesTrendChartData} height={400} />
          </Card>
        </div>
      )
    },
    {
      key: 'churn',
      label: t('ui.analytics.customer_churn'),
      children: (
        <div>
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.analytics.churn_rate')}
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
                  title={t('ui.analytics.at_risk_customers')}
                  value={churnData?.at_risk_count || 0}
                  valueStyle={{ color: '#faad14' }}
                  prefix={<UserOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.analytics.high_risk_customers')}
                  value={churnData?.high_risk_count || 0}
                  valueStyle={{ color: '#f5222d' }}
                  prefix={<WarningOutlined />}
                />
              </Card>
            </Col>
          </Row>

          <Card title={t('ui.analytics.customer_churn_risk_analysis')}>
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
      label: t('ui.analytics.delivery_performance'),
      children: (
        <div>
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.analytics.overall_on_time_rate')}
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
                  title={t('ui.analytics.avg_delivery_time')}
                  value={deliveryHeatmap?.avg_delivery_time || 0}
                  precision={1}
                  suffix={` ${t('ui.analytics.hrs')}`}
                  prefix={<CalendarOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.analytics.failed_deliveries')}
                  value={deliveryHeatmap?.failed_deliveries || 0}
                  valueStyle={{ color: '#f5222d' }}
                  prefix={<WarningOutlined />}
                />
              </Card>
            </Col>
          </Row>

          <Card title={t('ui.analytics.regional_delivery_performance')}>
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
      label: t('ui.analytics.revenue_forecast'),
      children: (
        <div>
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.analytics.next_month_forecast')}
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
                  title={t('ui.analytics.next_quarter_forecast')}
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
                  title={t('ui.analytics.confidence_level')}
                  value={revenueForecast?.confidence_level || 0}
                  precision={1}
                  suffix="%"
                  valueStyle={{ color: '#722ed1' }}
                />
              </Card>
            </Col>
          </Row>

          <Card title={t('ui.analytics.revenue_forecast_analysis')}>
            <LineChart
              data={{
                labels: revenueForecast?.labels || [],
                datasets: [
                  {
                    label: t('ui.analytics.historical_revenue'),
                    data: revenueForecast?.historical || [],
                    borderColor: '#1890ff',
                    backgroundColor: 'rgba(24, 144, 255, 0.1)'
                  },
                  {
                    label: t('ui.analytics.forecasted_revenue'),
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

          <Card title={t('ui.analytics.forecast_factors')} style={{ marginTop: 16 }}>
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
                      {item.weight}% {t('ui.analytics.impact')}
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
                style={{ width: 150 }}
              >
                <Option value="7d">{t('ui.analytics.last_7_days')}</Option>
                <Option value="30d">{t('ui.analytics.last_30_days')}</Option>
                <Option value="90d">{t('ui.analytics.last_90_days')}</Option>
                <Option value="1y">{t('ui.analytics.last_year')}</Option>
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
              {t('ui.analytics.export_report')}
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