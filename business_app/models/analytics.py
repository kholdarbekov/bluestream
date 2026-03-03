from datetime import datetime, UTC
from decimal import Decimal
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, JSON, ForeignKey, Numeric, Index
from sqlalchemy.orm import relationship
from business_app import db
from business_app.utils.constants import OrderStatus
from business_app.models import TimestampMixin
from business_app.models.translatable import TranslatableMixin, translatable


@translatable('name', 'description')
class CustomerSegment(db.Model, TimestampMixin, TranslatableMixin):
    __tablename__ = 'customer_segments'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)        # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)         # Default/fallback description (Uzbek)
    
    # Segment criteria
    criteria = Column(JSON, nullable=False)  # Complex rules for segmentation
    
    # Segment stats
    customer_count = Column(Integer, default=0)
    last_updated = Column(DateTime(timezone=True), default=datetime.now(UTC))
    
    # Automated actions
    auto_apply_discount = Column(Boolean, default=False)
    discount_percentage = Column(Float, default=0.0)
    auto_loyalty_multiplier = Column(Float, default=1.0)
    
    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)
        
        # Add segment-specific fields
        result.update({
            'criteria': self.criteria,
            'customer_count': self.customer_count,
            'auto_apply_discount': self.auto_apply_discount,
            'discount_percentage': self.discount_percentage,
            'auto_loyalty_multiplier': self.auto_loyalty_multiplier,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None
        })
        
        return result

@translatable('name', 'description')
class PromotionalCampaign(db.Model, TimestampMixin, TranslatableMixin):
    __tablename__ = 'promotional_campaigns'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)        # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)         # Default/fallback description (Uzbek)
    
    # Campaign type
    campaign_type = Column(String(50), nullable=False)  # discount, loyalty_bonus, free_delivery
    
    # Target audience
    target_segments = Column(JSON, default=[])  # List of customer segment IDs
    target_all_customers = Column(Boolean, default=False)
    target_new_customers = Column(Boolean, default=False)
    target_vip_customers = Column(Boolean, default=False)
    
    # Campaign rules
    discount_type = Column(String(20), nullable=True)  # percentage, fixed, buy_x_get_y
    discount_value = Column(Numeric(precision=10, scale=2), nullable=True)
    min_order_value = Column(Numeric(precision=10, scale=2), nullable=True)
    max_discount_amount = Column(Numeric(precision=10, scale=2), nullable=True)
    
    # Validity
    is_active = Column(Boolean, default=True)
    start_date = Column(DateTime(timezone=True), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=True)
    usage_limit = Column(Integer, nullable=True)  # Total usage limit
    usage_limit_per_customer = Column(Integer, default=1)
    
    # Tracking
    total_uses = Column(Integer, default=0)
    total_discount_given = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    total_revenue_generated = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    
    # Promo code
    promo_code = Column(String(50), unique=True, nullable=True, index=True)
    
    def is_valid(self):
        """Check if campaign is currently valid"""
        now = datetime.now(UTC)
        if not self.is_active:
            return False
        if now < self.start_date:
            return False
        if self.end_date and now > self.end_date:
            return False
        if self.usage_limit and self.total_uses >= self.usage_limit:
            return False
        return True
    
    def can_be_used_by_customer(self, user_id):
        """Check if customer can use this campaign"""
        if not self.is_valid():
            return False
        
        # Check usage limit per customer
        customer_usage = CampaignUsage.query.filter_by(
            campaign_id=self.id,
            user_id=user_id
        ).count()
        
        return customer_usage < self.usage_limit_per_customer
    
    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)
        
        # Add campaign-specific fields
        result.update({
            'campaign_type': self.campaign_type,
            'discount_type': self.discount_type,
            'discount_value': float(self.discount_value) if self.discount_value else None,
            'min_order_value': float(self.min_order_value) if self.min_order_value else None,
            'promo_code': self.promo_code,
            'is_active': self.is_active,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'usage_limit': self.usage_limit,
            'total_uses': self.total_uses,
            'is_valid': self.is_valid()
        })
        
        return result


