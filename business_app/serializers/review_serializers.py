"""
Review Serializers for the Water Business Platform
This file contains serializers for review-related models
"""
from datetime import datetime, UTC
from typing import Dict, Any, Optional, List

from business_app.utils.constants import UserRole


class ReviewSerializer:
    """Serializer for Review model"""
    
    def __init__(self, review):
        self.review = review
    
    def to_dict(self, include_user_details: bool = True, include_admin_fields: bool = False) -> Dict[str, Any]:
        """
        Convert review instance to dictionary
        
        Args:
            include_user_details: Whether to include user information
            include_admin_fields: Whether to include admin-only fields
        """
        data = {
            'id': self.review.id,
            'product_id': self.review.product_id,
            'order_id': self.review.order_id,
            'rating': self.review.rating,
            'title': self.review.title,
            'comment': self.review.comment,
            'pros': self.review.pros,
            'cons': self.review.cons,
            'photos': self.review.photos or [],
            'helpful_count': getattr(self.review, 'helpful_count', 0),
            'unhelpful_count': getattr(self.review, 'unhelpful_count', 0),
            'is_verified_purchase': self.review.order_id is not None,
            'created_at': self.review.created_at.isoformat() if self.review.created_at else None,
            'updated_at': self.review.updated_at.isoformat() if self.review.updated_at else None
        }
        
        # Add user information
        if include_user_details and self.review.user:
            data['user'] = {
                'id': self.review.user.id,
                'name': self._get_display_name(),
                'avatar_url': getattr(self.review.user, 'avatar_url', None),
                'is_verified': getattr(self.review.user, 'is_verified', False),
                'review_count': self._get_user_review_count(),
                'member_since': self.review.user.created_at.year if self.review.user.created_at else None
            }
        
        # Add product information
        if self.review.product:
            data['product'] = {
                'id': self.review.product.id,
                'name': self.review.product.name,
                'image_url': self.review.product.images,
                'sku': self.review.product.sku
            }
        
        # Add order information for verified purchases
        if self.review.order:
            data['purchase_info'] = {
                'order_date': self.review.order.created_at.isoformat() if self.review.order.created_at else None,
                'order_number': self.review.order.order_number
            }
        
        # Add admin fields if requested
        if include_admin_fields:
            data['admin_fields'] = {
                'is_approved': getattr(self.review, 'is_approved', True),
                'moderation_status': getattr(self.review, 'moderation_status', 'approved'),
                'moderation_notes': getattr(self.review, 'moderation_notes', None),
                'flagged_count': getattr(self.review, 'flagged_count', 0),
                'sentiment_score': getattr(self.review, 'sentiment_score', None),
                'ip_address': getattr(self.review, 'ip_address', None)
            }
        
        # Add helpful statistics
        total_votes = data['helpful_count'] + data['unhelpful_count']
        if total_votes > 0:
            data['helpfulness_ratio'] = round(data['helpful_count'] / total_votes, 2)
        else:
            data['helpfulness_ratio'] = 0
        
        return data
    
    def _get_display_name(self) -> str:
        """Get display name for user (anonymized if needed)"""
        if not self.review.user:
            return "Anonymous"
        
        # For privacy, show only first name + last initial
        first_name = self.review.user.first_name or "Anonymous"
        last_name = self.review.user.last_name
        
        if last_name:
            return f"{first_name} {last_name[0]}."
        return first_name
    
    def _get_user_review_count(self) -> int:
        """Get total number of reviews by this user"""
        # This would typically query the database
        # For now, return a placeholder
        return 0
    
    @classmethod
    def serialize_list(cls, reviews: List, include_user_details: bool = True, include_admin_fields: bool = False) -> List[Dict[str, Any]]:
        """Serialize a list of reviews"""
        return [cls(review).to_dict(include_user_details=include_user_details, include_admin_fields=include_admin_fields) for review in reviews]


