"""
Unit tests for ReviewService
Tests review creation, moderation, validation, and analytics
"""
import pytest
from decimal import Decimal
from datetime import datetime, timedelta, UTC
from unittest.mock import patch, MagicMock

from business_app.services.review_service import ReviewService
from business_app.models.review import Review
from business_app.models.product import Product
from business_app.models.user import User, UserRole
from business_app.models.order import Order, OrderItem
from business_app.utils.exceptions import ValidationError, NotFoundError, ConflictError, ForbiddenError


@pytest.fixture
def review_service():
    """Create ReviewService instance"""
    return ReviewService()


@pytest.fixture
def sample_review(db, sample_user, sample_product):
    """Create a sample review"""
    review = Review(
        user_id=sample_user.id,
        product_id=sample_product.id,
        rating=4,
        title='Great product',
        comment='Very good quality water',
        is_approved=True,
        helpful_count=5
    )
    db.session.add(review)
    db.session.commit()
    return review


@pytest.fixture
def sample_order_with_product(db, sample_user, sample_product):
    """Create an order with a product for verified purchase testing"""
    order = Order(
        user_id=sample_user.id,
        status='delivered',
        total_amount=Decimal('15000.00'),
        delivery_address='Test Address'
    )
    db.session.add(order)
    db.session.flush()

    order_item = OrderItem(
        order_id=order.id,
        product_id=sample_product.id,
        quantity=1,
        unit_price=Decimal('15000.00'),
        total_price=Decimal('15000.00')
    )
    db.session.add(order_item)
    db.session.commit()
    return order


@pytest.mark.critical
@pytest.mark.review
class TestReviewCreation:
    """Test review creation logic"""

    def test_create_review_basic(self, review_service, sample_user, sample_product, db):
        """Test creating a basic review"""
        with patch.object(review_service, '_track_review_analytics'):
            review = review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5,
                title='Excellent',
                comment='Best water ever'
            )

            assert review.id is not None
            assert review.user_id == sample_user.id
            assert review.product_id == sample_product.id
            assert review.rating == 5
            assert review.title == 'Excellent'
            assert review.comment == 'Best water ever'

    def test_create_review_with_verified_purchase(self, review_service, sample_user, sample_product, sample_order_with_product, db):
        """Test creating review with verified purchase"""
        with patch.object(review_service, '_track_review_analytics'):
            review = review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5,
                title='Excellent',
                comment='Best water ever',
                order_id=sample_order_with_product.id
            )

            assert review.order_id == sample_order_with_product.id

    def test_create_review_with_photos(self, review_service, sample_user, sample_product, db):
        """Test creating review with photos"""
        photos = ['photo1.jpg', 'photo2.jpg']

        with patch.object(review_service, '_track_review_analytics'):
            review = review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=4,
                photos=photos
            )

            assert review.photos == photos

    def test_create_review_invalid_rating_low(self, review_service, sample_user, sample_product, db):
        """Test creating review with rating too low"""
        with pytest.raises(ValidationError, match="Rating must be between 1 and 5"):
            review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=0
            )

    def test_create_review_invalid_rating_high(self, review_service, sample_user, sample_product, db):
        """Test creating review with rating too high"""
        with pytest.raises(ValidationError, match="Rating must be between 1 and 5"):
            review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=6
            )

    def test_create_review_user_not_found(self, review_service, sample_product, db):
        """Test creating review with non-existent user"""
        with pytest.raises(NotFoundError, match="User with ID 99999 not found"):
            review_service.create_review(
                user_id=99999,
                product_id=sample_product.id,
                rating=5
            )

    def test_create_review_product_not_found(self, review_service, sample_user, db):
        """Test creating review with non-existent product"""
        with pytest.raises(NotFoundError, match="Product with ID 99999 not found"):
            review_service.create_review(
                user_id=sample_user.id,
                product_id=99999,
                rating=5
            )

    def test_create_review_duplicate(self, review_service, sample_user, sample_product, sample_review, db):
        """Test creating duplicate review for same product"""
        with pytest.raises(ConflictError, match="You have already reviewed this product"):
            review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5
            )

    def test_create_review_too_many_photos(self, review_service, sample_user, sample_product, db):
        """Test creating review with too many photos"""
        photos = ['photo1.jpg', 'photo2.jpg', 'photo3.jpg', 'photo4.jpg', 'photo5.jpg', 'photo6.jpg']

        with pytest.raises(ValidationError, match="Maximum 5 photos allowed"):
            review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5,
                photos=photos
            )

    def test_create_review_invalid_order(self, review_service, sample_user, sample_product, db):
        """Test creating review with invalid order ID"""
        with pytest.raises(ValidationError, match="Invalid order ID"):
            review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5,
                order_id=99999
            )

    def test_create_review_product_not_in_order(self, review_service, sample_user, sample_product, db):
        """Test creating review when product not in order"""
        # Create order without this product
        order = Order(
            user_id=sample_user.id,
            status='delivered',
            total_amount=Decimal('5000.00')
        )
        db.session.add(order)
        db.session.commit()

        with pytest.raises(ForbiddenError, match="This product was not in the specified order"):
            review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5,
                order_id=order.id
            )

    def test_create_review_order_not_delivered(self, review_service, sample_user, sample_product, db):
        """Test creating review when order not delivered"""
        # Create pending order
        order = Order(
            user_id=sample_user.id,
            status='pending',
            total_amount=Decimal('15000.00')
        )
        db.session.add(order)
        db.session.flush()

        order_item = OrderItem(
            order_id=order.id,
            product_id=sample_product.id,
            quantity=1,
            unit_price=Decimal('15000.00'),
            total_price=Decimal('15000.00')
        )
        db.session.add(order_item)
        db.session.commit()

        with pytest.raises(ValidationError, match="Can only review products from delivered orders"):
            review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5,
                order_id=order.id
            )

    def test_create_review_auto_approve(self, review_service, sample_user, sample_product, db):
        """Test review is auto-approved for trusted users"""
        # Create 3 approved reviews to meet threshold
        for i in range(3):
            product = Product(
                name=f'Test Product {i}',
                base_price=Decimal('1000.00'),
                is_active=True
            )
            db.session.add(product)
            db.session.flush()

            review = Review(
                user_id=sample_user.id,
                product_id=product.id,
                rating=5,
                is_approved=True
            )
            db.session.add(review)
        db.session.commit()

        # New review should be auto-approved
        with patch.object(review_service, '_track_review_analytics'):
            new_review = review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=4
            )

            assert new_review.is_approved is True


