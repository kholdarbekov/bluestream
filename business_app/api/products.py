"""
Products API endpoints
This file should be placed in business_app/api/products.py
"""
from flask import Blueprint, request, current_app, g
from flask_jwt_extended import jwt_required, get_jwt_identity, jwt_required
from sqlalchemy import and_, or_, func
from datetime import datetime, UTC

from business_app.models.product import Product, ProductCategory, PriceRule
from business_app.models.review import Review
from business_app.models.user import User
# from business_app.services.recommendation_service import RecommendationService
from business_app.utils.service_factory import (
    get_analytics_service,
    get_product_service,
    get_review_service
)
from business_app.utils.helpers import get_current_language
from business_app.utils.translations import get_translation
from business_app.utils.exceptions import ValidationError, NotFoundError, ConflictError, ForbiddenError

# Import proper serializers
from business_app.serializers.product_serializers import (
    serialize_product,
    serialize_product_list,
    serialize_product_category
)
from business_app.serializers.review_serializers import ReviewSerializer

from business_app.utils.decorators import validate_json, cache_response
from business_app.utils.constants import PriceRuleType
from business_app import db

# Import API response helpers
from business_app.utils.api_responses import (
    success_response, error_response, paginated_response, created_response,
    not_found_response, validation_error_response, forbidden_response,
    conflict_response, internal_error_response
)

products_bp = Blueprint('products', __name__)


@products_bp.route('/categories', methods=['GET'])
@cache_response(300)  # Cache for 5 minutes
def get_categories():
    """Get all product categories"""
    try:
        language = get_current_language()
        product_service = get_product_service()

        categories = product_service.get_categories()

        return success_response(data={
            'categories': [
                serialize_product_category(cat, language)
                for cat in categories
            ]
        })

    except Exception as e:
        current_app.logger.error(f"Get categories error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@products_bp.route('/categories/<int:category_id>', methods=['GET'])
@cache_response(300)
def get_category(category_id):
    """Get specific category"""
    try:
        language = get_current_language()
        product_service = get_product_service()

        category = product_service.get_category_by_id(category_id)

        if not category:
            return not_found_response(message=get_translation('error.not_found'))

        return success_response(data={
            'category': serialize_product_category(category, language)
        })

    except NotFoundError as e:
        return not_found_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Get category error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@products_bp.route('/', methods=['GET'])
def get_products():
    """Get products with filtering and pagination"""
    try:
        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 100)
        category_id = request.args.get('category_id', type=int)
        search = request.args.get('search', '').strip()
        sort_by = request.args.get('sort_by', 'name')  # name, price, rating, popularity
        sort_order = request.args.get('sort_order', 'asc')  # asc, desc
        is_featured = request.args.get('is_featured', type=bool)
        min_price = request.args.get('min_price', type=float)
        max_price = request.args.get('max_price', type=float)
        language = get_current_language()
        in_stock_only = request.args.get('in_stock_only', type=bool, default=False)

        # Get current user for personalized pricing
        current_user = None
        try:
            if request.headers.get('Authorization'):
                from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
                verify_jwt_in_request(optional=True)
                user_id = get_jwt_identity()
                if user_id:
                    current_user = User.query.get(user_id)
        except:
            pass  # Continue without user context

        # Use ProductService to get products
        product_service = get_product_service()
        products, total, page, per_page, metadata = product_service.get_products_with_filters(
            page=page,
            per_page=per_page,
            category_id=category_id,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order,
            is_featured=is_featured,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
            current_user=current_user,
            language=language
        )

        return paginated_response(
            items=[
                serialize_product(
                    product,
                    language,
                    current_user
                ) for product in products
            ],
            page=page,
            per_page=per_page,
            total=total,
            additional_meta=metadata
        )

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Get products error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@products_bp.route('/<int:product_id>', methods=['GET'])
def get_product(product_id):
    """Get specific product details"""
    try:
        language = get_current_language()
        quantity = int(request.args.get('quantity', 1))

        # Get current user for personalized pricing
        current_user = None
        try:
            if request.headers.get('Authorization'):
                from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
                verify_jwt_in_request(optional=True)
                user_id = get_jwt_identity()
                if user_id:
                    current_user = User.query.get(user_id)
        except:
            pass

        # Use ProductService to get product
        product_service = get_product_service()
        product = product_service.get_product_by_id(
            product_id=product_id,
            current_user_id=current_user.id if current_user else None
        )

        # Get related products
        # related_products = recommendation_service.get_related_products(
        #     product_id, limit=4, user=current_user
        # )

        # Use ReviewService to get recent reviews
        review_service = get_review_service()
        reviews, _, _, _ = review_service.get_product_reviews(
            product_id=product_id,
            page=1,
            per_page=5,
            sort_by='recent',
            approved_only=True
        )

        return success_response(data={
            'product': serialize_product(
                product,
                language,
                current_user,
                quantity
            ),
            # 'related_products': [
            #     serialize_product(p, language, current_user)
            #     for p in related_products
            # ],
            'reviews': [
                ReviewSerializer(review).to_dict() for review in reviews
            ]
        })

    except NotFoundError as e:
        return not_found_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Get product error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@products_bp.route('/<int:product_id>/reviews', methods=['GET'])