class ReviewSummarySerializer:
    """Serializer for review summary/statistics"""
    
    def __init__(self, product_id: int, review_stats: Dict[str, Any]):
        self.product_id = product_id
        self.review_stats = review_stats
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert review summary to dictionary"""
        return {
            'product_id': self.product_id,
            'total_reviews': self.review_stats.get('total_reviews', 0),
            'average_rating': round(self.review_stats.get('average_rating', 0), 1),
            'rating_distribution': {
                '5_star': self.review_stats.get('5_star_count', 0),
                '4_star': self.review_stats.get('4_star_count', 0),
                '3_star': self.review_stats.get('3_star_count', 0),
                '2_star': self.review_stats.get('2_star_count', 0),
                '1_star': self.review_stats.get('1_star_count', 0)
            },
            'rating_percentages': self._calculate_rating_percentages(),
            'sentiment_analysis': {
                'positive_percentage': self.review_stats.get('positive_percentage', 0),
                'neutral_percentage': self.review_stats.get('neutral_percentage', 0),
                'negative_percentage': self.review_stats.get('negative_percentage', 0)
            },
            'common_themes': {
                'positive': self.review_stats.get('positive_themes', []),
                'negative': self.review_stats.get('negative_themes', [])
            },
            'verified_purchase_percentage': self.review_stats.get('verified_purchase_percentage', 0),
            'recent_trend': self._calculate_recent_trend(),
            'recommendation_score': self._calculate_recommendation_score()
        }
    
    def _calculate_rating_percentages(self) -> Dict[str, float]:
        """Calculate percentage distribution of ratings"""
        total = self.review_stats.get('total_reviews', 0)
        if total == 0:
            return {'5_star': 0, '4_star': 0, '3_star': 0, '2_star': 0, '1_star': 0}
        
        return {
            '5_star': round((self.review_stats.get('5_star_count', 0) / total) * 100, 1),
            '4_star': round((self.review_stats.get('4_star_count', 0) / total) * 100, 1),
            '3_star': round((self.review_stats.get('3_star_count', 0) / total) * 100, 1),
            '2_star': round((self.review_stats.get('2_star_count', 0) / total) * 100, 1),
            '1_star': round((self.review_stats.get('1_star_count', 0) / total) * 100, 1)
        }
    
    def _calculate_recent_trend(self) -> Dict[str, Any]:
        """Calculate recent rating trend"""
        return {
            'direction': self.review_stats.get('trend_direction', 'stable'),  # up, down, stable
            'change_percentage': self.review_stats.get('trend_change', 0),
            'period': 'last_30_days'
        }
    
    def _calculate_recommendation_score(self) -> float:
        """Calculate how likely users are to recommend this product"""
        # Based on ratings 4-5 being "would recommend"
        total = self.review_stats.get('total_reviews', 0)
        if total == 0:
            return 0
        
        positive_ratings = (
            self.review_stats.get('5_star_count', 0) + 
            self.review_stats.get('4_star_count', 0)
        )
        
        return round((positive_ratings / total) * 100, 1)


class ReviewModerationSerializer:
    """Serializer for review moderation (admin use)"""
    
    def __init__(self, review):
        self.review = review
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert review to moderation format"""
        return {
            'id': self.review.id,
            'product_name': self.review.product.name if self.review.product else None,
            'user_name': self._get_full_user_name(),
            'rating': self.review.rating,
            'title': self.review.title,
            'comment': self.review.comment,
            'photos': self.review.photos or [],
            'status': {
                'is_approved': getattr(self.review, 'is_approved', True),
                'moderation_status': getattr(self.review, 'moderation_status', 'pending'),
                'flagged_count': getattr(self.review, 'flagged_count', 0),
                'auto_flagged': getattr(self.review, 'auto_flagged', False)
            },
            'analysis': {
                'sentiment_score': getattr(self.review, 'sentiment_score', None),
                'toxicity_score': getattr(self.review, 'toxicity_score', None),
                'spam_probability': getattr(self.review, 'spam_probability', None),
                'language_detected': getattr(self.review, 'detected_language', None)
            },
            'metadata': {
                'ip_address': getattr(self.review, 'ip_address', None),
                'user_agent': getattr(self.review, 'user_agent', None),
                'created_at': self.review.created_at.isoformat() if self.review.created_at else None,
                'is_verified_purchase': self.review.order_id is not None
            },
            'user_history': {
                'total_reviews': self._get_user_review_count(),
                'flagged_reviews': self._get_user_flagged_count(),
                'account_age_days': self._get_account_age_days()
            }
        }
    
    def _get_full_user_name(self) -> str:
        """Get full user name for admin view"""
        if not self.review.user:
            return "Anonymous"
        
        first_name = self.review.user.first_name or ""
        last_name = self.review.user.last_name or ""
        return f"{first_name} {last_name}".strip() or "Anonymous"
    
    def _get_user_review_count(self) -> int:
        """Get total reviews by this user"""
        # This would query the database
        return 0
    
    def _get_user_flagged_count(self) -> int:
        """Get count of flagged reviews by this user"""
        # This would query the database
        return 0
    
    def _get_account_age_days(self) -> int:
        """Get user account age in days"""
        if not self.review.user or not self.review.user.created_at:
            return 0
        
        return (datetime.now(UTC) - self.review.user.created_at).days


