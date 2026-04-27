import { useMemo, useState } from 'react';
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
  message
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
  CalendarOutlined,
  GiftOutlined,
  StarOutlined
} from '@ant-design/icons';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import { formatDate } from '../utils/dateUtils';
import LineChart from '../components/charts/LineChart';
import BarChart from '../components/charts/BarChart';
import PieChart from '../components/charts/PieChart';
import adminService from '../services/adminService';
import exportUtils from '../utils/exportUtils';

const { Option } = Select;
const { RangePicker } = DatePicker;

const getTimeframeDateRange = (timeframe) => {
  const end = dayjs();
  const start = dayjs();

  if (timeframe === '7d') {
    return [start.subtract(7, 'day'), end];
  }
  if (timeframe === '90d') {
    return [start.subtract(90, 'day'), end];
  }
  if (timeframe === '1y') {
    return [start.subtract(1, 'year'), end];
  }

  return [start.subtract(30, 'day'), end];
};

const buildExportRows = (activeTab, overviewData, salesTrends, churnData, deliveryHeatmap, revenueForecast, loyaltyAnalytics) => {
  if (activeTab === 'sales') {
    return (salesTrends?.labels || []).map((label, index) => ({
      Period: label,
      Revenue: salesTrends?.revenue?.at(index) || 0,
      Orders: salesTrends?.orders?.at(index) || 0
    }));
  }

  if (activeTab === 'churn') {
    return (churnData?.customers || []).map((customer) => ({
      Customer: customer.customer_name,
      Email: customer.customer_email,
      'Risk Score': customer.risk_score,
      'Risk Level': customer.risk_level,
      'Last Order': customer.last_order_date || 'Never',
      'Total Spent': customer.total_spent || 0
    }));
  }

  if (activeTab === 'delivery') {
    return (deliveryHeatmap?.regions || []).map((region) => ({
      Region: region.region,
      Deliveries: region.total_deliveries,
      'On Time Rate': region.on_time_rate,
      'Avg Delivery Time (hrs)': region.avg_delivery_time,
      Performance: region.performance
    }));
  }

  if (activeTab === 'forecast') {
    return (revenueForecast?.labels || []).map((label, index) => ({
      Period: label,
      Historical: revenueForecast?.historical?.at(index) ?? '',
      Forecast: revenueForecast?.forecast?.at(index) ?? ''
    }));
  }

  if (activeTab === 'loyalty') {
    return [
      { Metric: 'Total Loyalty Members', Value: loyaltyAnalytics?.summary?.total_members || 0 },
      { Metric: 'Points In Circulation', Value: loyaltyAnalytics?.summary?.total_points_in_circulation || 0 },
      { Metric: 'Points Earned', Value: loyaltyAnalytics?.summary?.points_earned || 0 },
      { Metric: 'Points Redeemed', Value: loyaltyAnalytics?.summary?.points_redeemed || 0 },
    ];
  }

  return [
    {
      Metric: 'Total Revenue',
      Value: overviewData.total_revenue || 0
    },
    {
      Metric: 'Total Orders',
      Value: overviewData.total_orders || 0
    },
    {
      Metric: 'Active Customers',
      Value: overviewData.active_customers || 0
    },
    {
      Metric: 'Growth Rate',
      Value: `${overviewData.growth_rate || 0}%`
    }
  ];
};

