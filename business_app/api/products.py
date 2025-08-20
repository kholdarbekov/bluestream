"""
Products API endpoints
This file should be placed in business_app/api/products.py
"""
from flask import Blueprint, request, jsonify, current_app, g
from flask_jwt_extended import jwt_required, get_jwt_identity, jwt_required
from sqlalchemy import and_, or_, func
from datetime import datetime, UTC

from business_app.models.product import Product, ProductCategory, PriceRule
from business_app.models.review import Review
from business_app.models.user import User
# from business_app.services.recommendation_service import RecommendationService
from business_app.utils.service_factory import get_analytics_service
# Basic serializer functions - replace with proper serializers later
def product_to_dict(product, language='uz', user=None, quantity=1):
    return {
        'id': product.id,
        'name': product.name,
        'description': product.description,
        'sku': product.sku,
        'base_price': product.base_price,
        'current_price': product.current_price,
        'image_urls': product.image_urls or [],
        'is_active': product.is_active,
        'is_featured': product.is_featured,
        'stock_quantity': product.stock_quantity if product.track_inventory else None,
        'average_rating': product.average_rating,
        'review_count': product.review_count,
        'created_at': product.created_at.isoformat() if product.created_at else None
    }

def product_category_to_dict(category, language='uz'):
    return {
        'id': category.id,
        'name': category.name,
        'description': category.description,
        'icon_url': category.icon_url,
        'is_active': category.is_active,
        'sort_order': category.sort_order
    }

def review_to_dict(review):
    return {
        'id': review.id,
        'rating': review.rating,
        'title': review.title,
        'comment': review.comment,
        'user_name': review.user.first_name + ' ' + review.user.last_name if review.user else 'Anonymous',
        'created_at': review.created_at.isoformat() if review.created_at else None
    }

class ProductSerializer:
    def __init__(self, product):
        self.product = product
    
    def to_dict(self, language='uz', user=None, quantity=1):
        return product_to_dict(self.product, language, user, quantity)

class ProductCategorySerializer:
    def __init__(self, category):
        self.category = category
    
    def to_dict(self, language='uz'):
        return product_category_to_dict(self.category, language)

class ReviewSerializer:
    def __init__(self, review):
        self.review = review
    
    def to_dict(self):
        return review_to_dict(self.review)
from business_app.utils.decorators import validate_json, cache_response
from business_app.utils.constants import PriceRuleType
from business_app import db

products_bp = Blueprint('products', __name__)


@products_bp.route('/categories', methods=['GET'])
@cache_response(300)  # Cache for 5 minutes
def get_categories():
    """Get all product categories"""
    try:
        language = request.args.get('language', 'uz')
        
        categories = ProductCategory.query.filter_by(is_active=True).order_by(
            ProductCategory.sort_order, ProductCategory.name
        ).all()
        
        return jsonify({
            'categories': [
                ProductCategorySerializer(cat).to_dict(language=language) 
                for cat in categories
            ]
        })
        
    except Exception as e:
        current_app.logger.error(f"Get categories error: {e}")
        return jsonify({'error': 'Failed to get categories'}), 500


