"""
Frontend Routes for Blue Stream Water Business Platform
"""
from flask import render_template, request, session, current_app, jsonify, redirect, url_for, flash, g
from flask_jwt_extended import jwt_required, get_jwt_identity, verify_jwt_in_request
from sqlalchemy import desc

from . import frontend_bp
from business_app.models.product import Product, ProductCategory
from business_app.models.order import Order
from business_app.models.subscription import Subscription, SubscriptionPlan
from business_app.models.user import User
from business_app.models.loyalty import LoyaltyPoints, LoyaltyReward
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.services.subscription_service import SubscriptionService
from business_app.utils.translations import get_translation
from business_app.utils.helpers import get_current_language
from business_app import db


@frontend_bp.route('/')
def index():
    """Main homepage using index-4.html template"""
    language = get_current_language()
    
    # Get featured products (defensive)
    try:
        featured_products = Product.query.filter_by(is_featured=True, is_active=True).limit(8).all()
        featured_products = [p.to_dict(language=language) for p in featured_products]
    except Exception as e:
        print(f"Error getting featured products: {e}")
        featured_products = []
    
    # Get product categories (defensive)
    try:
        categories = ProductCategory.query.filter_by(is_active=True).order_by(ProductCategory.sort_order).all()
        categories = [c.to_dict(language=language) for c in categories]
    except Exception as e:
        print(f"Error getting categories: {e}")
        categories = []
    
    # Get subscription plans (defensive)
    try:
        subscription_plans = SubscriptionPlan.query.filter_by(is_active=True).order_by(SubscriptionPlan.sort_order).limit(3).all()
        subscription_plans = [sp.to_dict(language=language) for sp in subscription_plans]
    except Exception as e:
        print(f"Error getting subscription plans: {e}")
        subscription_plans = []
    
    # Get loyalty rewards (defensive)
    try:
        featured_rewards = LoyaltyReward.query.filter_by(is_active=True, is_featured=True).limit(4).all()
        featured_rewards = [fr.to_dict(language=language) for fr in featured_rewards]
    except Exception as e:
        print(f"Error getting loyalty rewards: {e}")
        featured_rewards = []
    
    # Get user info if logged in
    user_data = None
    loyalty_data = None
    try:
        verify_jwt_in_request(optional=True)
        current_user_id = get_jwt_identity()
        if current_user_id:
            user = User.query.get(current_user_id)
            if user:
                user_data = {
                    'id': user.id,
                    'name': f"{user.first_name} {user.last_name}",
                    'email': user.email
                }
                
                # Get loyalty info
                loyalty_service = LoyaltyService()
                loyalty_points = loyalty_service.get_or_create_loyalty_account(current_user_id)
                loyalty_data = {
                    'points_balance': loyalty_points.current_balance,
                    'current_tier': loyalty_points.current_tier
                }
    except:
        pass  # User not logged in or token invalid
    
    return render_template('frontend/index.html',
                         featured_products=featured_products,
                         categories=categories,
                         subscription_plans=subscription_plans,
                         featured_rewards=featured_rewards,
                         user_data=user_data,
                         loyalty_data=loyalty_data)


@frontend_bp.route('/shop')
def shop():
    """Shop page for browsing products"""
    language = get_current_language()
    page = request.args.get('page', 1, type=int)
    category_id = request.args.get('category', type=int)
    search = request.args.get('search', '')
    
    # Build query
    query = Product.query.filter_by(is_active=True)
    
    if category_id:
        query = query.filter_by(category_id=category_id)
    
    if search:
        query = query.filter(Product.name.contains(search))
    
    # Paginate products
    products_pagination = query.order_by(Product.created_at.desc()).paginate(
        page=page, per_page=12, error_out=False
    )
    
    # Convert products to multilingual dict
    products_pagination.items = [p.to_dict(language=language) for p in products_pagination.items]
    
    # Get categories for filter
    categories = ProductCategory.query.filter_by(is_active=True).all()
    categories = [c.to_dict(language=language) for c in categories]
    
    return render_template('frontend/shop.html',
                         products=products_pagination,
                         categories=categories,
                         current_category=category_id,
                         search_query=search)


@frontend_bp.route('/product/<int:product_id>')
def product_detail(product_id):
    """Product detail page"""
    language = get_current_language()
    product = Product.query.get_or_404(product_id)
    
    # Get related products
    related_products = Product.query.filter(
        Product.category_id == product.category_id,
        Product.id != product.id,
        Product.is_active == True
    ).limit(4).all()
    
    return render_template('frontend/product_detail.html',
                         product=product.to_dict(language=language),
                         related_products=[rp.to_dict(language=language) for rp in related_products])


@frontend_bp.route('/cart')
def cart():
    """Shopping cart page"""
    return render_template('frontend/cart.html')


@frontend_bp.route('/checkout')
@jwt_required()
def checkout():
    """Checkout page"""
    current_user_id = get_jwt_identity()
    user = User.query.get(current_user_id)
    
    # Get user addresses
    addresses = user.addresses if user else []
    
    return render_template('frontend/checkout.html',
                         user=user,
                         addresses=addresses)