def get_product_reviews(product_id):
    """Get product reviews with pagination"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 10)), 50)
        sort_by_param = request.args.get('sort_by', 'created_at')  # created_at, rating, helpful

        # Map API sort_by values to ReviewService values
        sort_by_mapping = {
            'created_at': 'recent',
            'rating': 'highest',
            'helpful': 'helpful'
        }
        sort_by = sort_by_mapping.get(sort_by_param, 'recent')

        # Verify product exists
        product_service = get_product_service()

        # Use ReviewService to get reviews
        review_service = get_review_service()
        reviews, total, page, per_page = review_service.get_product_reviews(
            product_id=product_id,
            page=page,
            per_page=per_page,
            sort_by=sort_by,
            approved_only=True
        )

        # Get review statistics
        review_stats = review_service.get_product_review_stats(product_id)

        return paginated_response(
            items=[
                ReviewSerializer(review).to_dict() for review in reviews
            ],
            page=page,
            per_page=per_page,
            total=total,
            additional_meta={
                'summary': review_stats
            }
        )

    except NotFoundError as e:
        return not_found_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Get product reviews error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@products_bp.route('/<int:product_id>/reviews', methods=['POST'])
@jwt_required()
@validate_json(['rating'])
def add_product_review(product_id):
    """Add a product review"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        # Find the order that contains this product
        from business_app.models.order import Order, OrderItem
        has_purchased = db.session.query(OrderItem).join(Order).filter(
            Order.user_id == current_user_id,
            OrderItem.product_id == product_id,
            Order.status == 'delivered'
        ).first()

        order_id = has_purchased.order_id if has_purchased else None

        # Use ReviewService to create review
        review_service = get_review_service()
        review = review_service.create_review(
            user_id=current_user_id,
            product_id=product_id,
            rating=data.get('rating'),
            title=data.get('title'),
            comment=data.get('comment'),
            order_id=order_id,
            photos=data.get('photos', [])
        )

        return created_response(
            data={
                'review': ReviewSerializer(review).to_dict()
            },
            message=get_translation('success.saved')
        )

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ConflictError as e:
        return conflict_response(message=str(e))
    except ForbiddenError as e:
        return forbidden_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Add review error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@products_bp.route('/featured', methods=['GET'])
