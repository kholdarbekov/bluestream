"""
Analytics service for the Water Business Platform
Provides business intelligence, reporting, and predictive analytics
"""
from datetime import datetime, timedelta, UTC
from typing import Dict, Any, List, Optional, Tuple
from flask import current_app
from sqlalchemy import func, and_, or_, text, case
import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler

from business_app.models.user import User
from business_app.models.order import Order
from business_app.models.delivery import Delivery
from business_app.models.subscription import Subscription
from business_app.models.product import Product
from business_app.models.payment import Payment
from business_app.models.analytics import AnalyticsReport, UserBehavior, SalesMetric
from business_app.models.order import OrderItem
from business_app.utils.constants import OrderStatus, DeliveryStatus, SubscriptionStatus
from business_app import db


class AnalyticsService:
    """Service for analytics and business intelligence"""
    
    def __init__(self):
        self.default_period_days = 30
    
    def get_dashboard_overview(self, start_date: datetime = None, 
                             end_date: datetime = None) -> Dict[str, Any]:
        """Get high-level dashboard overview metrics"""
        if not start_date:
            start_date = datetime.now(UTC) - timedelta(days=self.default_period_days)
        if not end_date:
            end_date = datetime.now(UTC)
        
        # Revenue metrics
        revenue_data = self._get_revenue_metrics(start_date, end_date)
        
        # Order metrics
        order_data = self._get_order_metrics(start_date, end_date)
        
        # Customer metrics
        customer_data = self._get_customer_metrics(start_date, end_date)
        
        # Delivery metrics
        delivery_data = self._get_delivery_metrics(start_date, end_date)
        
        # Growth trends
        growth_data = self._get_growth_trends(start_date, end_date)
        
        return {
            'period': {
                'start_date': start_date.isoformat(),
                'end_date': end_date.isoformat()
            },
            'revenue': revenue_data,
            'orders': order_data,
            'customers': customer_data,
            'delivery': delivery_data,
            'growth': growth_data,
            'generated_at': datetime.now(UTC).isoformat()
        }
    
    def get_sales_analytics(self, start_date: datetime = None,
                           end_date: datetime = None) -> Dict[str, Any]:
        """Get detailed sales analytics"""
        if not start_date:
            start_date = datetime.now(UTC) - timedelta(days=90)
        if not end_date:
            end_date = datetime.now(UTC)
        
        # Daily sales trends
        daily_sales = self._get_daily_sales_trend(start_date, end_date)
        
        # Product performance
        product_performance = self._get_product_performance(start_date, end_date)
        
        # Sales by time periods
        hourly_sales = self._get_hourly_sales_distribution(start_date, end_date)
        weekly_sales = self._get_weekly_sales_distribution(start_date, end_date)
        
        # Geographic distribution
        geographic_sales = self._get_geographic_sales_distribution(start_date, end_date)
        
        # Customer segments
        customer_segments = self._get_customer_segment_analysis(start_date, end_date)
        
        return {
            'daily_trends': daily_sales,
            'product_performance': product_performance,
            'hourly_distribution': hourly_sales,
            'weekly_distribution': weekly_sales,
            'geographic_distribution': geographic_sales,
            'customer_segments': customer_segments
        }
    
    def get_customer_analytics(self, start_date: datetime = None,
                              end_date: datetime = None) -> Dict[str, Any]:
        """Get customer behavior analytics"""
        if not start_date:
            start_date = datetime.now(UTC) - timedelta(days=90)
        if not end_date:
            end_date = datetime.now(UTC)
        
        # Customer acquisition
        acquisition_data = self._get_customer_acquisition_metrics(start_date, end_date)
        
        # Customer retention
        retention_data = self._get_customer_retention_metrics(start_date, end_date)
        
        # Customer lifetime value
        clv_data = self._get_customer_lifetime_value_analysis()
        
        # Churn analysis
        churn_data = self._get_customer_churn_analysis(start_date, end_date)
        
        # Behavioral patterns
        behavior_data = self._get_customer_behavior_patterns(start_date, end_date)
        
        return {
            'acquisition': acquisition_data,
            'retention': retention_data,
            'lifetime_value': clv_data,
            'churn': churn_data,
            'behavior_patterns': behavior_data
        }
    
    def get_delivery_analytics(self, start_date: datetime = None,
                              end_date: datetime = None) -> Dict[str, Any]:
        """Get delivery performance analytics"""
        if not start_date:
            start_date = datetime.now(UTC) - timedelta(days=30)
        if not end_date:
            end_date = datetime.now(UTC)
        
        # Delivery performance metrics
        performance_data = self._get_delivery_performance_metrics(start_date, end_date)
        
        # Route efficiency
        route_data = self._get_route_efficiency_metrics(start_date, end_date)
        
        # Driver performance
        driver_data = self._get_driver_performance_metrics(start_date, end_date)
        
        # Geographic delivery patterns
        geographic_data = self._get_delivery_geographic_patterns(start_date, end_date)
        
        return {
            'performance': performance_data,
            'route_efficiency': route_data,
            'driver_performance': driver_data,
            'geographic_patterns': geographic_data
        }
    
    def predict_demand(self, forecast_days: int = 30) -> Dict[str, Any]:
        """Predict demand for the next period using machine learning"""
        try:
            # Get historical data
            historical_data = self._get_historical_demand_data()
            
            if len(historical_data) < 30:  # Need at least 30 days of data
                return {
                    'error': 'Insufficient historical data for prediction',
                    'min_days_required': 30,
                    'available_days': len(historical_data)
                }
            
            # Prepare data for ML model
            df = pd.DataFrame(historical_data)
            
            # Feature engineering
            df['day_of_week'] = pd.to_datetime(df['date']).dt.dayofweek
            df['day_of_month'] = pd.to_datetime(df['date']).dt.day
            df['month'] = pd.to_datetime(df['date']).dt.month
            df['is_weekend'] = df['day_of_week'].isin([5, 6]).astype(int)
            
            # Prepare features
            features = ['day_of_week', 'day_of_month', 'month', 'is_weekend']
            X = df[features].values
            y = df['order_count'].values
            
            # Train model
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)
            
            model = LinearRegression()
            model.fit(X_scaled, y)
            
            # Generate predictions
            predictions = []
            current_date = datetime.now(UTC).date()
            
            for i in range(forecast_days):
                future_date = current_date + timedelta(days=i+1)
                
                # Create features for future date
                future_features = [
                    future_date.weekday(),
                    future_date.day,
                    future_date.month,
                    1 if future_date.weekday() in [5, 6] else 0
                ]
                
                # Scale features and predict
                future_features_scaled = scaler.transform([future_features])
                predicted_orders = model.predict(future_features_scaled)[0]
                
                predictions.append({
                    'date': future_date.isoformat(),
                    'predicted_orders': max(0, int(round(predicted_orders))),
                    'day_of_week': future_date.strftime('%A'),
                    'is_weekend': future_date.weekday() in [5, 6]
                })
            
            # Calculate model accuracy
            y_pred = model.predict(X_scaled)
            accuracy = 1 - np.mean(np.abs(y - y_pred) / y) if np.mean(y) > 0 else 0
            
            return {
                'predictions': predictions,
                'model_accuracy': round(accuracy * 100, 2),
                'historical_avg': round(np.mean(y), 2),
                'forecast_period_days': forecast_days
            }
            
        except Exception as e:
            current_app.logger.error(f"Demand prediction failed: {e}")
            return {'error': f'Prediction failed: {str(e)}'}
    
    def predict_customer_churn(self, user_id: int = None) -> Dict[str, Any]:
        """Predict customer churn probability with optimized queries"""
        try:
            if user_id:
                # Predict for specific user
                churn_probability = self._calculate_user_churn_probability_optimized(user_id)
                return {
                    'user_id': user_id,
                    'churn_probability': churn_probability,
                    'risk_level': self._get_churn_risk_level(churn_probability)
                }
            else:
                # Predict for all active customers using batch processing
                return self._batch_calculate_churn_predictions()
                
        except Exception as e:
            current_app.logger.error(f"Churn prediction failed: {e}")
            return {'error': f'Churn prediction failed: {str(e)}'}
    
    def generate_business_report(self, report_type: str, start_date: datetime = None,
                               end_date: datetime = None) -> Dict[str, Any]:
        """Generate comprehensive business report"""
        if not start_date:
            start_date = datetime.now(UTC) - timedelta(days=30)
        if not end_date:
            end_date = datetime.now(UTC)
        
        report_generators = {
            'daily': self._generate_daily_report,
            'weekly': self._generate_weekly_report,
            'monthly': self._generate_monthly_report,
            'quarterly': self._generate_quarterly_report,
            'annual': self._generate_annual_report
        }
        
        if report_type not in report_generators:
            raise ValueError(f"Unsupported report type: {report_type}")
        
        return report_generators[report_type](start_date, end_date)
    
    def track_user_behavior(self, user_id: int, action: str, metadata: Dict[str, Any] = None):
        """Track user behavior for analytics"""
        try:
            behavior = UserBehavior(
                user_id=user_id,
                action=action,
                metadata=metadata or {},
                timestamp=datetime.now(UTC),
                ip_address=metadata.get('ip_address') if metadata else None,
                user_agent=metadata.get('user_agent') if metadata else None
            )
            
            db.session.add(behavior)
            db.session.commit()
            
        except Exception as e:
            current_app.logger.error(f"Failed to track user behavior: {e}")
    
    # Private helper methods
    def _get_revenue_metrics(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Get revenue metrics for the period"""
        # Total revenue
        total_revenue = db.session.query(func.sum(Order.total_amount)).filter(
            Order.created_at.between(start_date, end_date),
            Order.status != OrderStatus.CANCELLED
        ).scalar() or 0
        
        # Average order value
        avg_order_value = db.session.query(func.avg(Order.total_amount)).filter(
            Order.created_at.between(start_date, end_date),
            Order.status != OrderStatus.CANCELLED
        ).scalar() or 0
        
        # Revenue growth (compared to previous period)
        previous_start = start_date - (end_date - start_date)
        previous_revenue = db.session.query(func.sum(Order.total_amount)).filter(
            Order.created_at.between(previous_start, start_date),
            Order.status != OrderStatus.CANCELLED
        ).scalar() or 0
        
        growth_rate = ((total_revenue - previous_revenue) / previous_revenue * 100) if previous_revenue > 0 else 0
        
        return {
            'total_revenue': total_revenue,
            'average_order_value': round(avg_order_value, 2),
            'growth_rate': round(growth_rate, 2),
            'previous_period_revenue': previous_revenue
        }
    
    def _get_order_metrics(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Get order metrics for the period"""
        # Total orders
        total_orders = Order.query.filter(
            Order.created_at.between(start_date, end_date)
        ).count()
        
        # Orders by status
        status_breakdown = db.session.query(
            Order.status, func.count(Order.id)
        ).filter(
            Order.created_at.between(start_date, end_date)
        ).group_by(Order.status).all()
        
        # Completion rate
        completed_orders = Order.query.filter(
            Order.created_at.between(start_date, end_date),
            Order.status == OrderStatus.DELIVERED
        ).count()
        
        completion_rate = (completed_orders / total_orders * 100) if total_orders > 0 else 0
        
        return {
            'total_orders': total_orders,
            'completed_orders': completed_orders,
            'completion_rate': round(completion_rate, 2),
            'status_breakdown': {status.value: count for status, count in status_breakdown}
        }
    
    def _get_customer_metrics(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Get customer metrics for the period"""
        # New customers
        new_customers = User.query.filter(
            User.created_at.between(start_date, end_date)
        ).count()
        
        # Active customers (placed at least one order)
        active_customers = db.session.query(func.count(func.distinct(Order.user_id))).filter(
            Order.created_at.between(start_date, end_date)
        ).scalar()
        
        # Repeat customers
        repeat_customers = db.session.query(Order.user_id).filter(
            Order.created_at.between(start_date, end_date)
        ).group_by(Order.user_id).having(func.count(Order.id) > 1).count()
        
        return {
            'new_customers': new_customers,
            'active_customers': active_customers,
            'repeat_customers': repeat_customers,
            'repeat_rate': round((repeat_customers / active_customers * 100) if active_customers > 0 else 0, 2)
        }
    
    def _get_delivery_metrics(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Get delivery metrics for the period"""
        # Total deliveries
        total_deliveries = Delivery.query.filter(
            Delivery.created_at.between(start_date, end_date)
        ).count()
        
        # Successful deliveries
        successful_deliveries = Delivery.query.filter(
            Delivery.created_at.between(start_date, end_date),
            Delivery.status == DeliveryStatus.DELIVERED
        ).count()
        
        # Average delivery time
        avg_delivery_time = db.session.query(
            func.avg(func.extract('epoch', Delivery.delivered_at - Delivery.created_at))
        ).filter(
            Delivery.created_at.between(start_date, end_date),
            Delivery.status == DeliveryStatus.DELIVERED
        ).scalar()
        
        avg_delivery_hours = (avg_delivery_time / 3600) if avg_delivery_time else 0
        
        return {
            'total_deliveries': total_deliveries,
            'successful_deliveries': successful_deliveries,
            'success_rate': round((successful_deliveries / total_deliveries * 100) if total_deliveries > 0 else 0, 2),
            'average_delivery_time_hours': round(avg_delivery_hours, 2)
        }
    
    def _get_growth_trends(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Get growth trend data"""
        # Daily order trends
        daily_orders = db.session.query(
            func.date(Order.created_at).label('date'),
            func.count(Order.id).label('count')
        ).filter(
            Order.created_at.between(start_date, end_date)
        ).group_by(func.date(Order.created_at)).all()
        
        # Daily revenue trends
        daily_revenue = db.session.query(
            func.date(Order.created_at).label('date'),
            func.sum(Order.total_amount).label('revenue')
        ).filter(
            Order.created_at.between(start_date, end_date),
            Order.status != OrderStatus.CANCELLED
        ).group_by(func.date(Order.created_at)).all()
        
        return {
            'daily_orders': [
                {'date': date.isoformat(), 'count': count}
                for date, count in daily_orders
            ],
            'daily_revenue': [
                {'date': date.isoformat(), 'revenue': float(revenue or 0)}
                for date, revenue in daily_revenue
            ]
        }
    
    def _get_historical_demand_data(self) -> List[Dict[str, Any]]:
        """Get historical demand data for ML predictions"""
        # Get daily order counts for the last 90 days
        end_date = datetime.now(UTC).date()
        start_date = end_date - timedelta(days=90)
        
        daily_orders = db.session.query(
            func.date(Order.created_at).label('date'),
            func.count(Order.id).label('order_count'),
            func.sum(Order.total_amount).label('revenue')
        ).filter(
            Order.created_at >= start_date,
            Order.created_at <= end_date
        ).group_by(func.date(Order.created_at)).all()
        
        return [
            {
                'date': date.isoformat(),
                'order_count': count,
                'revenue': float(revenue or 0)
            }
            for date, count, revenue in daily_orders
        ]
    
    def _batch_calculate_churn_predictions(self) -> Dict[str, Any]:
        """Calculate churn predictions for all users in batch to avoid N+1 queries"""
        # Get all active users
        active_users = db.session.query(
            User.id, User.first_name, User.last_name, User.email, User.created_at
        ).filter_by(is_active=True).all()
        
        if not active_users:
            return {'predictions': [], 'high_risk_customers': 0, 'medium_risk_customers': 0}
        
        user_ids = [user.id for user in active_users]
        
        # Batch fetch all user statistics in single queries
        user_stats = self._get_batch_user_statistics(user_ids)
        
        # Calculate overall averages once
        overall_avg_order_value = db.session.query(func.avg(Order.total_amount)).scalar() or 1
        
        churn_predictions = []
        
        for user in active_users:
            stats = user_stats.get(user.id, {})
            probability = self._calculate_churn_from_stats(stats, user.created_at, overall_avg_order_value)
            
            if probability > 0.3:  # Only include medium and high risk
                churn_predictions.append({
                    'user_id': user.id,
                    'user_name': f"{user.first_name} {user.last_name}",
                    'email': user.email,
                    'churn_probability': probability,
                    'risk_level': self._get_churn_risk_level(probability)
                })
        
        # Sort by risk level
        churn_predictions.sort(key=lambda x: x['churn_probability'], reverse=True)
        
        return {
            'high_risk_customers': len([p for p in churn_predictions if p['risk_level'] == 'high']),
            'medium_risk_customers': len([p for p in churn_predictions if p['risk_level'] == 'medium']),
            'predictions': churn_predictions[:50]  # Top 50 at-risk customers
        }
    
    def _get_batch_user_statistics(self, user_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Get user statistics in batch to avoid N+1 queries"""
        # Get last order date for each user
        last_orders_subquery = db.session.query(
            Order.user_id,
            func.max(Order.created_at).label('last_order_date')
        ).filter(Order.user_id.in_(user_ids)).group_by(Order.user_id).subquery()
        
        # Get order statistics for each user
        order_stats = db.session.query(
            Order.user_id,
            func.count(Order.id).label('order_count'),
            func.avg(Order.total_amount).label('avg_order_value')
        ).filter(Order.user_id.in_(user_ids)).group_by(Order.user_id).all()
        
        # Get delivery failure statistics
        delivery_stats = db.session.query(
            Order.user_id,
            func.count(Delivery.id).label('total_deliveries'),
            func.sum(case((Delivery.status == DeliveryStatus.FAILED, 1), else_=0)).label('failed_deliveries')
        ).join(Delivery).filter(Order.user_id.in_(user_ids)).group_by(Order.user_id).all()
        
        # Combine all statistics
        user_stats = {}
        
        # Initialize with last order dates
        last_order_data = db.session.query(last_orders_subquery).all()
        for user_id, last_order_date in last_order_data:
            user_stats[user_id] = {
                'last_order_date': last_order_date,
                'order_count': 0,
                'avg_order_value': 0,
                'total_deliveries': 0,
                'failed_deliveries': 0
            }
        
        # Add order statistics
        for user_id, order_count, avg_order_value in order_stats:
            if user_id in user_stats:
                user_stats[user_id].update({
                    'order_count': order_count,
                    'avg_order_value': avg_order_value or 0
                })
            else:
                user_stats[user_id] = {
                    'last_order_date': None,
                    'order_count': order_count,
                    'avg_order_value': avg_order_value or 0,
                    'total_deliveries': 0,
                    'failed_deliveries': 0
                }
        
        # Add delivery statistics
        for user_id, total_deliveries, failed_deliveries in delivery_stats:
            if user_id in user_stats:
                user_stats[user_id].update({
                    'total_deliveries': total_deliveries or 0,
                    'failed_deliveries': failed_deliveries or 0
                })
        
        return user_stats
    
    def _calculate_churn_from_stats(self, stats: Dict[str, Any], user_created_at: datetime, overall_avg_order_value: float) -> float:
        """Calculate churn probability from pre-fetched statistics"""
        factors = {
            'days_since_last_order': 0,
            'order_frequency': 0,
            'avg_order_value': 0,
            'delivery_issues': 0
        }
        
        # Days since last order
        if stats.get('last_order_date'):
            days_since_last = (datetime.now(UTC) - stats['last_order_date']).days
            factors['days_since_last_order'] = min(days_since_last / 30, 1.0)
        else:
            factors['days_since_last_order'] = 1.0
        
        # Order frequency
        user_age_months = max(1, (datetime.now(UTC) - user_created_at).days / 30)
        order_frequency = stats.get('order_count', 0) / user_age_months
        factors['order_frequency'] = max(0, 1 - (order_frequency / 4))
        
        # Average order value
        avg_order_value = stats.get('avg_order_value', 0)
        factors['avg_order_value'] = max(0, 1 - (avg_order_value / overall_avg_order_value))
        
        # Delivery issues
        total_deliveries = stats.get('total_deliveries', 0)
        failed_deliveries = stats.get('failed_deliveries', 0)
        if total_deliveries > 0:
            failure_rate = failed_deliveries / total_deliveries
            factors['delivery_issues'] = min(failure_rate * 2, 1.0)
        
        # Weighted average of factors
        weights = {
            'days_since_last_order': 0.4,
            'order_frequency': 0.3,
            'avg_order_value': 0.2,
            'delivery_issues': 0.1
        }
        
        probability = sum(factors[factor] * weights[factor] for factor in factors)
        return min(max(probability, 0.0), 1.0)
    
    def _calculate_user_churn_probability_optimized(self, user_id: int) -> float:
        """Calculate churn probability for a specific user with optimized queries"""
        # Get user and statistics in a single optimized query set
        user_stats = self._get_batch_user_statistics([user_id])
        user = User.query.get(user_id)
        
        if not user or user_id not in user_stats:
            return 0.0
        
        overall_avg_order_value = db.session.query(func.avg(Order.total_amount)).scalar() or 1
        return self._calculate_churn_from_stats(user_stats[user_id], user.created_at, overall_avg_order_value)
    
    def _calculate_user_churn_probability(self, user_id: int) -> float:
        """DEPRECATED: Calculate churn probability for a specific user (legacy method)"""
        # Keep for backward compatibility, but use optimized version
        return self._calculate_user_churn_probability_optimized(user_id)
    
    def _get_churn_risk_level(self, probability: float) -> str:
        """Get risk level based on churn probability"""
        if probability >= 0.7:
            return 'high'
        elif probability >= 0.4:
            return 'medium'
        else:
            return 'low'
    
    def _get_daily_sales_trend(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        """Get daily sales trend data"""
        daily_sales = db.session.query(
            func.date(Order.created_at).label('date'),
            func.count(Order.id).label('orders'),
            func.sum(Order.total_amount).label('revenue')
        ).filter(
            Order.created_at.between(start_date, end_date),
            Order.status != OrderStatus.CANCELLED
        ).group_by(func.date(Order.created_at)).order_by('date').all()
        
        return [
            {
                'date': date.isoformat(),
                'orders': orders,
                'revenue': float(revenue or 0)
            }
            for date, orders, revenue in daily_sales
        ]
    
    def _get_product_performance(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        """Get product performance metrics"""
        product_sales = db.session.query(
            Product.id,
            Product.name,
            func.sum(OrderItem.quantity).label('total_quantity'),
            func.sum(OrderItem.total_price).label('total_revenue'),
            func.count(func.distinct(Order.id)).label('order_count')
        ).join(OrderItem).join(Order).filter(
            Order.created_at.between(start_date, end_date),
            Order.status != OrderStatus.CANCELLED
        ).group_by(Product.id, Product.name).order_by(func.sum(OrderItem.total_price).desc()).all()
        
        return [
            {
                'product_id': product_id,
                'product_name': name,
                'quantity_sold': int(quantity),
                'revenue': float(revenue),
                'order_count': orders
            }
            for product_id, name, quantity, revenue, orders in product_sales
        ]
    
    def _get_hourly_sales_distribution(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        """Get sales distribution by hour of day"""
        hourly_sales = db.session.query(
            func.extract('hour', Order.created_at).label('hour'),
            func.count(Order.id).label('orders'),
            func.sum(Order.total_amount).label('revenue')
        ).filter(
            Order.created_at.between(start_date, end_date),
            Order.status != OrderStatus.CANCELLED
        ).group_by(func.extract('hour', Order.created_at)).order_by('hour').all()
        
        return [
            {
                'hour': int(hour),
                'orders': orders,
                'revenue': float(revenue or 0)
            }
            for hour, orders, revenue in hourly_sales
        ]
    
    def _get_weekly_sales_distribution(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        """Get sales distribution by day of week"""
        weekly_sales = db.session.query(
            func.extract('dow', Order.created_at).label('day_of_week'),
            func.count(Order.id).label('orders'),
            func.sum(Order.total_amount).label('revenue')
        ).filter(
            Order.created_at.between(start_date, end_date),
            Order.status != OrderStatus.CANCELLED
        ).group_by(func.extract('dow', Order.created_at)).order_by('day_of_week').all()
        
        day_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
        
        return [
            {
                'day_of_week': int(dow),
                'day_name': day_names[int(dow)],
                'orders': orders,
                'revenue': float(revenue or 0)
            }
            for dow, orders, revenue in weekly_sales
        ]
    
    def _get_geographic_sales_distribution(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        """Get sales distribution by geographic area"""
        geographic_sales = db.session.query(
            Order.delivery_address_city,
            func.count(Order.id).label('orders'),
            func.sum(Order.total_amount).label('revenue')
        ).filter(
            Order.created_at.between(start_date, end_date),
            Order.status != OrderStatus.CANCELLED
        ).group_by(Order.delivery_address_city).order_by(func.sum(Order.total_amount).desc()).all()
        
        return [
            {
                'city': city,
                'orders': orders,
                'revenue': float(revenue or 0)
            }
            for city, orders, revenue in geographic_sales
        ]
    
    def _get_customer_segment_analysis(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Analyze customer segments"""
        # Customer value segments based on total spending
        customer_values = db.session.query(
            Order.user_id,
            func.sum(Order.total_amount).label('total_spent'),
            func.count(Order.id).label('order_count')
        ).filter(
            Order.created_at.between(start_date, end_date),
            Order.status != OrderStatus.CANCELLED
        ).group_by(Order.user_id).subquery()
        
        # Define segments
        segments = {
            'high_value': 0,    # >100,000 UZS
            'medium_value': 0,  # 25,000-100,000 UZS  
            'low_value': 0      # <25,000 UZS
        }
        
        customer_data = db.session.query(customer_values).all()
        
        for customer in customer_data:
            total_spent = customer.total_spent
            if total_spent >= 100000:
                segments['high_value'] += 1
            elif total_spent >= 25000:
                segments['medium_value'] += 1
            else:
                segments['low_value'] += 1
        
        return segments
    
    def _get_customer_acquisition_metrics(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Get customer acquisition metrics"""
        new_customers = User.query.filter(
            User.created_at.between(start_date, end_date)
        ).count()
        
        # Acquisition channels (based on referrals, etc.)
        referred_customers = User.query.filter(
            User.created_at.between(start_date, end_date),
            User.referred_by.isnot(None)
        ).count()
        
        organic_customers = new_customers - referred_customers
        
        return {
            'total_new_customers': new_customers,
            'organic_customers': organic_customers,
            'referred_customers': referred_customers,
            'referral_rate': round((referred_customers / new_customers * 100) if new_customers > 0 else 0, 2)
        }
    
    def _get_customer_retention_metrics(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Get customer retention metrics"""
        # Customers who placed orders in both current and previous period
        previous_start = start_date - (end_date - start_date)
        
        current_customers = set(db.session.query(Order.user_id).filter(
            Order.created_at.between(start_date, end_date)
        ).distinct().all())
        
        previous_customers = set(db.session.query(Order.user_id).filter(
            Order.created_at.between(previous_start, start_date)
        ).distinct().all())
        
        retained_customers = len(current_customers.intersection(previous_customers))
        
        retention_rate = (retained_customers / len(previous_customers) * 100) if previous_customers else 0
        
        return {
            'current_period_customers': len(current_customers),
            'previous_period_customers': len(previous_customers),
            'retained_customers': retained_customers,
            'retention_rate': round(retention_rate, 2)
        }
    
    def _get_customer_lifetime_value_analysis(self) -> Dict[str, Any]:
        """Calculate customer lifetime value metrics"""
        # Average customer lifetime value
        customer_values = db.session.query(
            Order.user_id,
            func.sum(Order.total_amount).label('total_value'),
            func.count(Order.id).label('order_count'),
            func.min(Order.created_at).label('first_order'),
            func.max(Order.created_at).label('last_order')
        ).filter(
            Order.status != OrderStatus.CANCELLED
        ).group_by(Order.user_id).all()
        
        total_clv = sum(customer.total_value for customer in customer_values)
        avg_clv = total_clv / len(customer_values) if customer_values else 0
        
        # Calculate average customer lifespan
        active_customers = [c for c in customer_values if c.order_count > 1]
        avg_lifespan_days = 0
        
        if active_customers:
            lifespans = [(c.last_order - c.first_order).days for c in active_customers]
            avg_lifespan_days = sum(lifespans) / len(lifespans)
        
        return {
            'average_clv': round(avg_clv, 2),
            'total_customers': len(customer_values),
            'average_lifespan_days': round(avg_lifespan_days, 2),
            'average_orders_per_customer': round(sum(c.order_count for c in customer_values) / len(customer_values), 2) if customer_values else 0
        }
    
    def _get_customer_churn_analysis(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Analyze customer churn patterns"""
        # Customers who haven't ordered in the last 30 days
        inactive_threshold = datetime.now(UTC) - timedelta(days=30)
        
        total_customers = User.query.filter_by(is_active=True).count()
        
        # Customers with recent orders
        active_customers = db.session.query(func.count(func.distinct(Order.user_id))).filter(
            Order.created_at >= inactive_threshold
        ).scalar()
        
        # Estimated churned customers
        churned_customers = total_customers - active_customers
        churn_rate = (churned_customers / total_customers * 100) if total_customers > 0 else 0
        
        return {
            'total_customers': total_customers,
            'active_customers': active_customers,
            'churned_customers': churned_customers,
            'churn_rate': round(churn_rate, 2)
        }
    
    def _get_customer_behavior_patterns(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Analyze customer behavior patterns"""
        # Order frequency patterns
        customer_frequencies = db.session.query(
            Order.user_id,
            func.count(Order.id).label('order_count')
        ).filter(
            Order.created_at.between(start_date, end_date)
        ).group_by(Order.user_id).all()
        
        frequency_distribution = {
            'single_order': 0,
            'occasional': 0,    # 2-5 orders
            'regular': 0,       # 6-15 orders
            'frequent': 0       # 16+ orders
        }
        
        for customer in customer_frequencies:
            count = customer.order_count
            if count == 1:
                frequency_distribution['single_order'] += 1
            elif count <= 5:
                frequency_distribution['occasional'] += 1
            elif count <= 15:
                frequency_distribution['regular'] += 1
            else:
                frequency_distribution['frequent'] += 1
        
        return {
            'frequency_distribution': frequency_distribution,
            'total_analyzed_customers': len(customer_frequencies)
        }
    
    def _generate_daily_report(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Generate daily business report"""
        return {
            'report_type': 'daily',
            'overview': self.get_dashboard_overview(start_date, end_date),
            'sales': self.get_sales_analytics(start_date, end_date),
            'delivery': self.get_delivery_analytics(start_date, end_date)
        }
    
    def _generate_weekly_report(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Generate weekly business report"""
        return {
            'report_type': 'weekly',
            'overview': self.get_dashboard_overview(start_date, end_date),
            'sales': self.get_sales_analytics(start_date, end_date),
            'customers': self.get_customer_analytics(start_date, end_date),
            'delivery': self.get_delivery_analytics(start_date, end_date)
        }
    
    def _generate_monthly_report(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Generate monthly business report"""
        return {
            'report_type': 'monthly',
            'overview': self.get_dashboard_overview(start_date, end_date),
            'sales': self.get_sales_analytics(start_date, end_date),
            'customers': self.get_customer_analytics(start_date, end_date),
            'delivery': self.get_delivery_analytics(start_date, end_date),
            'predictions': self.predict_demand(30),
            'churn_analysis': self.predict_customer_churn()
        }
    
    def _generate_quarterly_report(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Generate quarterly business report"""
        return self._generate_monthly_report(start_date, end_date)
    
    def _generate_annual_report(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Generate annual business report"""
        return self._generate_monthly_report(start_date, end_date)