@frontend_bp.route('/subscriptions')
def subscriptions():
    """Subscription plans page"""
    language = get_current_language()
    plans = SubscriptionPlan.query.filter_by(is_active=True).order_by(SubscriptionPlan.sort_order).all()
    plans = [plan.to_dict(language=language) for plan in plans]
    
    return render_template('frontend/subscriptions.html', plans=plans)


@frontend_bp.route('/my-subscriptions')
@jwt_required()
def my_subscriptions():
    """User subscriptions management page"""
    current_user_id = get_jwt_identity()
    user = User.query.get(current_user_id)
    
    # Get user's subscriptions
    subscriptions = Subscription.query.filter_by(user_id=current_user_id).order_by(
        desc(Subscription.created_at)
    ).all()
    
    # Get active subscriptions count
    active_subscriptions = [s for s in subscriptions if s.status == 'active']
    
    # Get subscription overview data
    subscription_service = SubscriptionService()
    try:
        overview_data = {
            'active_count': len(active_subscriptions),
            'deliveries_this_month': subscription_service.get_monthly_deliveries_count(current_user_id),
            'monthly_savings': subscription_service.calculate_monthly_savings(current_user_id)
        }
    except Exception as e:
        print(f"Error getting subscription overview: {e}")
        overview_data = {
            'active_count': len(active_subscriptions),
            'deliveries_this_month': 0,
            'monthly_savings': 0
        }
    
    return render_template('frontend/my_subscriptions.html',
                         user=user,
                         subscriptions=subscriptions,
                         active_subscriptions=active_subscriptions,
                         overview_data=overview_data)


@frontend_bp.route('/my-account')
@jwt_required()
def my_account():
    """User account dashboard"""
    current_user_id = get_jwt_identity()
    user = User.query.get(current_user_id)
    
    # Get recent orders
    recent_orders = Order.query.filter_by(user_id=current_user_id).order_by(
        desc(Order.created_at)
    ).limit(5).all()
    
    # Get active subscriptions
    active_subscriptions = Subscription.query.filter_by(
        user_id=current_user_id,
        status='active'
    ).all()
    
    # Get loyalty info
    loyalty_service = LoyaltyService()
    loyalty_account = loyalty_service.get_or_create_loyalty_account(current_user_id)
    try:
        loyalty_stats = loyalty_service.get_user_tier_info(current_user_id)
    except Exception as e:
        print(f"Error getting loyalty stats: {e}")
        loyalty_stats = {
            'current_tier': 'Bronze',
            'points_balance': 0,
            'lifetime_points_earned': 0,
            'tier_benefits': {},
            'next_tier': None,
            'referral_code': f'REF{current_user_id}',
            'referrals_count': 0
        }
    
    return render_template('frontend/my_account.html',
                         user=user,
                         recent_orders=recent_orders,
                         active_subscriptions=active_subscriptions,
                         loyalty_account=loyalty_account,
                         loyalty_stats=loyalty_stats)


@frontend_bp.route('/my-loyalty')
@jwt_required()
def my_loyalty():
    """User loyalty program page"""
    current_user_id = get_jwt_identity()
    user = User.query.get(current_user_id)
    
    loyalty_service = LoyaltyService()
    
    # Get loyalty account info
    loyalty_account = loyalty_service.get_or_create_loyalty_account(current_user_id)
    tier_info = loyalty_service.get_user_tier_info(current_user_id)
    
    # Get available rewards
    available_rewards = LoyaltyReward.query.filter_by(is_active=True).all()
    available_rewards = [reward.to_dict(language=get_current_language()) for reward in available_rewards]
    
    # Get recent transactions
    transactions = loyalty_service.get_loyalty_history(current_user_id, page=1, per_page=10)
    
    return render_template('frontend/loyalty.html',
                         user=user,
                         loyalty_account=loyalty_account,
                         tier_info=tier_info,
                         available_rewards=available_rewards,
                         transactions=transactions)


@frontend_bp.route('/my-orders')
@jwt_required()
def my_orders():
    """User orders page"""
    current_user_id = get_jwt_identity()
    user = User.query.get(current_user_id)
    page = request.args.get('page', 1, type=int)
    
    orders = Order.query.filter_by(user_id=current_user_id).order_by(
        desc(Order.created_at)
    ).paginate(page=page, per_page=10, error_out=False)
    
    return render_template('frontend/orders.html', user=user, orders=orders)


@frontend_bp.route('/order/<int:order_id>')
@jwt_required()
def order_detail(order_id):
    """Order detail page"""
    current_user_id = get_jwt_identity()
    order = Order.query.filter_by(id=order_id, user_id=current_user_id).first_or_404()
    
    return render_template('frontend/order_detail.html', order=order)


@frontend_bp.route('/about')
def about():
    """About us page"""
    return render_template('frontend/about.html')


@frontend_bp.route('/contact')
def contact():
    """Contact page"""
    return render_template('frontend/contact.html')


@frontend_bp.route('/services')
def services():
    """Services page"""
    return render_template('frontend/services.html')