@cache_response(600)  # Cache for 10 minutes
def get_featured_products():
    """Get featured products"""
    try:
        language = get_current_language()
        limit = min(int(request.args.get('limit', 8)), 20)

        # Get current user for personalized pricing
        current_user = None
        try:
            if request.headers.get('Authorization'):
                from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
                verify_jwt_in_request(optional=True)
                user_id = get_jwt_identity()
                if user_id:
                    current_user = User.query.get(user_id)
        except:
            pass

        # Use ProductService to get featured products
        product_service = get_product_service()
        products = product_service.get_featured_products(
            limit=limit,
            current_user=current_user
        )

        return success_response(data={
            'featured_products': [
                serialize_product(
                    product,
                    language,
                    current_user
                ) for product in products
            ]
        })

    except Exception as e:
        current_app.logger.error(f"Get featured products error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@products_bp.route('/bulk', methods=['POST'])
def get_products_bulk():
    """
    Get multiple products by IDs
    Used by cart page to fetch product details for cart items
    """
    try:
        data = request.get_json()

        if not data or 'product_ids' not in data:
            return validation_error_response(
                errors={'product_ids': 'Product IDs are required'}
            )

        product_ids = data.get('product_ids', [])

        if not isinstance(product_ids, list):
            return validation_error_response(
                errors={'product_ids': 'Product IDs must be a list'}
            )

        if len(product_ids) == 0:
            return success_response(data={'products': []})

        if len(product_ids) > 50:
            return validation_error_response(
                errors={'product_ids': 'Maximum 50 products can be fetched at once'}
            )

        language = get_current_language()

        # Get current user for personalized pricing
        current_user = None
        try:
            if request.headers.get('Authorization'):
                from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
                verify_jwt_in_request(optional=True)
                user_id = get_jwt_identity()
                if user_id:
                    current_user = User.query.get(user_id)
        except:
            pass

        # Fetch products
        products = Product.query.filter(
            Product.id.in_(product_ids),
            Product.is_active == True
        ).all()

        # Create a dictionary for quick lookup
        products_dict = {p.id: p for p in products}

        # Build response maintaining order of requested IDs
        result = []
        for product_id in product_ids:
            if product_id in products_dict:
                product = products_dict[product_id]
                result.append(serialize_product(product, language, current_user))

        return success_response(data={
            'products': result,
            'found_count': len(result),
            'requested_count': len(product_ids)
        })

    except Exception as e:
        current_app.logger.error(f"Get products bulk error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


# @products_bp.route('/recommendations', methods=['GET'])
# @jwt_required()
# def get_recommendations():
#     """Get personalized product recommendations"""
#     try:
#         current_user_id = get_jwt_identity()
#         language = request.args.get('language', 'uz')
#         limit = min(int(request.args.get('limit', 10)), 20)

#         current_user = User.query.get(current_user_id)
#         if not current_user:
#             return jsonify({'error': 'User not found'}), 404

#         recommendations = recommendation_service.get_personalized_recommendations(
#             user_id=current_user_id,
#             limit=limit
#         )

#         return jsonify({
#             'recommendations': [
#                 {
#                     'product': serialize_product(
#                         rec['product'],
#                         language,
#                         current_user
#                     ),
#                     'score': rec['score'],
#                     'reason': rec['reason']
#                 }
#                 for rec in recommendations
#             ]
#         })

#     except Exception as e:
#         current_app.logger.error(f"Get recommendations error: {e}")
#         return jsonify({'error': 'Failed to get recommendations'}), 500


@products_bp.route('/search-suggestions', methods=['GET'])
def get_search_suggestions():
    """Get search suggestions"""
    try:
        query = request.args.get('q', '').strip()
        language = get_current_language()
        limit = min(int(request.args.get('limit', 5)), 10)

        if len(query) < 2:
            return success_response(data={'suggestions': []})

        # Use ProductService to get search suggestions
        product_service = get_product_service()
        suggestions = product_service.get_search_suggestions(
            query=query,
            limit=limit,
            language=language
        )

        return success_response(data={'suggestions': suggestions})

    except Exception as e:
        current_app.logger.error(f"Get search suggestions error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@products_bp.route('/popular', methods=['GET'])
@cache_response(1800)  # Cache for 30 minutes
def get_popular_products():
    """Get popular products based on sales and views"""
    try:
        language = get_current_language()
        limit = min(int(request.args.get('limit', 10)), 20)
        period = request.args.get('period', 'week')  # week, month, all

        # Get current user for personalized pricing
        current_user = None
        try:
            if request.headers.get('Authorization'):
                from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
                verify_jwt_in_request(optional=True)
                user_id = get_jwt_identity()
                if user_id:
                    current_user = User.query.get(user_id)
        except:
            pass

        # Use ProductService to get popular products
        product_service = get_product_service()
        products = product_service.get_popular_products(
            limit=limit,
            period=period,
            current_user=current_user
        )

        return success_response(
            data={
                'popular_products': [
                    serialize_product(
                        product,
                        language,
                        current_user
                    ) for product in products
                ]
            },
            meta={'period': period}
        )

    except Exception as e:
        current_app.logger.error(f"Get popular products error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@products_bp.route('/price-calculator', methods=['POST'])
@validate_json(['product_id', 'quantity'])
def calculate_price():
    """Calculate product price with discounts"""
    try:
        data = request.get_json()
        product_id = data.get('product_id')
        quantity = data.get('quantity', 1)

        if quantity < 1:
            return validation_error_response(errors=get_translation('error.validation.min_value'))

        # Get current user for personalized pricing
        current_user = None
        try:
            if request.headers.get('Authorization'):
                from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
                verify_jwt_in_request(optional=True)
                user_id = get_jwt_identity()
                if user_id:
                    current_user = User.query.get(user_id)
        except:
            pass

        # Use ProductService to calculate price
        product_service = get_product_service()
        pricing_data = product_service.calculate_product_price(
            product_id=product_id,
            quantity=quantity,
            user=current_user,
            promo_code=data.get('promo_code')
        )

        return success_response(data=pricing_data)

    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Calculate price error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))