@pytest.mark.critical
@pytest.mark.review
class TestReviewUpdate:
    """Test review update logic"""

    def test_update_review_rating(self, review_service, sample_user, sample_review, db):
        """Test updating review rating"""
        updated = review_service.update_review(
            review_id=sample_review.id,
            user_id=sample_user.id,
            rating=5
        )

        assert updated.rating == 5
        assert updated.is_approved is False  # Should be reset for moderation

    def test_update_review_title_and_comment(self, review_service, sample_user, sample_review, db):
        """Test updating review title and comment"""
        updated = review_service.update_review(
            review_id=sample_review.id,
            user_id=sample_user.id,
            title='Updated title',
            comment='Updated comment'
        )

        assert updated.title == 'Updated title'
        assert updated.comment == 'Updated comment'

    def test_update_review_photos(self, review_service, sample_user, sample_review, db):
        """Test updating review photos"""
        new_photos = ['new1.jpg', 'new2.jpg']

        updated = review_service.update_review(
            review_id=sample_review.id,
            user_id=sample_user.id,
            photos=new_photos
        )

        assert updated.photos == new_photos

    def test_update_review_not_found(self, review_service, sample_user, db):
        """Test updating non-existent review"""
        with pytest.raises(NotFoundError, match="Review with ID 99999 not found"):
            review_service.update_review(
                review_id=99999,
                user_id=sample_user.id,
                rating=5
            )

    def test_update_review_wrong_user(self, review_service, sample_review, db):
        """Test updating review by different user"""
        other_user = User(
            email='other@example.com',
            phone='+998901234568',
            role=UserRole.CUSTOMER
        )
        db.session.add(other_user)
        db.session.commit()

        with pytest.raises(ForbiddenError, match="You can only edit your own reviews"):
            review_service.update_review(
                review_id=sample_review.id,
                user_id=other_user.id,
                rating=5
            )

    def test_update_review_expired_edit_window(self, review_service, sample_user, sample_review, db):
        """Test updating review after edit window expired"""
        # Set review creation time to 25 hours ago (beyond 24-hour window)
        sample_review.created_at = datetime.now(UTC) - timedelta(hours=25)
        db.session.commit()

        with pytest.raises(ForbiddenError, match="Reviews can only be edited within 24 hours"):
            review_service.update_review(
                review_id=sample_review.id,
                user_id=sample_user.id,
                rating=5
            )

    def test_update_review_invalid_rating(self, review_service, sample_user, sample_review, db):
        """Test updating review with invalid rating"""
        with pytest.raises(ValidationError, match="Rating must be between 1 and 5"):
            review_service.update_review(
                review_id=sample_review.id,
                user_id=sample_user.id,
                rating=6
            )

    def test_update_review_too_many_photos(self, review_service, sample_user, sample_review, db):
        """Test updating review with too many photos"""
        photos = ['p1.jpg', 'p2.jpg', 'p3.jpg', 'p4.jpg', 'p5.jpg', 'p6.jpg']

        with pytest.raises(ValidationError, match="Maximum 5 photos allowed"):
            review_service.update_review(
                review_id=sample_review.id,
                user_id=sample_user.id,
                photos=photos
            )


