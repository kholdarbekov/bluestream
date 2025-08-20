"""
Analytics Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for analytics-related data serialization
"""
from datetime import datetime, date
from typing import Dict, Any, Optional, List, Union
from enum import Enum
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic.alias_generators import to_camel


class MetricType(str, Enum):
    REVENUE = "revenue"
    ORDERS = "orders"
    USERS = "users"
    PRODUCTS = "products"
    DELIVERIES = "deliveries"
    CONVERSIONS = "conversions"


class TimeRange(str, Enum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"
    LAST_90_DAYS = "last_90_days"
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"
    THIS_YEAR = "this_year"
    CUSTOM = "custom"


class ChartType(str, Enum):
    LINE = "line"
    BAR = "bar"
    PIE = "pie"
    AREA = "area"
    DONUT = "donut"


class DashboardMetricSchema(BaseModel):
    """Dashboard metric schema"""
    name: str
    value: Union[int, float, Decimal]
    previous_value: Optional[Union[int, float, Decimal]] = None
    change_percentage: Optional[float] = None
    change_direction: str = Field(default="neutral")  # up, down, neutral
    format_type: str = Field(default="number")  # number, currency, percentage
    icon: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    
    @field_validator('value', 'previous_value')
    @classmethod
    def validate_numeric_values(cls, v):
        if isinstance(v, Decimal):
            return float(v)
        return v


class ChartDataPointSchema(BaseModel):
    """Chart data point schema"""
    label: str
    value: Union[int, float, Decimal]
    date: Optional[Union[datetime, date, str]] = None
    category: Optional[str] = None
    color: Optional[str] = None
    
    @field_validator('value')
    @classmethod
    def validate_value(cls, v):
        if isinstance(v, Decimal):
            return float(v)
        return v


class ChartSchema(BaseModel):
    """Chart schema"""
    title: str
    chart_type: ChartType
    data: List[ChartDataPointSchema]
    total: Optional[Union[int, float]] = None
    format_type: str = Field(default="number")
    currency: str = Field(default="UZS")
    x_axis_label: Optional[str] = None
    y_axis_label: Optional[str] = None
    colors: List[str] = Field(default_factory=list)
    
    @field_validator('total')
    @classmethod
    def validate_total(cls, v):
        if isinstance(v, Decimal):
            return float(v)
        return v


class SalesAnalyticsSchema(BaseModel):
    """Sales analytics schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    period: str
    total_revenue: Decimal
    total_orders: int
    average_order_value: Decimal
    new_customers: int
    returning_customers: int
    conversion_rate: float
    
    # Growth metrics
    revenue_growth: float = Field(default=0.0)
    orders_growth: float = Field(default=0.0)
    aov_growth: float = Field(default=0.0)
    customer_growth: float = Field(default=0.0)
    
    # Top products
    top_products: List[Dict[str, Any]] = Field(default_factory=list)
    
    # Revenue breakdown
    revenue_by_category: List[ChartDataPointSchema] = Field(default_factory=list)
    revenue_trend: List[ChartDataPointSchema] = Field(default_factory=list)
    orders_trend: List[ChartDataPointSchema] = Field(default_factory=list)
    
    @field_validator('total_revenue', 'average_order_value')
    @classmethod
    def validate_amounts(cls, v):
        return float(v)


class CustomerAnalyticsSchema(BaseModel):
    """Customer analytics schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    period: str
    total_customers: int
    new_customers: int
    active_customers: int
    customer_retention_rate: float
    customer_lifetime_value: Decimal
    churn_rate: float
    
    # Customer segments
    customer_segments: List[Dict[str, Any]] = Field(default_factory=list)
    
    # Demographics
    age_distribution: List[ChartDataPointSchema] = Field(default_factory=list)
    gender_distribution: List[ChartDataPointSchema] = Field(default_factory=list)
    location_distribution: List[ChartDataPointSchema] = Field(default_factory=list)
    
    # Acquisition channels
    acquisition_channels: List[ChartDataPointSchema] = Field(default_factory=list)
    
    # Cohort analysis
    cohort_data: List[Dict[str, Any]] = Field(default_factory=list)
    
    @field_validator('customer_lifetime_value')
    @classmethod
    def validate_clv(cls, v):
        return float(v)


class ProductAnalyticsSchema(BaseModel):
    """Product analytics schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    period: str
    total_products: int
    active_products: int
    total_views: int
    total_sales: int
    
    # Top performers
    best_selling_products: List[Dict[str, Any]] = Field(default_factory=list)
    most_viewed_products: List[Dict[str, Any]] = Field(default_factory=list)
    top_rated_products: List[Dict[str, Any]] = Field(default_factory=list)
    
    # Category performance
    category_performance: List[ChartDataPointSchema] = Field(default_factory=list)
    
    # Product trends
    views_trend: List[ChartDataPointSchema] = Field(default_factory=list)
    sales_trend: List[ChartDataPointSchema] = Field(default_factory=list)
    
    # Inventory alerts
    low_stock_products: List[Dict[str, Any]] = Field(default_factory=list)
    out_of_stock_products: List[Dict[str, Any]] = Field(default_factory=list)


class DeliveryAnalyticsSchema(BaseModel):
    """Delivery analytics schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    period: str
    total_deliveries: int
    successful_deliveries: int
    failed_deliveries: int
    pending_deliveries: int
    average_delivery_time: float  # in minutes
    on_time_delivery_rate: float
    
    # Performance metrics
    delivery_success_rate: float
    customer_satisfaction: float
    
    # Delivery trends
    deliveries_trend: List[ChartDataPointSchema] = Field(default_factory=list)
    success_rate_trend: List[ChartDataPointSchema] = Field(default_factory=list)
    
    # Geographic distribution
    deliveries_by_location: List[ChartDataPointSchema] = Field(default_factory=list)
    
    # Time slot analysis
    time_slot_utilization: List[ChartDataPointSchema] = Field(default_factory=list)
    
    # Driver performance
    top_drivers: List[Dict[str, Any]] = Field(default_factory=list)


class RevenueAnalyticsSchema(BaseModel):
    """Revenue analytics schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    period: str
    total_revenue: Decimal
    recurring_revenue: Decimal
    one_time_revenue: Decimal
    refunds: Decimal
    net_revenue: Decimal
    
    # Growth metrics
    revenue_growth_rate: float
    mrr_growth_rate: float  # Monthly Recurring Revenue
    
    # Revenue streams
    revenue_by_source: List[ChartDataPointSchema] = Field(default_factory=list)
    revenue_by_product: List[ChartDataPointSchema] = Field(default_factory=list)
    revenue_by_customer_segment: List[ChartDataPointSchema] = Field(default_factory=list)
    
    # Forecasting
    revenue_forecast: List[ChartDataPointSchema] = Field(default_factory=list)
    
    # Financial ratios
    gross_margin: float
    profit_margin: float
    
    @field_validator('total_revenue', 'recurring_revenue', 'one_time_revenue', 'refunds', 'net_revenue')
    @classmethod
    def validate_amounts(cls, v):
        return float(v)


class ConversionFunnelSchema(BaseModel):
    """Conversion funnel schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    funnel_name: str
    period: str
    stages: List[Dict[str, Any]] = Field(default_factory=list)  # stage_name, count, conversion_rate
    overall_conversion_rate: float
    total_visitors: int
    total_conversions: int
    
    # Dropoff analysis
    biggest_dropoff_stage: Optional[str] = None
    biggest_dropoff_rate: Optional[float] = None
    
    # Improvement suggestions
    optimization_opportunities: List[str] = Field(default_factory=list)


class CohortAnalysisSchema(BaseModel):
    """Cohort analysis schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    cohort_type: str  # monthly, weekly
    metric: str  # retention, revenue
    period: str
    cohorts: List[Dict[str, Any]] = Field(default_factory=list)
    average_retention: List[float] = Field(default_factory=list)
    
    # Key insights
    best_performing_cohort: Optional[Dict[str, Any]] = None
    worst_performing_cohort: Optional[Dict[str, Any]] = None
    retention_trend: str = Field(default="stable")  # improving, declining, stable


class RealTimeMetricsSchema(BaseModel):
    """Real-time metrics schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    current_visitors: int
    active_sessions: int
    orders_today: int
    revenue_today: Decimal
    conversion_rate_today: float
    
    # Live activity
    recent_orders: List[Dict[str, Any]] = Field(default_factory=list)
    popular_pages: List[Dict[str, Any]] = Field(default_factory=list)
    active_promotions: List[Dict[str, Any]] = Field(default_factory=list)
    
    # System status
    system_health: Dict[str, Any] = Field(default_factory=dict)
    
    # Timestamp
    last_updated: datetime
    
    @field_validator('revenue_today')
    @classmethod
    def validate_revenue(cls, v):
        return float(v)


class PredictiveAnalyticsSchema(BaseModel):
    """Predictive analytics schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    model_name: str
    prediction_type: str  # revenue, demand, churn, etc.
    confidence_level: float
    time_horizon: str  # next_week, next_month, next_quarter
    
    # Predictions
    predictions: List[Dict[str, Any]] = Field(default_factory=list)
    
    # Model performance
    model_accuracy: float
    last_trained: datetime
    
    # Key insights
    insights: List[str] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)


class AnalyticsDashboardSchema(BaseModel):
    """Main analytics dashboard schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    period: str
    generated_at: datetime
    
    # Key metrics
    key_metrics: List[DashboardMetricSchema] = Field(default_factory=list)
    
    # Charts
    charts: List[ChartSchema] = Field(default_factory=list)
    
    # Detailed analytics
    sales_analytics: Optional[SalesAnalyticsSchema] = None
    customer_analytics: Optional[CustomerAnalyticsSchema] = None
    product_analytics: Optional[ProductAnalyticsSchema] = None
    delivery_analytics: Optional[DeliveryAnalyticsSchema] = None
    
    # Real-time data
    real_time_metrics: Optional[RealTimeMetricsSchema] = None


class CreateReportRequest(BaseModel):
    """Create analytics report request"""
    report_name: str = Field(..., min_length=3, max_length=100)
    report_type: str = Field(..., pattern=r'^(sales|customers|products|deliveries|revenue|custom)$')
    time_range: TimeRange
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    filters: Dict[str, Any] = Field(default_factory=dict)
    metrics: List[str] = Field(default_factory=list)
    format_type: str = Field(default="pdf", pattern=r'^(pdf|excel|csv)$')
    schedule_frequency: Optional[str] = Field(None, pattern=r'^(daily|weekly|monthly)$')
    recipients: List[str] = Field(default_factory=list)  # email addresses


class AnalyticsQueryRequest(BaseModel):
    """Analytics query request"""
    metric_type: MetricType
    time_range: TimeRange
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    granularity: str = Field(default="day", pattern=r'^(hour|day|week|month)$')
    filters: Dict[str, Any] = Field(default_factory=dict)
    group_by: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=1000)