class CampaignUsage(db.Model, TimestampMixin):
    """Track campaign usage by customers"""
    __tablename__ = 'campaign_usage'
    __table_args__ = (
        Index('idx_campaign_usage_campaign_user', 'campaign_id', 'user_id'),
        Index('idx_campaign_usage_order_id', 'order_id'),
    )
    
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey('promotional_campaigns.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)

    campaign = relationship('PromotionalCampaign', backref='usage_records')
    user = relationship('User')
    order = relationship('Order')
    
    def __repr__(self):
        return f'<CampaignUsage {self.campaign_id}:{self.user_id}>'


class AnalyticsReport(db.Model, TimestampMixin):
    """Store generated analytics reports"""
    __tablename__ = 'analytics_reports'
    
    id = Column(Integer, primary_key=True)
    report_type = Column(String(50), nullable=False)  # daily, weekly, monthly, etc.
    title = Column(String(200), nullable=False)
    
    # Report metadata
    start_date = Column(DateTime(timezone=True), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=False)
    generated_by = Column(Integer, ForeignKey('users.id'), nullable=True)  # User ID who generated
    
    # Report data
    report_data = Column(JSON, nullable=False)
    
    # Status
    status = Column(String(20), default='generated')  # generated, archived, deleted
    is_public = Column(Boolean, default=False)
    
    def to_dict(self):
        return {
            'id': self.id,
            'report_type': self.report_type,
            'title': self.title,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'status': self.status,
            'is_public': self.is_public,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class UserBehavior(db.Model, TimestampMixin):
    """Track user behavior for analytics"""
    __tablename__ = 'user_behavior'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    session_id = Column(String(100), nullable=True)
    
    # Action details
    action = Column(String(100), nullable=False)  # page_view, product_view, add_to_cart, etc.
    page_url = Column(String(500), nullable=True)
    referrer_url = Column(String(500), nullable=True)
    
    # Technical details
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    device_type = Column(String(50), nullable=True)  # mobile, tablet, desktop
    browser = Column(String(100), nullable=True)
    
    # Additional metadata
    extra_data = Column(JSON, default={})
    timestamp = Column(DateTime(timezone=True), default=datetime.now(UTC), nullable=False)
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'action': self.action,
            'page_url': self.page_url,
            'device_type': self.device_type,
            'browser': self.browser,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'extra_data': self.extra_data
        }


class SalesMetric(db.Model, TimestampMixin):
    """Store calculated sales metrics"""
    __tablename__ = 'sales_metrics'
    
    id = Column(Integer, primary_key=True)
    metric_name = Column(String(100), nullable=False)
    metric_type = Column(String(50), nullable=False)  # daily, weekly, monthly
    
    # Time period
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)

    # Metric values
    value = Column(Numeric(precision=10, scale=2), nullable=False)
    target_value = Column(Numeric(precision=10, scale=2), nullable=True)
    previous_value = Column(Numeric(precision=10, scale=2), nullable=True)
    
    # Additional context
    unit = Column(String(20), nullable=True)  # UZS, orders, percentage, etc.
    category = Column(String(50), nullable=True)  # revenue, orders, customers, etc.
    extra_data = Column(JSON, default={})
    
    def to_dict(self):
        return {
            'id': self.id,
            'metric_name': self.metric_name,
            'metric_type': self.metric_type,
            'period_start': self.period_start.isoformat() if self.period_start else None,
            'period_end': self.period_end.isoformat() if self.period_end else None,
            'value': self.value,
            'target_value': self.target_value,
            'previous_value': self.previous_value,
            'unit': self.unit,
            'category': self.category,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class UserEvent(db.Model, TimestampMixin):
    """Track user events and activities"""
    __tablename__ = 'user_events'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)  # Null for anonymous users
    session_id = Column(String(100), nullable=False, index=True)
    
    # Event details
    event_type = Column(String(50), nullable=False, index=True)  # page_view, click, purchase, etc.
    event_name = Column(String(100), nullable=False)
    event_category = Column(String(50), nullable=True)
    
    # Context
    page_url = Column(String(500), nullable=True)
    referrer = Column(String(500), nullable=True)
    user_agent = Column(String(500), nullable=True)
    ip_address = Column(String(45), nullable=True)
    
    # Additional data
    event_data = Column(JSON, default={})
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'session_id': self.session_id,
            'event_type': self.event_type,
            'event_name': self.event_name,
            'event_category': self.event_category,
            'page_url': self.page_url,
            'referrer': self.referrer,
            'event_data': self.event_data,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class ProductView(db.Model, TimestampMixin):
    """Track product page views"""
    __tablename__ = 'product_views'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    session_id = Column(String(100), nullable=False, index=True)
    
    # View details
    view_duration = Column(Integer, default=0)  # seconds
    referrer_source = Column(String(100), nullable=True)
    device_type = Column(String(20), nullable=True)  # mobile, desktop, tablet
    
    # Relationships
    user = relationship('User')
    product = relationship('Product')
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'product_id': self.product_id,
            'session_id': self.session_id,
            'view_duration': self.view_duration,
            'referrer_source': self.referrer_source,
            'device_type': self.device_type,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class SearchQuery(db.Model, TimestampMixin):
    """Track search queries and results"""
    __tablename__ = 'search_queries'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    session_id = Column(String(100), nullable=False, index=True)
    
    # Search details
    query_text = Column(String(255), nullable=False, index=True)
    results_count = Column(Integer, default=0)
    filters_applied = Column(JSON, default={})
    
    # User interaction
    clicked_result_position = Column(Integer, nullable=True)
    clicked_product_id = Column(Integer, ForeignKey('products.id'), nullable=True)
    
    # Relationships
    user = relationship('User')
    clicked_product = relationship('Product')
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'session_id': self.session_id,
            'query_text': self.query_text,
            'results_count': self.results_count,
            'filters_applied': self.filters_applied,
            'clicked_result_position': self.clicked_result_position,
            'clicked_product_id': self.clicked_product_id,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class ConversionEvent(db.Model, TimestampMixin):
    """Track conversion events and funnel stages"""
    __tablename__ = 'conversion_events'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    session_id = Column(String(100), nullable=False, index=True)
    
    # Conversion details
    event_type = Column(String(50), nullable=False, index=True)  # signup, purchase, subscription, etc.
    funnel_stage = Column(String(50), nullable=False)  # awareness, consideration, conversion
    conversion_value = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    
    # Associated entities
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=True)
    
    # Attribution
    source = Column(String(100), nullable=True)  # organic, paid, referral, etc.
    medium = Column(String(100), nullable=True)  # search, social, email, etc.
    campaign = Column(String(100), nullable=True)
    
    # Relationships
    user = relationship('User')
    order = relationship('Order')
    product = relationship('Product')
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'session_id': self.session_id,
            'event_type': self.event_type,
            'funnel_stage': self.funnel_stage,
            'conversion_value': self.conversion_value,
            'order_id': self.order_id,
            'product_id': self.product_id,
            'source': self.source,
            'medium': self.medium,
            'campaign': self.campaign,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class RevenueMetric(db.Model, TimestampMixin):
    """Store revenue metrics and KPIs"""
    __tablename__ = 'revenue_metrics'
    
    id = Column(Integer, primary_key=True)
    
    # Time period
    period_start = Column(DateTime(timezone=True), nullable=False, index=True)
    period_end = Column(DateTime(timezone=True), nullable=False, index=True)
    period_type = Column(String(20), nullable=False)  # daily, weekly, monthly, yearly
    
    # Revenue metrics
    gross_revenue = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    net_revenue = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    recurring_revenue = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    average_order_value = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    
    # Order metrics
    total_orders = Column(Integer, default=0)
    new_customer_orders = Column(Integer, default=0)
    repeat_customer_orders = Column(Integer, default=0)
    
    # Customer metrics
    new_customers = Column(Integer, default=0)
    active_customers = Column(Integer, default=0)
    churned_customers = Column(Integer, default=0)
    
    def to_dict(self):
        return {
            'id': self.id,
            'period_start': self.period_start.isoformat() if self.period_start else None,
            'period_end': self.period_end.isoformat() if self.period_end else None,
            'period_type': self.period_type,
            'gross_revenue': self.gross_revenue,
            'net_revenue': self.net_revenue,
            'recurring_revenue': self.recurring_revenue,
            'average_order_value': self.average_order_value,
            'total_orders': self.total_orders,
            'new_customer_orders': self.new_customer_orders,
            'repeat_customer_orders': self.repeat_customer_orders,
            'new_customers': self.new_customers,
            'active_customers': self.active_customers,
            'churned_customers': self.churned_customers,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


@translatable('name', 'description')
class UserSegment(db.Model, TimestampMixin, TranslatableMixin):
    """User segments for targeted analytics and marketing"""
    __tablename__ = 'user_segments'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)        # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)         # Default/fallback description (Uzbek)
    
    # Segment criteria (stored as JSON)
    criteria = Column(JSON, nullable=False)
    
    # Segment statistics
    user_count = Column(Integer, default=0)
    last_calculated = Column(DateTime(timezone=True), nullable=True)
    
    # Status
    is_active = Column(Boolean, default=True)
    auto_update = Column(Boolean, default=True)
    
    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)
        
        # Add segment-specific fields
        result.update({
            'criteria': self.criteria,
            'user_count': self.user_count,
            'last_calculated': self.last_calculated.isoformat() if self.last_calculated else None,
            'is_active': self.is_active,
            'auto_update': self.auto_update
        })
        
        return result