@pytest.mark.critical
@pytest.mark.review
class TestReviewDelete:
    """Test review deletion logic"""

    def test_delete_review_by_owner(self, review_service, sample_user, sample_review, db):
        """Test deleting review by owner"""
        review_service.delete_review(
            review_id=sample_review.id,
            user_id=sample_user.id
        )

        # Verify review is deleted
        deleted = Review.query.get(sample_review.id)
        assert deleted is None

    def test_delete_review_by_admin(self, review_service, sample_review, db):
        """Test deleting review by admin"""
        admin = User(
            email='admin@example.com',
            phone='+998901234569',
            role=UserRole.ADMIN
        )
        db.session.add(admin)
        db.session.commit()

        review_service.delete_review(
            review_id=sample_review.id,
            user_id=admin.id,
            is_admin=True
        )

        deleted = Review.query.get(sample_review.id)
        assert deleted is None

    def test_delete_review_not_found(self, review_service, sample_user, db):
        """Test deleting non-existent review"""
        with pytest.raises(NotFoundError, match="Review with ID 99999 not found"):
            review_service.delete_review(
                review_id=99999,
                user_id=sample_user.id
            )

    def test_delete_review_wrong_user(self, review_service, sample_review, db):
        """Test deleting review by different non-admin user"""
        other_user = User(
            email='other@example.com',
            phone='+998901234570',
            role=UserRole.CUSTOMER
        )
        db.session.add(other_user)
        db.session.commit()

        with pytest.raises(ForbiddenError, match="You can only delete your own reviews"):
            review_service.delete_review(
                review_id=sample_review.id,
                user_id=other_user.id,
                is_admin=False
            )


