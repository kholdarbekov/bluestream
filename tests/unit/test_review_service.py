"""Unit tests for ReviewService aligned with the current implementation."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.models.review import Review
from business_app.models.user import User
from business_app.services.review_service import ReviewService
from business_app.utils.constants import OrderStatus, UserRole
from business_app.utils.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from business_app.utils.password_security import hash_password


@pytest.fixture
def review_service(app):
    with app.app_context():
        return ReviewService()


@pytest.fixture
def make_user(db):
    counter = {'value': 0}

    def _make_user(email_prefix='user'):
        counter['value'] += 1
        idx = counter['value']
        user = User(
            email=f'{email_prefix}{idx}@example.com',
            phone=f'+99890123{idx:04d}',
            password_hash=hash_password('TestPassword123!'),
            first_name='Test',
            last_name=f'User{idx}',
            role=UserRole.CUSTOMER,
            is_verified=True,
        )
        db.session.add(user)
        db.session.flush()
        return user

    return _make_user


@pytest.fixture
def sample_review(db, sample_user, sample_product):
    review = Review(
        user_id=sample_user.id,
        product_id=sample_product.id,
        rating=4,
        title='Great product',
        comment='Very good quality water',
        is_approved=True,
        helpful_count=5,
    )
    db.session.add(review)
    db.session.commit()
    return review


@pytest.mark.unit
@pytest.mark.review
class TestReviewCreation:
    def test_create_review_basic(self, review_service, sample_user, sample_product):
        with patch.object(review_service, '_track_review_analytics'):
            review = review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5,
                title='Excellent',
                comment='Best water ever',
            )

        assert review.id is not None
        assert review.rating == 5
        assert review.title == 'Excellent'

    def test_create_review_validates_rating(self, review_service, sample_user, sample_product):
        with pytest.raises(ValidationError):
            review_service.create_review(user_id=sample_user.id, product_id=sample_product.id, rating=0)

    def test_create_review_user_not_found(self, review_service, sample_product):
        with pytest.raises(NotFoundError):
            review_service.create_review(user_id=999999, product_id=sample_product.id, rating=5)

    def test_create_review_duplicate(self, review_service, sample_user, sample_product, sample_review):
        with pytest.raises(ConflictError):
            review_service.create_review(user_id=sample_user.id, product_id=sample_product.id, rating=5)

    def test_create_review_too_many_photos(self, review_service, sample_user, sample_product):
        with pytest.raises(ValidationError):
            review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5,
                photos=['1.jpg', '2.jpg', '3.jpg', '4.jpg', '5.jpg', '6.jpg'],
            )

    def test_create_review_invalid_order_reference(self, review_service, sample_user, sample_product):
        with pytest.raises(ValidationError):
            review_service.create_review(
                user_id=sample_user.id,
                product_id=sample_product.id,
                rating=5,
                order_id=999999,
            )


@pytest.mark.unit
@pytest.mark.review
class TestReviewUpdatesAndDeletes:
    def test_update_review_rating_resets_approval(self, review_service, sample_user, sample_review, db):
        sample_review.created_at = datetime.now(UTC)
        db.session.flush()
        updated = review_service.update_review(review_id=sample_review.id, user_id=sample_user.id, rating=5)

        assert updated.rating == 5
        assert updated.is_approved is False

    def test_update_review_forbidden_for_other_user(self, review_service, sample_review, make_user):
        other = make_user('other')

        with pytest.raises(ForbiddenError):
            review_service.update_review(review_id=sample_review.id, user_id=other.id, rating=5)

    def test_update_review_respects_edit_window(self, review_service, sample_user, sample_review, db):
        sample_review.created_at = datetime.now(UTC) - timedelta(hours=25)
        db.session.flush()

        with pytest.raises(ForbiddenError):
            review_service.update_review(review_id=sample_review.id, user_id=sample_user.id, rating=5)

    def test_delete_review_by_owner(self, review_service, sample_user, sample_review, db):
        review_service.delete_review(review_id=sample_review.id, user_id=sample_user.id)

        assert db.session.get(Review, sample_review.id) is None

    def test_delete_review_by_admin_flag(self, review_service, sample_review, make_user, db):
        admin_like_user = make_user('admin')
        review_service.delete_review(review_id=sample_review.id, user_id=admin_like_user.id, is_admin=True)

        assert db.session.get(Review, sample_review.id) is None


@pytest.mark.unit
@pytest.mark.review
class TestReviewQueriesAndModeration:
    def test_get_product_reviews_filters_approved_only(self, review_service, sample_product, sample_user, make_user, db):
        approved = Review(user_id=sample_user.id, product_id=sample_product.id, rating=5, is_approved=True)
        unapproved_user = make_user('pending')
        unapproved = Review(user_id=unapproved_user.id, product_id=sample_product.id, rating=3, is_approved=False)
        db.session.add_all([approved, unapproved])
        db.session.commit()

        reviews, total, *_ = review_service.get_product_reviews(product_id=sample_product.id, approved_only=True)

        assert total == 1
        assert reviews[0].is_approved is True

    def test_get_product_reviews_sort_by_helpful(self, review_service, sample_product, sample_user, make_user, db):
        first = Review(user_id=sample_user.id, product_id=sample_product.id, rating=4, is_approved=True, helpful_count=2)
        second_user = make_user('helpful')
        second = Review(user_id=second_user.id, product_id=sample_product.id, rating=5, is_approved=True, helpful_count=10)
        db.session.add_all([first, second])
        db.session.commit()

        reviews, *_ = review_service.get_product_reviews(product_id=sample_product.id, sort_by='helpful')

        assert reviews[0].helpful_count == 10
        assert reviews[1].helpful_count == 2

    def test_moderate_review_updates_flags(self, review_service, admin_user, sample_review):
        sample_review.is_approved = False

        moderated = review_service.moderate_review(
            review_id=sample_review.id,
            moderator_id=admin_user.id,
            approve=True,
            moderator_notes='Looks good',
            is_featured=True,
        )

        assert moderated.is_approved is True
        assert moderated.is_featured is True
        assert moderated.moderator_notes == 'Looks good'

    def test_mark_helpful_increments_counter(self, review_service, sample_review, sample_user):
        initial = sample_review.helpful_count
        updated = review_service.mark_helpful(review_id=sample_review.id, user_id=sample_user.id)
        assert updated.helpful_count == initial + 1


@pytest.mark.unit
@pytest.mark.review
class TestReviewStatsAndAnalytics:
    def test_get_product_review_stats(self, review_service, sample_product, sample_user, sample_order, make_user, db):
        r1 = Review(
            user_id=sample_user.id,
            product_id=sample_product.id,
            order_id=sample_order.id,
            rating=5,
            is_approved=True,
        )
        u2 = make_user('stats')
        r2 = Review(user_id=u2.id, product_id=sample_product.id, rating=3, is_approved=True)
        db.session.add_all([r1, r2])
        db.session.commit()

        stats = review_service.get_product_review_stats(sample_product.id)

        assert stats['total_reviews'] == 2
        assert stats['average_rating'] == 4.0
        assert stats['rating_distribution'][5] == 1
        assert stats['rating_distribution'][3] == 1
        assert stats['verified_purchase_percentage'] == 50.0

    def test_track_review_analytics_uses_service_factory(self, review_service, sample_user, sample_product):
        mock_analytics = MagicMock()

        with patch('business_app.utils.service_factory.get_analytics_service', return_value=mock_analytics):
            with patch.object(review_service, '_update_product_rating'):
                review = review_service.create_review(
                    user_id=sample_user.id,
                    product_id=sample_product.id,
                    rating=5,
                )

        mock_analytics.track_review_created.assert_called_once()
        assert review.id is not None

    def test_track_review_analytics_errors_do_not_break_creation(self, review_service, sample_user, sample_product):
        mock_analytics = MagicMock()
        mock_analytics.track_review_created.side_effect = RuntimeError('analytics down')

        with patch('business_app.utils.service_factory.get_analytics_service', return_value=mock_analytics):
            with patch.object(review_service, '_update_product_rating'):
                review = review_service.create_review(
                    user_id=sample_user.id,
                    product_id=sample_product.id,
                    rating=4,
                )

        assert review.id is not None