@products_bp.route('/categories/<int:category_id>', methods=['GET'])
@cache_response(300)
def get_category(category_id):
    """Get specific category"""
    try:
        language = request.args.get('language', 'uz')
        
        category = ProductCategory.query.filter_by(
            id=category_id, is_active=True
        ).first()
        
        if not category:
            return jsonify({'error': 'Category not found'}), 404
        
        return jsonify({
            'category': ProductCategorySerializer(category).to_dict(language=language)
        })
        
    except Exception as e:
        current_app.logger.error(f"Get category error: {e}")
        return jsonify({'error': 'Failed to get category'}), 500


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
        language = request.args.get('language', 'uz')
        
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
        
        # Build query
        query = Product.query.filter_by(is_active=True)
        
        # Apply filters
        if category_id:
            query = query.filter_by(category_id=category_id)
        
        if search:
            search_term = f"%{search}%"
            query = query.filter(or_(
                Product.name.ilike(search_term),
                Product.description.ilike(search_term),
                Product.sku.ilike(search_term)
            ))
        
        if is_featured is not None:
            query = query.filter_by(is_featured=is_featured)
        
        if min_price is not None:
            query = query.filter(Product.current_price >= min_price)
        
        if max_price is not None:
            query = query.filter(Product.current_price <= max_price)
        
        # Apply sorting
        if sort_by == 'price':
            order_field = Product.current_price
        elif sort_by == 'rating':
            order_field = Product.average_rating
        elif sort_by == 'popularity':
            order_field = Product.total_sold
        else:  # default to name
            order_field = Product.name
        
        if sort_order == 'desc':
            order_field = order_field.desc()
        
        query = query.order_by(order_field)
        
        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        products = pagination.items
        
        # Track product views for analytics
        if search:
            get_analytics_service().track_search(search, len(products))
        
        return jsonify({
            'products': [
                ProductSerializer(product).to_dict(
                    language=language, 
                    user=current_user
                ) for product in products
            ],
            'pagination': {
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            },
            'filters': {
                'category_id': category_id,
                'search': search,
                'sort_by': sort_by,
                'sort_order': sort_order,
                'is_featured': is_featured,
                'min_price': min_price,
                'max_price': max_price
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get products error: {e}")
        return jsonify({'error': 'Failed to get products'}), 500


@products_bp.route('/<int:product_id>', methods=['GET'])
def get_product(product_id):
    """Get specific product details"""
    try:
        language = request.args.get('language', 'uz')
        quantity = int(request.args.get('quantity', 1))
        
        product = Product.query.filter_by(id=product_id, is_active=True).first()
        if not product:
            return jsonify({'error': 'Product not found'}), 404
        
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
        
        # Track product view
        get_analytics_service().track_product_view(product_id, user_id=current_user.id if current_user else None)
        
        # Increment view count
        product.view_count += 1
        
        # Get related products
        # related_products = recommendation_service.get_related_products(
        #     product_id, limit=4, user=current_user
        # )
        
        # Get recent reviews
        reviews = Review.query.filter_by(
            product_id=product_id, is_approved=True
        ).order_by(Review.created_at.desc()).limit(5).all()
        
        db.session.commit()
        
        return jsonify({
            'product': ProductSerializer(product).to_dict(
                language=language, 
                user=current_user, 
                quantity=quantity
            ),
            # 'related_products': [
            #     ProductSerializer(p).to_dict(language=language, user=current_user) 
            #     for p in related_products
            # ],
            'reviews': [
                ReviewSerializer(review).to_dict() for review in reviews
            ]
        })
        
    except Exception as e:
        current_app.logger.error(f"Get product error: {e}")
        return jsonify({'error': 'Failed to get product'}), 500


@products_bp.route('/<int:product_id>/reviews', methods=['GET'])
def get_product_reviews(product_id):
    """Get product reviews with pagination"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 10)), 50)
        sort_by = request.args.get('sort_by', 'created_at')  # created_at, rating, helpful
        sort_order = request.args.get('sort_order', 'desc')
        
        product = Product.query.filter_by(id=product_id, is_active=True).first()
        if not product:
            return jsonify({'error': 'Product not found'}), 404
        
        query = Review.query.filter_by(product_id=product_id, is_approved=True)
        
        # Apply sorting
        if sort_by == 'rating':
            order_field = Review.rating
        elif sort_by == 'helpful':
            order_field = Review.helpful_count
        else:  # default to created_at
            order_field = Review.created_at
        
        if sort_order == 'desc':
            order_field = order_field.desc()
        
        query = query.order_by(order_field)
        
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        return jsonify({
            'reviews': [
                ReviewSerializer(review).to_dict() for review in pagination.items
            ],
            'pagination': {
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            },
            'summary': {
                'average_rating': product.average_rating,
                'total_reviews': product.review_count,
                'rating_distribution': get_analytics_service().get_rating_distribution(product_id)
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get product reviews error: {e}")
        return jsonify({'error': 'Failed to get reviews'}), 500


@products_bp.route('/<int:product_id>/reviews', methods=['POST'])
@jwt_required()
@validate_json(['rating'])
def add_product_review(product_id):
    """Add a product review"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        product = Product.query.filter_by(id=product_id, is_active=True).first()
        if not product:
            return jsonify({'error': 'Product not found'}), 404
        
        # Check if user has purchased this product
        from business_app.models.order import Order, OrderItem
        has_purchased = db.session.query(OrderItem).join(Order).filter(
            Order.user_id == current_user_id,
            OrderItem.product_id == product_id,
            Order.status == 'delivered'
        ).first()
        
        if not has_purchased:
            return jsonify({'error': 'You can only review products you have purchased'}), 403
        
        # Check if user already reviewed this product
        existing_review = Review.query.filter_by(
            user_id=current_user_id, 
            product_id=product_id
        ).first()
        
        if existing_review:
            return jsonify({'error': 'You have already reviewed this product'}), 409
        
        rating = data.get('rating')
        if not isinstance(rating, int) or rating < 1 or rating > 5:
            return jsonify({'error': 'Rating must be between 1 and 5'}), 400
        
        review = Review(
            user_id=current_user_id,
            product_id=product_id,
            order_id=has_purchased.order_id,
            rating=rating,
            title=data.get('title'),
            comment=data.get('comment'),
            photos=data.get('photos', []),
            is_approved=True  # Auto-approve for now, can add moderation later
        )
        
        db.session.add(review)
        
        # Update product rating
        product.update_rating()
        
        db.session.commit()
        
        return jsonify({
            'message': 'Review added successfully',
            'review': ReviewSerializer(review).to_dict()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Add review error: {e}")
        return jsonify({'error': 'Failed to add review'}), 500


@products_bp.route('/featured', methods=['GET'])
@cache_response(600)  # Cache for 10 minutes
def get_featured_products():
    """Get featured products"""
    try:
        language = request.args.get('language', 'uz')
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
        
        products = Product.query.filter_by(
            is_active=True, 
            is_featured=True
        ).order_by(Product.total_sold.desc()).limit(limit).all()
        
        return jsonify({
            'featured_products': [
                ProductSerializer(product).to_dict(
                    language=language, 
                    user=current_user
                ) for product in products
            ]
        })
        
    except Exception as e:
        current_app.logger.error(f"Get featured products error: {e}")
        return jsonify({'error': 'Failed to get featured products'}), 500


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
#                     'product': ProductSerializer(rec['product']).to_dict(
#                         language=language, 
#                         user=current_user
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
        language = request.args.get('language', 'uz')
        limit = min(int(request.args.get('limit', 5)), 10)
        
        if len(query) < 2:
            return jsonify({'suggestions': []})
        
        # Search in product names and categories
        search_term = f"%{query}%"
        
        # Product suggestions
        products = Product.query.filter(
            and_(
                Product.is_active == True,
                or_(
                    Product.name.ilike(search_term),
                    Product.sku.ilike(search_term)
                )
            )
        ).limit(limit).all()
        
        # Category suggestions
        categories = ProductCategory.query.filter(
            and_(
                ProductCategory.is_active == True,
                ProductCategory.name.ilike(search_term)
            )
        ).limit(3).all()
        
        suggestions = []
        
        # Add product suggestions
        for product in products:
            suggestions.append({
                'type': 'product',
                'id': product.id,
                'name': getattr(product, f'name_{language}', product.name) or product.name,
                'image_url': product.image_urls[0] if product.image_urls else None
            })
        
        # Add category suggestions
        for category in categories:
            suggestions.append({
                'type': 'category',
                'id': category.id,
                'name': getattr(category, f'name_{language}', category.name) or category.name,
                'icon_url': category.icon_url
            })
        
        return jsonify({'suggestions': suggestions})
        
    except Exception as e:
        current_app.logger.error(f"Get search suggestions error: {e}")
        return jsonify({'error': 'Failed to get suggestions'}), 500


@products_bp.route('/popular', methods=['GET'])
@cache_response(1800)  # Cache for 30 minutes
def get_popular_products():
    """Get popular products based on sales and views"""
    try:
        language = request.args.get('language', 'uz')
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
        
        # Base query
        query = Product.query.filter_by(is_active=True)
        
        # Apply time period filter for sales data
        if period in ['week', 'month']:
            from business_app.models.order import OrderItem, Order
            from datetime import timedelta
            
            if period == 'week':
                date_threshold = datetime.now(UTC) - timedelta(days=7)
            else:  # month
                date_threshold = datetime.now(UTC) - timedelta(days=30)
            
            # Get products with recent sales
            recent_sales = db.session.query(
                OrderItem.product_id,
                func.sum(OrderItem.quantity).label('recent_sales')
            ).join(Order).filter(
                Order.created_at >= date_threshold,
                Order.status.in_(['confirmed', 'delivered'])
            ).group_by(OrderItem.product_id).subquery()
            
            query = query.outerjoin(
                recent_sales, Product.id == recent_sales.c.product_id
            ).order_by(
                func.coalesce(recent_sales.c.recent_sales, 0).desc(),
                Product.view_count.desc()
            )
        else:
            # All time popularity
            query = query.order_by(
                Product.total_sold.desc(),
                Product.view_count.desc()
            )
        
        products = query.limit(limit).all()
        
        return jsonify({
            'popular_products': [
                ProductSerializer(product).to_dict(
                    language=language, 
                    user=current_user
                ) for product in products
            ],
            'period': period
        })
        
    except Exception as e:
        current_app.logger.error(f"Get popular products error: {e}")
        return jsonify({'error': 'Failed to get popular products'}), 500


@products_bp.route('/price-calculator', methods=['POST'])
@validate_json(['product_id', 'quantity'])
def calculate_price():
    """Calculate product price with discounts"""
    try:
        data = request.get_json()
        product_id = data.get('product_id')
        quantity = data.get('quantity', 1)
        
        product = Product.query.filter_by(id=product_id, is_active=True).first()
        if not product:
            return jsonify({'error': 'Product not found'}), 404
        
        if quantity < 1:
            return jsonify({'error': 'Quantity must be at least 1'}), 400
        
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
        
        # Calculate pricing
        base_price = product.base_price
        unit_price = product.calculate_price(user=current_user, quantity=quantity)
        total_price = unit_price * quantity
        
        # Calculate savings
        base_total = base_price * quantity
        total_savings = base_total - total_price
        
        # Get applicable discounts
        applicable_discounts = []
        
        for rule in product.price_rules:
            if (rule.is_active and 
                quantity >= rule.min_quantity and 
                (not rule.max_quantity or quantity <= rule.max_quantity)):
                
                if rule.rule_type == PriceRuleType.BULK_DISCOUNT:
                    applicable_discounts.append({
                        'type': 'bulk',
                        'name': rule.name,
                        'description': rule.description,
                        'discount_value': rule.discount_value,
                        'discount_type': rule.discount_type
                    })
                elif (rule.rule_type == PriceRuleType.VIP_DISCOUNT and 
                      current_user and current_user.is_vip):
                    applicable_discounts.append({
                        'type': 'vip',
                        'name': rule.name,
                        'description': rule.description,
                        'discount_value': rule.discount_value,
                        'discount_type': rule.discount_type
                    })
        
        return jsonify({
            'product_id': product_id,
            'quantity': quantity,
            'pricing': {
                'base_price': base_price,
                'unit_price': unit_price,
                'total_price': total_price,
                'total_savings': total_savings,
                'savings_percentage': round((total_savings / base_total * 100), 2) if base_total > 0 else 0
            },
            'applicable_discounts': applicable_discounts,
            'is_vip_customer': current_user.is_vip if current_user else False
        })
        
    except Exception as e:
        current_app.logger.error(f"Calculate price error: {e}")
        return jsonify({'error': 'Failed to calculate price'}), 500