@pytest.mark.review
class TestReviewRetrieval:
    """Test review retrieval and filtering"""

    def test_get_product_reviews(self, review_service, sample_product, sample_review, db):
        """Test getting product reviews"""
        reviews, total, page, per_page = review_service.get_product_reviews(
            product_id=sample_product.id,
            page=1,
            per_page=10
        )

        assert len(reviews) == 1
        assert total == 1
        assert reviews[0].id == sample_review.id

    def test_get_product_reviews_approved_only(self, review_service, sample_user, sample_product, db):
        """Test filtering approved reviews only"""
        # Create approved and unapproved reviews
        approved = Review(
            user_id=sample_user.id,
            product_id=sample_product.id,
            rating=5,
            is_approved=True
        )
        unapproved = Review(
            user_id=sample_user.id,
            product_id=sample_product.id,
            rating=3,
            is_approved=False
        )

        # Create another user for second review
        other_user = User(
            email='other@example.com',
            phone='+998901234571',
            role=UserRole.CUSTOMER
        )
        db.session.add(other_user)
        db.session.flush()
        unapproved.user_id = other_user.id

        db.session.add_all([approved, unapproved])
        db.session.commit()

        reviews, total, page, per_page = review_service.get_product_reviews(
            product_id=sample_product.id,
            approved_only=True
        )

        assert len(reviews) == 1
        assert reviews[0].is_approved is True

    def test_get_product_reviews_rating_filter(self, review_service, sample_user, sample_product, db):
        """Test filtering by rating"""
        # Create reviews with different ratings
        for rating in [3, 4, 5]:
            user = User(
                email=f'user{rating}@example.com',
                phone=f'+99890123456{rating}',
                role=UserRole.CUSTOMER
            )
            db.session.add(user)
            db.session.flush()

            review = Review(
                user_id=user.id,
                product_id=sample_product.id,
                rating=rating,
                is_approved=True
            )
            db.session.add(review)
        db.session.commit()

        reviews, total, page, per_page = review_service.get_product_reviews(
            product_id=sample_product.id,
            rating_filter=5
        )

        assert len(reviews) == 1
        assert reviews[0].rating == 5

    def test_get_product_reviews_sort_by_recent(self, review_service, sample_user, sample_product, db):
        """Test sorting by recent"""
        # Create reviews at different times
        old_review = Review(
            user_id=sample_user.id,
            product_id=sample_product.id,
            rating=3,
            is_approved=True,
            created_at=datetime.now(UTC) - timedelta(days=5)
        )

        # Create another user for new review
        other_user = User(
            email='other@example.com',
            phone='+998901234572',
            role=UserRole.CUSTOMER
        )
        db.session.add(other_user)
        db.session.flush()

        new_review = Review(
            user_id=other_user.id,
            product_id=sample_product.id,
            rating=5,
            is_approved=True,
            created_at=datetime.now(UTC)
        )

        db.session.add_all([old_review, new_review])
        db.session.commit()

        reviews, total, page, per_page = review_service.get_product_reviews(
            product_id=sample_product.id,
            sort_by='recent'
        )

        assert reviews[0].id == new_review.id
        assert reviews[1].id == old_review.id

    def test_get_product_reviews_sort_by_helpful(self, review_service, sample_user, sample_product, db):
        """Test sorting by helpful count"""
        review1 = Review(
            user_id=sample_user.id,
            product_id=sample_product.id,
            rating=4,
            is_approved=True,
            helpful_count=10
        )

        other_user = User(
            email='other@example.com',
            phone='+998901234573',
            role=UserRole.CUSTOMER
        )
        db.session.add(other_user)
        db.session.flush()

        review2 = Review(
            user_id=other_user.id,
            product_id=sample_product.id,
            rating=5,
            is_approved=True,
            helpful_count=20
        )

        db.session.add_all([review1, review2])
        db.session.commit()

        reviews, total, page, per_page = review_service.get_product_reviews(
            product_id=sample_product.id,
            sort_by='helpful'
        )

        assert reviews[0].helpful_count == 20
        assert reviews[1].helpful_count == 10


@pytest.mark.review
class TestReviewModeration:
    """Test review moderation functionality"""

    def test_moderate_review_approve(self, review_service, admin_user, sample_review, db):
        """Test approving a review"""
        sample_review.is_approved = False
        db.session.commit()

        moderated = review_service.moderate_review(
            review_id=sample_review.id,
            moderator_id=admin_user.id,
            approve=True,
            moderator_notes='Looks good'
        )

        assert moderated.is_approved is True
        assert moderated.moderator_notes == 'Looks good'

    def test_moderate_review_reject(self, review_service, admin_user, sample_review, db):
        """Test rejecting a review"""
        moderated = review_service.moderate_review(
            review_id=sample_review.id,
            moderator_id=admin_user.id,
            approve=False,
            moderator_notes='Inappropriate content'
        )

        assert moderated.is_approved is False
        assert moderated.moderator_notes == 'Inappropriate content'

    def test_moderate_review_featured(self, review_service, admin_user, sample_review, db):
        """Test marking review as featured"""
        moderated = review_service.moderate_review(
            review_id=sample_review.id,
            moderator_id=admin_user.id,
            approve=True,
            is_featured=True
        )

        assert moderated.is_approved is True
        assert moderated.is_featured is True

    def test_moderate_review_not_found(self, review_service, admin_user, db):
        """Test moderating non-existent review"""
        with pytest.raises(NotFoundError, match="Review with ID 99999 not found"):
            review_service.moderate_review(
                review_id=99999,
                moderator_id=admin_user.id,
                approve=True
            )