const Analytics = () => {
  const { t } = useTranslation('analytics');
  const [activeTab, setActiveTab] = useState('overview');
  const [timeframe, setTimeframe] = useState('30d');
  const [dateRange, setDateRange] = useState(getTimeframeDateRange('30d'));

  const startDate = dateRange?.[0]?.format('YYYY-MM-DD');
  const endDate = dateRange?.[1]?.format('YYYY-MM-DD');
  const analyticsParams = {
    timeframe,
    start_date: startDate,
    end_date: endDate
  };

  const { data: analyticsData = {}, isLoading } = useQuery({
    queryKey: ['analytics', timeframe, startDate, endDate],
    queryFn: () => adminService.getAnalytics(analyticsParams),
    placeholderData: keepPreviousData,
  });

  const { data: salesTrends } = useQuery({
    queryKey: ['sales-trends', timeframe, startDate, endDate],
    queryFn: () => adminService.getSalesTrends(analyticsParams),
    placeholderData: keepPreviousData,
    enabled: activeTab === 'sales',
  });

  const { data: churnData } = useQuery({
    queryKey: ['customer-churn', timeframe, startDate, endDate],
    queryFn: () => adminService.getChurnPrediction(analyticsParams),
    placeholderData: keepPreviousData,
    enabled: activeTab === 'churn',
  });

  const { data: deliveryHeatmap } = useQuery({
    queryKey: ['delivery-heatmap', timeframe, startDate, endDate],
    queryFn: () => adminService.getDeliveryHeatmap(analyticsParams),
    placeholderData: keepPreviousData,
    enabled: activeTab === 'delivery',
  });

  const { data: revenueForecast } = useQuery({
    queryKey: ['revenue-forecast', timeframe, startDate, endDate],
    queryFn: () => adminService.getRevenueForecast(analyticsParams),
    placeholderData: keepPreviousData,
    enabled: activeTab === 'forecast',
  });

  const { data: loyaltyAnalytics } = useQuery({
    queryKey: ['analytics-loyalty', startDate, endDate],

    queryFn: () => adminService.getLoyaltyAnalytics({
      start_date: startDate,
      end_date: endDate,
    }),

    placeholderData: keepPreviousData,
    enabled: activeTab === 'loyalty',
  });

  const overviewTrendChartData = {
    labels: analyticsData.revenue_trend?.map((item) => item.label) || [],
    datasets: [
      {
        label: t('ui.analytics.revenue'),
        data: analyticsData.revenue_trend?.map((item) => item.value) || []
      },
      {
        label: t('ui.analytics.orders'),
        data: analyticsData.order_trend?.map((item) => item.value) || [],
        yAxisID: 'y1'
      }
    ]
  };

  const salesTrendChartData = {
    labels: salesTrends?.labels || [],
    datasets: [
      {
        label: t('ui.analytics.revenue'),
        data: salesTrends?.revenue || []
      },
      {
        label: t('ui.analytics.orders'),
        data: salesTrends?.orders || [],
        yAxisID: 'y1'
      }
    ]
  };

  const productPerformanceData = {
    labels: analyticsData.top_products?.map((product) => product.name) || [],
    datasets: [{
      label: t('ui.analytics.sales'),
      data: analyticsData.top_products?.map((product) => product.sales) || []
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
    values: [
      analyticsData.customer_segments?.new || 0,
      analyticsData.customer_segments?.active || 0,
      analyticsData.customer_segments?.loyal || 0,
      analyticsData.customer_segments?.at_risk || 0,
      analyticsData.customer_segments?.inactive || 0
    ]
  };

  const overviewInsights = useMemo(() => {
    const topProduct = analyticsData.top_products?.[0];
    const items = [];

    if (topProduct) {
      items.push({
        title: t('ui.analytics.top_products'),
        description: `${topProduct.name} generated ${topProduct.sales.toFixed(2)} in revenue.`
      });
    }

    items.push({
      title: t('ui.analytics.growth_rate'),
      description: `${(analyticsData.growth_rate || 0).toFixed(1)}% revenue growth for the selected range.`
    });

    items.push({
      title: t('ui.analytics.customer_segments'),
      description: `${analyticsData.customer_segments?.at_risk || 0} customers currently look at risk.`
    });

    items.push({
      title: t('ui.analytics.delivery_performance'),
      description: `${(analyticsData.delivery_success_rate || 0).toFixed(1)}% delivery success rate across the selected period.`
    });

    return items;
  }, [analyticsData, t]);

  const loyaltyTrendChartData = {
    labels: (loyaltyAnalytics?.points_trend || []).map((item) => formatDate(item.date)),
    datasets: [
      {
        label: t('ui.analytics.loyalty_points_earned', { defaultValue: 'Points Earned' }),
        data: (loyaltyAnalytics?.points_trend || []).map((item) => item.earned || 0)
      },
      {
        label: t('ui.analytics.loyalty_points_redeemed', { defaultValue: 'Points Redeemed' }),
        data: (loyaltyAnalytics?.points_trend || []).map((item) => item.redeemed || 0)
      }
    ]
  };

  const loyaltyTierDistributionData = {
    labels: (loyaltyAnalytics?.tier_distribution || []).map((item) => item.tier || 'Unknown'),
    values: (loyaltyAnalytics?.tier_distribution || []).map((item) => item.count || 0)
  };

  const loyaltyTopRewardsData = {
    labels: (loyaltyAnalytics?.top_rewards || []).map((item) => item.name),
    datasets: [{
      label: t('ui.analytics.redemptions', { defaultValue: 'Redemptions' }),
      data: (loyaltyAnalytics?.top_rewards || []).map((item) => item.redemptions || 0)
    }]
  };

  const handleTimeframeChange = (value) => {
    setTimeframe(value);
    setDateRange(getTimeframeDateRange(value));
  };

  const handleDateRangeChange = (dates) => {
    if (dates?.[0] && dates?.[1]) {
      setDateRange(dates);
    }
  };

  const handleExport = () => {
    const rows = buildExportRows(
      activeTab,
      analyticsData,
      salesTrends,
      churnData,
      deliveryHeatmap,
      revenueForecast,
      loyaltyAnalytics
    );

    const exportResult = exportUtils.exportToExcel(
      rows,
      `analytics_${activeTab}_${endDate || dayjs().format('YYYY-MM-DD')}`,
      'Analytics'
    );

    if (!exportResult.success) {
      message.error(exportResult.message);
    }
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
          {t(`ui.analytics.risk_${level || 'low'}`).toUpperCase()}
        </Tag>
      )
    },
    {
      title: t('ui.analytics.last_order'),
      dataIndex: 'last_order_date',
      key: 'last_order_date',
      width: 120,
      render: (date) => (date ? formatDate(date) : t('ui.analytics.never'))
    },
    {
      title: t('ui.analytics.total_spent'),
      dataIndex: 'total_spent',
      key: 'total_spent',
      width: 120,
      render: (amount) => `${amount?.toFixed(2) || '0.00'} UZS`
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
          {t(`ui.analytics.performance_${performance || 'average'}`).toUpperCase()}
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
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.total_revenue')}
                  value={analyticsData.total_revenue || 0}
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
                  value={analyticsData.total_orders || 0}
                  prefix={<ShoppingCartOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.active_customers')}
                  value={analyticsData.active_customers || 0}
                  prefix={<UserOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.growth_rate')}
                  value={analyticsData.growth_rate || 0}
                  precision={1}
                  suffix="%"
                  prefix={<RiseOutlined />}
                  valueStyle={{ color: (analyticsData.growth_rate || 0) > 0 ? '#52c41a' : '#f5222d' }}
                />
              </Card>
            </Col>
          </Row>

          <Row gutter={[16, 16]}>
            <Col xs={24} lg={16}>
              <Card title={t('ui.analytics.revenue_trend')} loading={isLoading}>
                <LineChart data={overviewTrendChartData} height={300} />
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
                  dataSource={overviewInsights}
                  renderItem={(item) => (
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
                    data: revenueForecast?.historical || []
                  },
                  {
                    label: t('ui.analytics.forecasted_revenue'),
                    data: revenueForecast?.forecast || []
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
              renderItem={(item) => (
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
    },
    {
      key: 'loyalty',
      label: t('ui.analytics.loyalty', { defaultValue: 'Loyalty' }),
      children: (
        <div>
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.total_loyalty_members', { defaultValue: 'Total Loyalty Members' })}
                  value={loyaltyAnalytics?.summary?.total_members || 0}
                  prefix={<UserOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.points_in_circulation', { defaultValue: 'Points In Circulation' })}
                  value={loyaltyAnalytics?.summary?.total_points_in_circulation || 0}
                  prefix={<GiftOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.total_redemptions', { defaultValue: 'Total Redemptions' })}
                  value={loyaltyAnalytics?.summary?.total_redemptions || 0}
                  prefix={<ShoppingCartOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title={t('ui.analytics.avg_redemption_value', { defaultValue: 'Average Redemption Value' })}
                  value={loyaltyAnalytics?.summary?.avg_redemption_value || 0}
                  prefix={<StarOutlined />}
                />
              </Card>
            </Col>
          </Row>

          <Row gutter={[16, 16]}>
            <Col xs={24} lg={16}>
              <Card title={t('ui.analytics.loyalty_points_trend', { defaultValue: 'Loyalty Points Trend' })}>
                <LineChart data={loyaltyTrendChartData} height={320} />
              </Card>
            </Col>
            <Col xs={24} lg={8}>
              <Card title={t('ui.analytics.tier_distribution', { defaultValue: 'Tier Distribution' })}>
                <PieChart data={loyaltyTierDistributionData} height={320} />
              </Card>
            </Col>
          </Row>

          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} lg={12}>
              <Card title={t('ui.analytics.top_rewards', { defaultValue: 'Top Rewards' })}>
                <BarChart data={loyaltyTopRewardsData} height={300} />
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title={t('ui.analytics.program_breakdown', { defaultValue: 'Program Breakdown' })}>
                <Table
                  rowKey="program_id"
                  pagination={false}
                  dataSource={loyaltyAnalytics?.program_breakdown || []}
                  columns={[
                    {
                      title: t('ui.analytics.program', { defaultValue: 'Program' }),
                      dataIndex: 'program_name',
                      key: 'program_name'
                    },
                    {
                      title: t('ui.analytics.members', { defaultValue: 'Members' }),
                      dataIndex: 'member_count',
                      key: 'member_count',
                      width: 120
                    },
                    {
                      title: t('ui.analytics.points', { defaultValue: 'Points' }),
                      dataIndex: 'points_in_circulation',
                      key: 'points_in_circulation',
                      width: 160
                    },
                    {
                      title: t('ui.analytics.rewards', { defaultValue: 'Rewards' }),
                      dataIndex: 'reward_count',
                      key: 'reward_count',
                      width: 120
                    }
                  ]}
                />
              </Card>
            </Col>
          </Row>
        </div>
      )
    }
  ];

  return (
    <div>
      <Card style={{ marginBottom: 16 }}>
        <Row justify="space-between" align="middle">
          <Col>
            <Space>
              <Select
                value={timeframe}
                onChange={handleTimeframeChange}
                style={{ width: 150 }}
              >
                <Option value="7d">{t('ui.analytics.last_7_days')}</Option>
                <Option value="30d">{t('ui.analytics.last_30_days')}</Option>
                <Option value="90d">{t('ui.analytics.last_90_days')}</Option>
                <Option value="1y">{t('ui.analytics.last_year')}</Option>
              </Select>
              <RangePicker
                allowClear={false}
                value={dateRange}
                onChange={handleDateRangeChange}
                format="YYYY-MM-DD"
              />
            </Space>
          </Col>
          <Col>
            <Button type="primary" icon={<ExportOutlined />} onClick={handleExport}>
              {t('ui.analytics.export_report')}
            </Button>
          </Col>
        </Row>
      </Card>

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={tabItems}
      />
    </div>
  );
};

export default Analytics;
