"""
Review Service for the Water Business Platform
Handles all product review-related business logic including creation, moderation, and analytics
"""

import logging
from datetime import datetime, UTC
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy import func

from business_app.models.review import Review
from business_app.models.product import Product
from business_app.models.user import User
from business_app.models.order import Order, OrderItem
from business_app.utils.exceptions import ValidationError, NotFoundError, ConflictError, ForbiddenError
from business_app.utils.service_logging import log_service_call, log_business_event, log_database_query
from business_app import db

logger = logging.getLogger(__name__)


class ReviewService:
    """
    Service for managing product reviews and ratings

    Responsibilities:
    - Review creation with validation
    - Review moderation and approval
    - Rating aggregation
    - Review filtering and pagination
    - Helpful votes tracking
    - Photo upload management
    - Verified purchase validation
    """

    def __init__(self):
        self.min_rating = 1
        self.max_rating = 5
        self.max_photos = 5
        self.review_edit_window_hours = 24
        self.auto_approve_threshold = 3  # Auto-approve after 3 approved reviews

    @log_service_call(operation_type="review_create", track_performance=True)
    @log_business_event(event_type="created", entity_type="review")
    def create_review(
        self,
        user_id: int,
        product_id: int,
        rating: int,
        title: Optional[str] = None,
        comment: Optional[str] = None,
        order_id: Optional[int] = None,
        photos: Optional[List[str]] = None,
    ) -> Review:
        """
        Create a new product review

        Args:
            user_id: ID of user creating review
            product_id: ID of product being reviewed
            rating: Rating (1-5 stars)
            title: Review title
            comment: Review comment
            order_id: Optional order ID for verified purchase
            photos: Optional list of photo URLs

        Returns:
            Created Review object

        Raises:
            ValidationError: If review data is invalid
            NotFoundError: If user or product not found
            ConflictError: If user already reviewed this product
            ForbiddenError: If user didn't purchase the product
        """
        # Validate rating
        if not (self.min_rating <= rating <= self.max_rating):
            raise ValidationError(f"Rating must be between {self.min_rating} and {self.max_rating}")

        # Validate user exists
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError(f"User with ID {user_id} not found")

        # Validate product exists
        product = Product.query.get(product_id)
        if not product or not product.is_active:
            raise NotFoundError(f"Product with ID {product_id} not found")

        # Check for duplicate review
        existing_review = Review.query.filter_by(user_id=user_id, product_id=product_id).first()

        if existing_review:
            raise ConflictError("You have already reviewed this product")

        # Verify purchase if order_id provided
        verified_purchase = False
        if order_id:
            order = Order.query.filter_by(id=order_id, user_id=user_id).first()

            if not order:
                raise ValidationError("Invalid order ID")

            # Check if order contains this product
            order_item = OrderItem.query.filter_by(order_id=order_id, product_id=product_id).first()

            if not order_item:
                raise ForbiddenError("This product was not in the specified order")

            # Check if order is delivered
            if order.status != "delivered":
                raise ValidationError("Can only review products from delivered orders")

            verified_purchase = True

        # Validate photos
        if photos and len(photos) > self.max_photos:
            raise ValidationError(f"Maximum {self.max_photos} photos allowed")

        # Determine if review should be auto-approved
        auto_approve = self._should_auto_approve(user_id)

        # Create review
        review = Review(
            user_id=user_id,
            product_id=product_id,
            order_id=order_id,
            rating=rating,
            title=title,
            comment=comment,
            photos=photos or [],
            is_approved=auto_approve,
        )

        db.session.add(review)
        db.session.flush()

        # Update product rating
        self._update_product_rating(product_id)

        db.session.commit()

        # Track analytics
        self._track_review_analytics(review, verified_purchase)

        logger.info(f"Review created: ID={review.id}, Product={product_id}, User={user_id}, Rating={rating}")

        return review

    @log_service_call(operation_type="review_update", track_performance=True)
    @log_business_event(event_type="updated", entity_type="review")
    def update_review(
        self,
        review_id: int,
        user_id: int,
        rating: Optional[int] = None,
        title: Optional[str] = None,
        comment: Optional[str] = None,
        photos: Optional[List[str]] = None,
    ) -> Review:
        """
        Update an existing review

        Args:
            review_id: ID of review to update
            user_id: ID of user updating review
            rating: Updated rating
            title: Updated title
            comment: Updated comment
            photos: Updated photos

        Returns:
            Updated Review object

        Raises:
            NotFoundError: If review not found
            ForbiddenError: If user doesn't own review or edit window expired
            ValidationError: If data is invalid
        """
        review = Review.query.get(review_id)
        if not review:
            raise NotFoundError(f"Review with ID {review_id} not found")

        # Check ownership
        if review.user_id != user_id:
            raise ForbiddenError("You can only edit your own reviews")

        # Check edit window
        if review.created_at:
            hours_since_creation = (datetime.now(UTC) - review.created_at).total_seconds() / 3600
            if hours_since_creation > self.review_edit_window_hours:
                raise ForbiddenError(f"Reviews can only be edited within {self.review_edit_window_hours} hours")

        # Update fields
        if rating is not None:
            if not (self.min_rating <= rating <= self.max_rating):
                raise ValidationError(f"Rating must be between {self.min_rating} and {self.max_rating}")
            review.rating = rating

        if title is not None:
            review.title = title

        if comment is not None:
            review.comment = comment

        if photos is not None:
            if len(photos) > self.max_photos:
                raise ValidationError(f"Maximum {self.max_photos} photos allowed")
            review.photos = photos

        # Reset approval if content changed significantly
        if rating is not None or comment is not None:
            review.is_approved = False

        db.session.commit()

        # Update product rating if rating changed
        if rating is not None:
            self._update_product_rating(review.product_id)

        logger.info(f"Review updated: ID={review_id}, User={user_id}")

        return review

    @log_service_call(operation_type="review_delete", track_performance=True)
    @log_business_event(event_type="deleted", entity_type="review")
    def delete_review(self, review_id: int, user_id: int, is_admin: bool = False) -> None:
        """
        Delete a review

        Args:
            review_id: ID of review to delete
            user_id: ID of user deleting review
            is_admin: Whether user is admin

        Raises:
            NotFoundError: If review not found
            ForbiddenError: If user doesn't own review and isn't admin
        """
        review = Review.query.get(review_id)
        if not review:
            raise NotFoundError(f"Review with ID {review_id} not found")

        # Check permissions
        if not is_admin and review.user_id != user_id:
            raise ForbiddenError("You can only delete your own reviews")

        product_id = review.product_id

        db.session.delete(review)
        db.session.commit()

        # Update product rating
        self._update_product_rating(product_id)

        logger.info(f"Review deleted: ID={review_id}, User={user_id}, IsAdmin={is_admin}")

    @log_service_call(operation_type="review_query", track_performance=True)
    @log_database_query(query_type="SELECT", entity_type="review")
    def get_product_reviews(
        self,
        product_id: int,
        page: int = 1,
        per_page: int = 20,
        rating_filter: Optional[int] = None,
        sort_by: str = "recent",  # recent, helpful, highest, lowest
        approved_only: bool = True,
    ) -> Tuple[List[Review], int, int, int]:
        """
        Get reviews for a product with filtering and pagination

        Args:
            product_id: ID of product
            page: Page number
            per_page: Items per page
            rating_filter: Filter by specific rating (1-5)
            sort_by: Sort order
            approved_only: Only show approved reviews

        Returns:
            Tuple of (reviews list, total, page, per_page)
        """
        # Build query
        query = Review.query.filter_by(product_id=product_id)

        if approved_only:
            query = query.filter_by(is_approved=True)

        if rating_filter:
            query = query.filter_by(rating=rating_filter)

        # Apply sorting
        if sort_by == "helpful":
            query = query.order_by(Review.helpful_count.desc(), Review.created_at.desc())
        elif sort_by == "highest":
            query = query.order_by(Review.rating.desc(), Review.created_at.desc())
        elif sort_by == "lowest":
            query = query.order_by(Review.rating.asc(), Review.created_at.desc())
        else:  # recent
            query = query.order_by(Review.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        return pagination.items, pagination.total, page, per_page

    @log_service_call(operation_type="review_moderate", track_performance=True)
    @log_business_event(event_type="moderated", entity_type="review")
    def moderate_review(
        self,
        review_id: int,
        moderator_id: int,
        approve: bool,
        moderator_notes: Optional[str] = None,
        is_featured: bool = False,
    ) -> Review:
        """
        Moderate a review (approve/reject)

        Args:
            review_id: ID of review
            moderator_id: ID of moderator
            approve: Whether to approve review
            moderator_notes: Notes from moderator
            is_featured: Mark as featured

        Returns:
            Moderated Review object

        Raises:
            NotFoundError: If review not found
        """
        review = Review.query.get(review_id)
        if not review:
            raise NotFoundError(f"Review with ID {review_id} not found")

        review.is_approved = approve
        review.is_featured = is_featured if approve else False
        review.moderator_notes = moderator_notes

        db.session.commit()

        # Update product rating if approved
        if approve:
            self._update_product_rating(review.product_id)

        logger.info(f"Review moderated: ID={review_id}, Approved={approve}, Moderator={moderator_id}")

        return review

    @log_service_call(operation_type="review_helpful", track_performance=True)
    def mark_helpful(self, review_id: int, user_id: int) -> Review:
        """
        Mark a review as helpful

        Args:
            review_id: ID of review
            user_id: ID of user marking helpful

        Returns:
            Updated Review object

        Raises:
            NotFoundError: If review not found
        """
        review = Review.query.get(review_id)
        if not review:
            raise NotFoundError(f"Review with ID {review_id} not found")

        # In a production system, we'd track individual votes
        # For now, just increment the counter
        review.helpful_count += 1

        db.session.commit()

        logger.info(f"Review marked helpful: ID={review_id}, User={user_id}")

        return review

    @log_service_call(operation_type="review_stats", track_performance=True)
    def get_product_review_stats(self, product_id: int) -> Dict[str, Any]:
        """
        Get review statistics for a product

        Args:
            product_id: ID of product

        Returns:
            Dictionary with review stats
        """
        # Get all approved reviews for product
        reviews = Review.query.filter_by(product_id=product_id, is_approved=True).all()

        if not reviews:
            return {
                "total_reviews": 0,
                "average_rating": 0.0,
                "rating_distribution": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
                "verified_purchase_percentage": 0.0,
            }

        # Calculate stats
        total_reviews = len(reviews)
        average_rating = sum(r.rating for r in reviews) / total_reviews

        # Rating distribution
        rating_distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        verified_count = 0

        for review in reviews:
            rating_distribution[review.rating] += 1
            if review.order_id:
                verified_count += 1

        verified_percentage = (verified_count / total_reviews * 100) if total_reviews > 0 else 0

        return {
            "total_reviews": total_reviews,
            "average_rating": round(average_rating, 2),
            "rating_distribution": rating_distribution,
            "verified_purchase_percentage": round(verified_percentage, 1),
        }

    # Private helper methods

    def _should_auto_approve(self, user_id: int) -> bool:
        """Determine if user's review should be auto-approved"""
        # Count user's previously approved reviews
        approved_count = Review.query.filter_by(user_id=user_id, is_approved=True).count()

        return approved_count >= self.auto_approve_threshold

    def _update_product_rating(self, product_id: int) -> None:
        """Update product's average rating"""
        try:
            # Calculate average rating from approved reviews
            avg_rating = (
                db.session.query(func.avg(Review.rating))
                .filter(Review.product_id == product_id, Review.is_approved == True)
                .scalar()
            )

            # Update product if it has rating field
            # This would need to be added to Product model
            # product = Product.query.get(product_id)
            # if product:
            #     product.average_rating = float(avg_rating) if avg_rating else 0.0
            #     db.session.commit()

            logger.debug(f"Updated product rating: Product={product_id}, AvgRating={avg_rating}")

        except Exception as e:
            logger.warning(f"Failed to update product rating: {e}")

    def _track_review_analytics(self, review: Review, verified_purchase: bool) -> None:
        """Track review creation for analytics"""
        try:
            from business_app.utils.service_factory import get_analytics_service

            analytics = get_analytics_service()

            analytics.track_review_created(
                review_id=review.id,
                product_id=review.product_id,
                user_id=review.user_id,
                rating=review.rating,
                verified_purchase=verified_purchase,
            )
        except Exception as e:
            logger.warning(f"Failed to track review analytics: {e}")


# Singleton instance
_review_service = None


def get_review_service() -> ReviewService:
    """Get or create ReviewService singleton instance"""
    global _review_service
    if _review_service is None:
        _review_service = ReviewService()
    return _review_service


# Export
__all__ = ["ReviewService", "get_review_service"]