class AnalyticsResponseSchema(BaseModel):
    """Standard analytics response schema"""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    report_id: Optional[str] = None
    errors: Optional[List[str]] = None


# Export all schemas for easy importing
__all__ = [
    'AnalyticsDashboardSchema',
    'SalesAnalyticsSchema',
    'CustomerAnalyticsSchema',
    'ProductAnalyticsSchema',
    'DeliveryAnalyticsSchema',
    'RevenueAnalyticsSchema',
    'ConversionFunnelSchema',
    'CohortAnalysisSchema',
    'RealTimeMetricsSchema',
    'PredictiveAnalyticsSchema',
    'ChartSchema',
    'DashboardMetricSchema',
    'CreateReportRequest',
    'AnalyticsQueryRequest',
    'AnalyticsResponseSchema',
    'MetricType',
    'TimeRange',
    'ChartType'
]


def serialize_dashboard_metrics(metrics_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Serialize dashboard metrics data
    
    Args:
        metrics_data: Dictionary containing various metrics
        
    Returns:
        List of serialized dashboard metrics
    """
    metrics = []
    
    # Revenue metric
    if 'revenue' in metrics_data:
        revenue = metrics_data['revenue']
        revenue_metric = DashboardMetricSchema(
            name="Total Revenue",
            value=revenue.get('current', 0),
            previous_value=revenue.get('previous', 0),
            format_type="currency",
            icon="dollar-sign",
            color="green",
            description="Total revenue for the selected period"
        )
        
        # Calculate change percentage
        if revenue_metric.previous_value and revenue_metric.previous_value > 0:
            change = ((revenue_metric.value - revenue_metric.previous_value) / revenue_metric.previous_value) * 100
            revenue_metric.change_percentage = round(change, 2)
            revenue_metric.change_direction = "up" if change > 0 else "down" if change < 0 else "neutral"
        
        metrics.append(revenue_metric.model_dump())
    
    # Orders metric
    if 'orders' in metrics_data:
        orders = metrics_data['orders']
        orders_metric = DashboardMetricSchema(
            name="Total Orders",
            value=orders.get('current', 0),
            previous_value=orders.get('previous', 0),
            format_type="number",
            icon="shopping-cart",
            color="blue",
            description="Total number of orders"
        )
        
        if orders_metric.previous_value and orders_metric.previous_value > 0:
            change = ((orders_metric.value - orders_metric.previous_value) / orders_metric.previous_value) * 100
            orders_metric.change_percentage = round(change, 2)
            orders_metric.change_direction = "up" if change > 0 else "down" if change < 0 else "neutral"
        
        metrics.append(orders_metric.model_dump())
    
    # Customers metric
    if 'customers' in metrics_data:
        customers = metrics_data['customers']
        customers_metric = DashboardMetricSchema(
            name="New Customers",
            value=customers.get('current', 0),
            previous_value=customers.get('previous', 0),
            format_type="number",
            icon="users",
            color="purple",
            description="New customers acquired"
        )
        
        if customers_metric.previous_value and customers_metric.previous_value > 0:
            change = ((customers_metric.value - customers_metric.previous_value) / customers_metric.previous_value) * 100
            customers_metric.change_percentage = round(change, 2)
            customers_metric.change_direction = "up" if change > 0 else "down" if change < 0 else "neutral"
        
        metrics.append(customers_metric.model_dump())
    
    # Conversion rate metric
    if 'conversion_rate' in metrics_data:
        conversion = metrics_data['conversion_rate']
        conversion_metric = DashboardMetricSchema(
            name="Conversion Rate",
            value=conversion.get('current', 0),
            previous_value=conversion.get('previous', 0),
            format_type="percentage",
            icon="trending-up",
            color="orange",
            description="Visitor to customer conversion rate"
        )
        
        if conversion_metric.previous_value is not None:
            change = conversion_metric.value - conversion_metric.previous_value
            conversion_metric.change_percentage = round(change, 2)
            conversion_metric.change_direction = "up" if change > 0 else "down" if change < 0 else "neutral"
        
        metrics.append(conversion_metric.model_dump())
    
    return metrics


def serialize_chart_data(chart_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Serialize chart data
    
    Args:
        chart_config: Chart configuration and data
        
    Returns:
        Serialized chart data
    """
    chart_data = []
    
    for point in chart_config.get('data', []):
        chart_point = ChartDataPointSchema(
            label=point.get('label', ''),
            value=point.get('value', 0),
            date=point.get('date'),
            category=point.get('category'),
            color=point.get('color')
        )
        chart_data.append(chart_point)
    
    chart = ChartSchema(
        title=chart_config.get('title', ''),
        chart_type=chart_config.get('chart_type', 'line'),
        data=chart_data,
        total=chart_config.get('total'),
        format_type=chart_config.get('format_type', 'number'),
        currency=chart_config.get('currency', 'UZS'),
        x_axis_label=chart_config.get('x_axis_label'),
        y_axis_label=chart_config.get('y_axis_label'),
        colors=chart_config.get('colors', [])
    )
    
    return chart.model_dump()


def generate_sales_analytics(data: Dict[str, Any], period: str) -> Dict[str, Any]:
    """
    Generate sales analytics
    
    Args:
        data: Raw sales data
        period: Time period for analytics
        
    Returns:
        Serialized sales analytics
    """
    analytics = SalesAnalyticsSchema(
        period=period,
        total_revenue=data.get('total_revenue', 0),
        total_orders=data.get('total_orders', 0),
        average_order_value=data.get('average_order_value', 0),
        new_customers=data.get('new_customers', 0),
        returning_customers=data.get('returning_customers', 0),
        conversion_rate=data.get('conversion_rate', 0.0),
        revenue_growth=data.get('revenue_growth', 0.0),
        orders_growth=data.get('orders_growth', 0.0),
        aov_growth=data.get('aov_growth', 0.0),
        customer_growth=data.get('customer_growth', 0.0),
        top_products=data.get('top_products', []),
        revenue_by_category=[],
        revenue_trend=[],
        orders_trend=[]
    )
    
    # Generate revenue by category chart data
    if 'revenue_by_category' in data:
        for category_data in data['revenue_by_category']:
            chart_point = ChartDataPointSchema(
                label=category_data['category'],
                value=category_data['revenue'],
                category=category_data['category']
            )
            analytics.revenue_by_category.append(chart_point)
    
    # Generate trend data
    if 'revenue_trend' in data:
        for trend_point in data['revenue_trend']:
            chart_point = ChartDataPointSchema(
                label=trend_point['date'],
                value=trend_point['revenue'],
                date=trend_point['date']
            )
            analytics.revenue_trend.append(chart_point)
    
    if 'orders_trend' in data:
        for trend_point in data['orders_trend']:
            chart_point = ChartDataPointSchema(
                label=trend_point['date'],
                value=trend_point['orders'],
                date=trend_point['date']
            )
            analytics.orders_trend.append(chart_point)
    
    return analytics.model_dump()


def generate_customer_analytics(data: Dict[str, Any], period: str) -> Dict[str, Any]:
    """
    Generate customer analytics
    
    Args:
        data: Raw customer data
        period: Time period for analytics
        
    Returns:
        Serialized customer analytics
    """
    analytics = CustomerAnalyticsSchema(
        period=period,
        total_customers=data.get('total_customers', 0),
        new_customers=data.get('new_customers', 0),
        active_customers=data.get('active_customers', 0),
        customer_retention_rate=data.get('customer_retention_rate', 0.0),
        customer_lifetime_value=data.get('customer_lifetime_value', 0),
        churn_rate=data.get('churn_rate', 0.0),
        customer_segments=data.get('customer_segments', []),
        age_distribution=[],
        gender_distribution=[],
        location_distribution=[],
        acquisition_channels=[],
        cohort_data=data.get('cohort_data', [])
    )
    
    # Generate demographic distributions
    demographics = ['age_distribution', 'gender_distribution', 'location_distribution', 'acquisition_channels']
    
    for demo_type in demographics:
        if demo_type in data:
            chart_data = []
            for demo_data in data[demo_type]:
                chart_point = ChartDataPointSchema(
                    label=demo_data['label'],
                    value=demo_data['count'],
                    category=demo_data.get('category', demo_data['label'])
                )
                chart_data.append(chart_point)
            setattr(analytics, demo_type, chart_data)
    
    return analytics.model_dump()


def calculate_conversion_funnel(funnel_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calculate conversion funnel analytics
    
    Args:
        funnel_data: Raw funnel data with stages and counts
        
    Returns:
        Serialized conversion funnel analytics
    """
    stages = funnel_data.get('stages', [])
    total_visitors = stages[0]['count'] if stages else 0
    total_conversions = stages[-1]['count'] if stages else 0
    
    # Calculate conversion rates for each stage
    processed_stages = []
    previous_count = total_visitors
    
    biggest_dropoff_rate = 0
    biggest_dropoff_stage = None
    
    for i, stage in enumerate(stages):
        if i == 0:
            conversion_rate = 100.0
        else:
            conversion_rate = (stage['count'] / previous_count) * 100 if previous_count > 0 else 0
            dropoff_rate = 100 - conversion_rate
            
            if dropoff_rate > biggest_dropoff_rate:
                biggest_dropoff_rate = dropoff_rate
                biggest_dropoff_stage = stage['name']
        
        processed_stages.append({
            'stage_name': stage['name'],
            'count': stage['count'],
            'conversion_rate': round(conversion_rate, 2)
        })
        previous_count = stage['count']
    
    overall_conversion_rate = (total_conversions / total_visitors) * 100 if total_visitors > 0 else 0
    
    funnel = ConversionFunnelSchema(
        funnel_name=funnel_data.get('funnel_name', 'Default Funnel'),
        period=funnel_data.get('period', ''),
        stages=processed_stages,
        overall_conversion_rate=round(overall_conversion_rate, 2),
        total_visitors=total_visitors,
        total_conversions=total_conversions,
        biggest_dropoff_stage=biggest_dropoff_stage,
        biggest_dropoff_rate=round(biggest_dropoff_rate, 2) if biggest_dropoff_rate > 0 else None,
        optimization_opportunities=funnel_data.get('optimization_opportunities', [])
    )
    
    return funnel.model_dump()


def get_real_time_metrics() -> Dict[str, Any]:
    """
    Get real-time metrics
    
    Returns:
        Real-time metrics data
    """
    # This would typically fetch real-time data from the database and cache
    # For now, return placeholder data
    metrics = RealTimeMetricsSchema(
        current_visitors=0,
        active_sessions=0,
        orders_today=0,
        revenue_today=0,
        conversion_rate_today=0.0,
        recent_orders=[],
        popular_pages=[],
        active_promotions=[],
        system_health={'status': 'healthy', 'uptime': '99.9%'},
        last_updated=datetime.now()
    )
    
    return metrics.model_dump()


def generate_predictive_analytics(model_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate predictive analytics
    
    Args:
        model_data: ML model predictions and metadata
        
    Returns:
        Serialized predictive analytics
    """
    analytics = PredictiveAnalyticsSchema(
        model_name=model_data.get('model_name', 'Default Model'),
        prediction_type=model_data.get('prediction_type', 'revenue'),
        confidence_level=model_data.get('confidence_level', 0.85),
        time_horizon=model_data.get('time_horizon', 'next_month'),
        predictions=model_data.get('predictions', []),
        model_accuracy=model_data.get('model_accuracy', 0.0),
        last_trained=model_data.get('last_trained', datetime.now()),
        insights=model_data.get('insights', []),
        recommendations=model_data.get('recommendations', [])
    )
    
    return analytics.model_dump()


def serialize_user_segment(segment) -> Dict[str, Any]:
    """Serialize user segment data"""
    try:
        return {
            'id': segment.id,
            'name': segment.name,
            'description': segment.description,
            'criteria': segment.criteria or {},
            'user_count': getattr(segment, 'user_count', 0),
            'is_active': segment.is_active,
            'created_at': segment.created_at.isoformat() if hasattr(segment, 'created_at') and segment.created_at else None
        }
    except Exception:
        return {
            'id': segment.id,
            'name': segment.name,
            'description': segment.description,
            'is_active': getattr(segment, 'is_active', True)
        }