@frontend_bp.route('/gallery')
def gallery():
    """Gallery page"""
    return render_template('frontend/gallery.html')


@frontend_bp.route('/login')
def login():
    """Login page"""
    # If user is already logged in, redirect to account
    try:
        verify_jwt_in_request(optional=True)
        if get_jwt_identity():
            return redirect(url_for('frontend.my_account'))
    except:
        pass
    
    return render_template('frontend/login.html')


@frontend_bp.route('/register')
def register():
    """Registration page"""
    # If user is already logged in, redirect to account
    try:
        verify_jwt_in_request(optional=True)
        if get_jwt_identity():
            return redirect(url_for('frontend.my_account'))
    except:
        pass
    
    return render_template('frontend/register.html')


@frontend_bp.route('/verify-email')
def verify_email():
    """Email verification page"""
    return render_template('frontend/verify_email.html')


@frontend_bp.route('/verify-phone')
def verify_phone():
    """Phone verification page"""
    return render_template('frontend/verify_phone.html')


@frontend_bp.route('/forgot-password')
def forgot_password():
    """Forgot password page"""
    return render_template('frontend/forgot_password.html')


@frontend_bp.route('/reset-password')
@frontend_bp.route('/reset-password/<token>')
def reset_password(token=None):
    """Reset password page"""
    return render_template('frontend/reset_password.html', token=token)


@frontend_bp.route('/profile-settings')
@jwt_required()
def profile_settings():
    """Profile settings page"""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    return render_template('frontend/profile_settings.html', user=user)


@frontend_bp.route('/account-security')
@jwt_required()
def account_security():
    """Account security page"""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    return render_template('frontend/account_security.html', user=user)


@frontend_bp.route('/addresses')
@jwt_required()
def addresses():
    """User addresses management page"""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    # TODO: Get user addresses when address management is implemented
    addresses = []
    return render_template('frontend/addresses.html', user=user, addresses=addresses)


# Language switching
@frontend_bp.route('/set-language/<language>')
def set_language_route(language):
    """Set user language preference via URL redirect"""
    print(f"🌟 SET_LANG: Switching to '{language}'", flush=True)
    
    # Validate language
    if language not in current_app.config['LANGUAGES']:
        language = current_app.config['DEFAULT_LANGUAGE']
    
    session['language'] = language

    # Store in user profile if logged in
    try:
        verify_jwt_in_request(optional=True)
        current_user_id = get_jwt_identity()
        if current_user_id:
            user = User.query.get(current_user_id)
            if user:
                user.preferred_language = language
                db.session.commit()
    except Exception as exc:
        pass
    
    return redirect(request.referrer or url_for('frontend.index'))


# Context processor for global template variables
@frontend_bp.context_processor
def inject_global_vars():
    """Inject global variables into all templates"""
    from datetime import datetime
    
    class MomentJS:
        def format(self, fmt):
            if fmt == 'YYYY':
                return datetime.now().year
            return datetime.now().strftime(fmt)
    
    # Get current language and set in g object for template filters
    from business_app.utils.helpers import get_current_language
    language = get_current_language()
    
    # Set language in g object so template filters can use it
    g.language = language
    
    # Debug logging
    if current_app.debug:
        current_app.logger.debug(f"Context processor - current language: {language}")
    
    # Get categories for navigation
    categories = ProductCategory.query.filter_by(is_active=True).order_by(ProductCategory.sort_order).all()
    categories = [cat.to_dict(language=language) for cat in categories]
    
    # Get user info if logged in
    user_info = None
    try:
        verify_jwt_in_request(optional=True)
        current_user_id = get_jwt_identity()
        if current_user_id:
            user = User.query.get(current_user_id)
            if user:
                user_info = {
                    'id': user.id,
                    'name': f"{user.first_name} {user.last_name}",
                    'email': user.email
                }
    except:
        pass
    
    return {
        'current_language': language,
        'nav_categories': categories,
        'current_user': user_info,
        'company_name': 'Blue Stream Group',
        'company_phone': '+998 90 915 0172',
        'company_email': 'info@bluestream.uz',
        'moment': lambda: MomentJS(),
        'min': min,
        'max': max
    }


# Custom template filters


@frontend_bp.app_template_filter('format_price')
def format_price(amount):
    """Format price with proper formatting"""
    return f"{amount:,.0f} UZS"


@frontend_bp.route('/docs/api')
def api_documentation():
    """Serve API documentation"""
    import os
    from pathlib import Path
    
    docs_path = Path(__file__).parent.parent.parent / 'docs' / 'API_DOCUMENTATION.md'
    
    if docs_path.exists():
        with open(docs_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Convert markdown to HTML for better display
        try:
            import markdown
            html_content = markdown.markdown(content, extensions=['codehilite', 'tables', 'toc'])
        except ImportError:
            # Fallback to plain text if markdown not available
            html_content = f"<pre>{content}</pre>"
        
        return render_template('frontend/api_docs.html', content=html_content)
    else:
        return "API Documentation not found", 404