class ReviewFilterSerializer:
    """Serializer for review filters and sorting options"""
    
    @staticmethod
    def get_available_filters() -> Dict[str, Any]:
        """Get available review filters"""
        return {
            'rating': {
                'type': 'select',
                'options': [
                    {'value': '5', 'label': '5 Stars'},
                    {'value': '4', 'label': '4 Stars'},
                    {'value': '3', 'label': '3 Stars'},
                    {'value': '2', 'label': '2 Stars'},
                    {'value': '1', 'label': '1 Star'}
                ]
            },
            'verified_purchase': {
                'type': 'boolean',
                'label': 'Verified Purchase Only'
            },
            'has_photos': {
                'type': 'boolean',
                'label': 'Reviews with Photos'
            },
            'date_range': {
                'type': 'date_range',
                'options': [
                    {'value': '7', 'label': 'Last 7 days'},
                    {'value': '30', 'label': 'Last 30 days'},
                    {'value': '90', 'label': 'Last 3 months'},
                    {'value': '365', 'label': 'Last year'}
                ]
            },
            'sort_options': [
                {'value': 'newest', 'label': 'Newest First'},
                {'value': 'oldest', 'label': 'Oldest First'},
                {'value': 'highest_rating', 'label': 'Highest Rating'},
                {'value': 'lowest_rating', 'label': 'Lowest Rating'},
                {'value': 'most_helpful', 'label': 'Most Helpful'}
            ]
        }


class ReviewResponseSerializer:
    """Serializer for vendor/admin responses to reviews"""
    
    def __init__(self, review_response):
        self.response = review_response
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert review response to dictionary"""
        return {
            'id': self.response.id,
            'review_id': self.response.review_id,
            'response_text': self.response.response_text,
            'responder': {
                'id': self.response.user.id if self.response.user else None,
                'name': self._get_responder_name(),
                'role': self.response.responder_role,
                'is_verified_vendor': getattr(self.response, 'is_verified_vendor', False)
            },
            'created_at': self.response.created_at.isoformat() if self.response.created_at else None,
            'updated_at': self.response.updated_at.isoformat() if self.response.updated_at else None,
            'is_public': getattr(self.response, 'is_public', True),
            'helpful_count': getattr(self.response, 'helpful_count', 0)
        }
    
    def _get_responder_name(self) -> str:
        """Get responder display name"""
        if not self.response.user:
            return "Admin"

        if self.response.responder_role == UserRole.ADMIN:
            return "Customer Service"
        elif self.response.responder_role == UserRole.VENDOR:
            return "Vendor"

        return f"{self.response.user.first_name} {self.response.user.last_name}"