@pytest.mark.review
class TestReviewHelpful:
    """Test helpful voting functionality"""

    def test_mark_helpful(self, review_service, sample_user, sample_review, db):
        """Test marking review as helpful"""
        initial_count = sample_review.helpful_count

        updated = review_service.mark_helpful(
            review_id=sample_review.id,
            user_id=sample_user.id
        )

        assert updated.helpful_count == initial_count + 1

    def test_mark_helpful_not_found(self, review_service, sample_user, db):
        """Test marking non-existent review as helpful"""
        with pytest.raises(NotFoundError, match="Review with ID 99999 not found"):
            review_service.mark_helpful(
                review_id=99999,
                user_id=sample_user.id
            )


@pytest.mark.review
class TestReviewStatistics:
    """Test review statistics functionality"""

    def test_get_product_review_stats(self, review_service, sample_user, sample_product, db):
        """Test getting review statistics"""
        # Create multiple reviews with different ratings
        for rating in [3, 4, 4, 5, 5]:
            user = User(
                email=f'user{rating}_{datetime.now(UTC).timestamp()}@example.com',
                phone=f'+99890123{rating}{int(datetime.now(UTC).timestamp() % 10000)}',
                role=UserRole.CUSTOMER
            )
            db.session.add(user)
            db.session.flush()

            review = Review(
                user_id=user.id,
                product_id=sample_product.id,
                rating=rating,
                is_approved=True
            )
            db.session.add(review)
        db.session.commit()

        stats = review_service.get_product_review_stats(sample_product.id)

        assert stats['total_reviews'] == 5
        assert stats['average_rating'] == 4.2
        assert stats['rating_distribution'][3] == 1
        assert stats['rating_distribution'][4] == 2
        assert stats['rating_distribution'][5] == 2

    def test_get_product_review_stats_no_reviews(self, review_service, sample_product, db):
        """Test statistics when no reviews exist"""
        stats = review_service.get_product_review_stats(sample_product.id)

        assert stats['total_reviews'] == 0
        assert stats['average_rating'] == 0.0
        assert stats['rating_distribution'] == {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}

    def test_get_product_review_stats_verified_percentage(self, review_service, sample_user, sample_product, sample_order_with_product, db):
        """Test verified purchase percentage calculation"""
        # Create review with verified purchase
        verified_review = Review(
            user_id=sample_user.id,
            product_id=sample_product.id,
            rating=5,
            order_id=sample_order_with_product.id,
            is_approved=True
        )
        db.session.add(verified_review)
        db.session.flush()

        # Create review without verified purchase
        other_user = User(
            email='other@example.com',
            phone='+998901234574',
            role=UserRole.CUSTOMER
        )
        db.session.add(other_user)
        db.session.flush()

        unverified_review = Review(
            user_id=other_user.id,
            product_id=sample_product.id,
            rating=4,
            is_approved=True
        )
        db.session.add(unverified_review)
        db.session.commit()

        stats = review_service.get_product_review_stats(sample_product.id)

        assert stats['total_reviews'] == 2
        assert stats['verified_purchase_percentage'] == 50.0


@pytest.mark.review
class TestAnalyticsTracking:
    """Test analytics tracking integration"""

    def test_track_review_analytics(self, review_service, sample_user, sample_product, db):
        """Test review analytics tracking"""
        mock_analytics = MagicMock()

        with patch('business_app.services.review_service.get_analytics_service', return_value=mock_analytics):
            review = review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5
            )

            # Should have called track_review_created
            assert mock_analytics.track_review_created.called

    def test_track_analytics_handles_errors(self, review_service, sample_user, sample_product, db):
        """Test that analytics errors don't break review operations"""
        mock_analytics = MagicMock()
        mock_analytics.track_review_created.side_effect = Exception("Analytics service unavailable")

        with patch('business_app.services.review_service.get_analytics_service', return_value=mock_analytics):
            # Should not raise exception
            review = review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5
            )

            assert review.id is not None
