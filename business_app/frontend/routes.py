"""Frontend routes for Blue Stream Water Business Platform."""
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from xml.sax.saxutils import escape

from flask import render_template, request, session, current_app, jsonify, redirect, url_for, flash, g, Response
from flask_jwt_extended import jwt_required, get_jwt_identity, verify_jwt_in_request
from sqlalchemy import desc, or_

from . import frontend_bp
from business_app.models.product import Product, ProductCategory
from business_app.models.order import Order
from business_app.models.subscription import Subscription
from business_app.models.user import User
from business_app.models.loyalty import LoyaltyPoints, LoyaltyReward
from business_app.models.blog import BlogPost, BlogStatus, BlogCategory
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.services.subscription_service import SubscriptionService
from business_app.utils.translations import get_translation
from business_app.utils.helpers import get_current_language
from business_app import db
from datetime import datetime, UTC


def _absolute_public_url(path):
    """Build absolute URL respecting reverse-proxy scheme headers."""
    if not path.startswith('/'):
        path = f'/{path}'
    scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
    return f"{scheme}://{request.host}{path}"


def _current_lang_query_param():
    """Return current valid `lang` query param if present."""
    lang = request.args.get('lang')
    supported_languages = current_app.config.get('LANGUAGES', {})
    if lang and lang in supported_languages:
        return lang
    return None


def _build_external_url(endpoint, **params):
    """Build external URL preserving only explicit non-default lang query params."""
    lang = _current_lang_query_param()
    default_language = current_app.config.get('DEFAULT_LANGUAGE', 'uz')
    if lang and lang != default_language and 'lang' not in params:
        params['lang'] = lang
    return url_for(endpoint, _external=True, **params)


def _default_canonical_url():
    """Build a default canonical URL for the current request path."""
    canonical = _absolute_public_url(request.path)
    lang = _current_lang_query_param()
    default_language = current_app.config.get('DEFAULT_LANGUAGE', 'uz')
    if lang and lang != default_language:
        canonical = f"{canonical}?{urlencode({'lang': lang})}"
    return canonical


def _format_lastmod(value):
    """Format datetime/date values for sitemap lastmod tags."""
    if not value:
        return None
    try:
        if hasattr(value, 'date'):
            return value.date().isoformat()
        return str(value)
    except Exception:
        return None


def _as_absolute_url(url_value):
    """Convert relative URLs to absolute public URLs."""
    if not url_value:
        return None
    if url_value.startswith('http://') or url_value.startswith('https://'):
        return url_value
    path = url_value if url_value.startswith('/') else f'/{url_value}'
    return _absolute_public_url(path)


def _normalize_feed_gtin(value):
    """Return GTIN if it matches allowed lengths, else None."""
    if value is None:
        return None
    digits_only = ''.join(ch for ch in str(value) if ch.isdigit())
    if len(digits_only) in {8, 12, 13, 14}:
        return digits_only
    return None


def _format_feed_price(value):
    """Return a Google feed-compatible price string with 2 decimals."""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _render_sitemap_urlset(entries):
    """Render a sitemap <urlset> response."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for entry in entries:
        lines.append('<url>')
        lines.append(f"<loc>{escape(entry['loc'])}</loc>")
        if entry.get('lastmod'):
            lines.append(f"<lastmod>{escape(entry['lastmod'])}</lastmod>")
        if entry.get('changefreq'):
            lines.append(f"<changefreq>{escape(entry['changefreq'])}</changefreq>")
        if entry.get('priority') is not None:
            lines.append(f"<priority>{entry['priority']}</priority>")
        lines.append('</url>')
    lines.append('</urlset>')

    return Response('\n'.join(lines), mimetype='application/xml')


def _render_sitemap_index(entries):
    """Render a sitemap index response."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for entry in entries:
        lines.append('<sitemap>')
        lines.append(f"<loc>{escape(entry['loc'])}</loc>")
        if entry.get('lastmod'):
            lines.append(f"<lastmod>{escape(entry['lastmod'])}</lastmod>")
        lines.append('</sitemap>')
    lines.append('</sitemapindex>')

    return Response('\n'.join(lines), mimetype='application/xml')


def _render_product_detail_page(product, language):
    """Render product detail template with consistent related products logic."""
    related_products = Product.query.filter(
        Product.category_id == product.category_id,
        Product.id != product.id,
        Product.is_active == True
    ).limit(4).all()

    canonical_endpoint = 'frontend.product_detail_slug' if product.slug else 'frontend.product_detail'
    canonical_params = {'slug': product.slug} if product.slug else {'product_id': product.id}

    return render_template(
        'frontend/product_detail.html',
        product=product.to_dict(language=language),
        related_products=[rp.to_dict(language=language) for rp in related_products],
        canonical_url=_build_external_url(canonical_endpoint, **canonical_params)
    )


@frontend_bp.route('/')
def index():
    """Main homepage using index-4.html template"""
    language = get_current_language()
    request_id = getattr(g, 'request_id', 'N/A')
    
    # AGGRESSIVE DEBUG LOGGING
    current_app.logger.info(f"")
    current_app.logger.info(f"[INDEX-DEBUG] [REQ:{request_id}] ========== index() ROUTE START ==========")
    current_app.logger.info(f"[INDEX-DEBUG] [REQ:{request_id}] get_current_language() returned: '{language}'")
    current_app.logger.info(f"[INDEX-DEBUG] [REQ:{request_id}] g.language: '{getattr(g, 'language', None)}'")
    current_app.logger.info(f"[INDEX-DEBUG] [REQ:{request_id}] session.get('language'): '{session.get('language')}'")
    
    # Get featured products (defensive)
    try:
        featured_products = Product.query.filter_by(is_featured=True, is_active=True).limit(8).all()
        featured_products = [p.to_dict(language=language) for p in featured_products]
        current_app.logger.info(f"[INDEX-DEBUG] [REQ:{request_id}] Fetched {len(featured_products)} featured products with language='{language}'")
    except Exception as e:
        print(f"Error getting featured products: {e}")
        featured_products = []
    
    # Get product categories (defensive)
    try:
        categories = ProductCategory.query.filter_by(is_active=True).order_by(ProductCategory.sort_order).all()
        categories = [c.to_dict(language=language) for c in categories]
        current_app.logger.info(f"[INDEX-DEBUG] [REQ:{request_id}] Fetched {len(categories)} categories with language='{language}'")
    except Exception as e:
        print(f"Error getting categories: {e}")
        categories = []
    
    # Subscription plans removed - users create custom subscriptions
    subscription_plans = []
    
    # Get loyalty rewards (defensive)
    try:
        featured_rewards = LoyaltyReward.query.filter_by(is_active=True, is_featured=True).limit(4).all()
        featured_rewards = [fr.to_dict(language=language) for fr in featured_rewards]
    except Exception as e:
        print(f"Error getting loyalty rewards: {e}")
        featured_rewards = []

    # Get featured blog posts (defensive)
    try:
        featured_posts = BlogPost.query.filter(
            BlogPost.status == BlogStatus.PUBLISHED.value,
            BlogPost.is_featured == True,
            BlogPost.published_at <= datetime.now(UTC)
        ).order_by(desc(BlogPost.sort_order), desc(BlogPost.published_at)).limit(3).all()
    except Exception as e:
        current_app.logger.error(f"Error getting featured blog posts: {e}")
        featured_posts = []

    current_app.logger.info(f"featured_posts: {[post.to_dict() for post in featured_posts]}")

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
    
    current_app.logger.info(f"[INDEX-DEBUG] [REQ:{request_id}] Rendering template with language='{language}'")
    current_app.logger.info(f"[INDEX-DEBUG] [REQ:{request_id}] ========== index() ROUTE END ==========")
    
    print(f"!!!! RENDER_TEMPLATE ABOUT TO BE CALLED for {request_id} with language: {language}")
    
    return render_template('frontend/index.html',
                         featured_products=featured_products,
                         categories=categories,
                         subscription_plans=subscription_plans,
                         featured_rewards=featured_rewards,
                         featured_posts=featured_posts,
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
    
    base_listing_params = {}
    if category_id:
        base_listing_params['category'] = category_id
    if search:
        base_listing_params['search'] = search

    canonical_params = {}
    if category_id:
        canonical_params['category'] = category_id
    if page > 1:
        canonical_params['page'] = page

    # Internal search result pages should generally not be indexed.
    meta_robots = None
    if search:
        meta_robots = 'noindex,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1'

    prev_page_url = None
    next_page_url = None
    if products_pagination.has_prev:
        prev_page_url = _build_external_url('frontend.shop', page=products_pagination.prev_num, **base_listing_params)
    if products_pagination.has_next:
        next_page_url = _build_external_url('frontend.shop', page=products_pagination.next_num, **base_listing_params)

    return render_template(
        'frontend/shop.html',
        products=products_pagination,
        categories=categories,
        current_category=category_id,
        search_query=search,
        canonical_url=_build_external_url('frontend.shop', **canonical_params),
        meta_robots=meta_robots,
        prev_page_url=prev_page_url,
        next_page_url=next_page_url
    )


@frontend_bp.route('/product/<int:product_id>')
def product_detail(product_id):
    """Legacy product detail route using ID, kept for backward compatibility."""
    language = get_current_language()
    product = Product.query.get_or_404(product_id)
    if product.slug:
        return redirect(url_for('frontend.product_detail_slug', slug=product.slug), code=301)
    return _render_product_detail_page(product, language)


@frontend_bp.route('/product/<slug>')
def product_detail_slug(slug):
    """Primary SEO-friendly product detail route."""
    language = get_current_language()
    product = Product.query.filter_by(slug=slug).first_or_404()
    return _render_product_detail_page(product, language)


@frontend_bp.route('/cart')
def cart():
    """Shopping cart page"""
    return render_template('frontend/cart.html')


@frontend_bp.route('/checkout')
def checkout():
    """Checkout page"""
    # Check authentication
    try:
        verify_jwt_in_request(optional=True)
        current_user_id = get_jwt_identity()
        if not current_user_id:
            return redirect(url_for('frontend.login', next=request.path))
    except Exception:
        return redirect(url_for('frontend.login', next=request.path))

    user = User.query.get(current_user_id)

    # Get user addresses
    addresses = user.addresses if user else []

    # Get today's date for date picker
    today = datetime.now(UTC).strftime('%Y-%m-%d')

    return render_template('frontend/checkout.html',
                         user=user,
                         addresses=addresses,
                         today=today)


@frontend_bp.route('/order-confirmation')
@jwt_required()
def order_confirmation():
    """Order confirmation page"""
    from business_app.models.order import Order
    from business_app.models.delivery import Delivery

    current_user_id = get_jwt_identity()
    order_id = request.args.get('order_id', type=int)

    if not order_id:
        flash('Order not found', 'error')
        return redirect(url_for('frontend.shop'))

    # Get order with items
    order = Order.query.filter_by(id=order_id, user_id=current_user_id).first()

    if not order:
        flash('Order not found', 'error')
        return redirect(url_for('frontend.my_orders'))

    # Get delivery information
    delivery = Delivery.query.filter_by(order_id=order.id).first()

    return render_template('frontend/order_confirmation.html',
                         order=order,
                         delivery=delivery)


@frontend_bp.route('/payment/success')
@jwt_required()
def payment_success():
    """Payment success callback page"""
    from business_app.models.order import Order
    from business_app.models.payment import Payment

    current_user_id = get_jwt_identity()

    # Get order reference from query params
    order_id = request.args.get('order_id', type=int)

    if order_id:
        # Verify order belongs to user
        order = Order.query.filter_by(id=order_id, user_id=current_user_id).first()

        if order:
            # Check if there's a pending payment for this order
            payment = Payment.query.filter_by(order_id=order.id).order_by(
                desc(Payment.created_at)
            ).first()

            # If payment is still pending, show a waiting page
            # The webhook will update the payment status asynchronously
            if payment and payment.status.value == 'pending':
                return render_template('frontend/payment_pending.html',
                                     order=order,
                                     payment=payment)

            # If payment completed, redirect to order confirmation
            if payment and payment.status.value == 'completed':
                flash('Payment successful! Your order has been confirmed.', 'success')
                return redirect(url_for('frontend.order_confirmation', order_id=order.id))

    flash('Payment information not found. Please check your order status.', 'warning')
    return redirect(url_for('frontend.my_orders'))


@frontend_bp.route('/payment/cancel')
@jwt_required()
def payment_cancel():
    """Payment cancellation callback page"""
    from business_app.models.order import Order

    current_user_id = get_jwt_identity()
    order_id = request.args.get('order_id', type=int)

    if order_id:
        order = Order.query.filter_by(id=order_id, user_id=current_user_id).first()

        if order:
            # Show payment cancelled message
            flash('Payment was cancelled. You can try again or choose a different payment method.', 'warning')
            return render_template('frontend/payment_cancelled.html',
                                 order=order)

    flash('Order not found', 'error')
    return redirect(url_for('frontend.cart'))


@frontend_bp.route('/order-tracking')
@jwt_required()
def order_tracking():
    """Order tracking page with real-time status"""
    from business_app.models.order import Order
    from business_app.models.delivery import Delivery

    current_user_id = get_jwt_identity()
    order_id = request.args.get('order_id', type=int)

    if not order_id:
        flash('Order not found', 'error')
        return redirect(url_for('frontend.my_orders'))

    # Get order with items
    order = Order.query.filter_by(id=order_id, user_id=current_user_id).first()

    if not order:
        flash('Order not found or access denied', 'error')
        return redirect(url_for('frontend.my_orders'))

    # Get delivery information
    delivery = Delivery.query.filter_by(order_id=order.id).first()

    # Get map provider from config
    map_provider = current_app.config.get('MAPS_PROVIDER', 'google')
    maps_api_key = current_app.config.get('GOOGLE_MAPS_API_KEY', '')

    return render_template('frontend/order_tracking.html',
                         order=order,
                         delivery=delivery,
                         map_provider=map_provider,
                         maps_api_key=maps_api_key)


@frontend_bp.route('/subscriptions')
def subscriptions():
    """Subscription constructor page - users create custom subscriptions"""
    language = get_current_language()

    # Get active products for subscription constructor
    products = Product.query.filter_by(is_active=True).all()
    products = [p.to_dict(language=language) for p in products]

    return render_template('frontend/subscriptions.html', products=products)


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
    from business_app.utils.constants import OrderStatus
    
    current_user_id = get_jwt_identity()
    user = User.query.get(current_user_id)
    page = request.args.get('page', 1, type=int)
    
    orders = Order.query.filter_by(user_id=current_user_id).order_by(
        desc(Order.created_at)
    ).paginate(page=page, per_page=10, error_out=False)
    
    # Build order statuses list from enum (single source of truth)
    order_statuses = [
        {'value': status.value, 'label': status.value.replace('_', ' ').title()}
        for status in OrderStatus
    ]
    
    return render_template('frontend/orders.html', user=user, orders=orders, order_statuses=order_statuses)


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


@frontend_bp.route('/terms')
def terms():
    """Terms and Conditions page"""
    return render_template('frontend/terms.html')


@frontend_bp.route('/privacy')
def privacy():
    """Privacy Policy page"""
    return render_template('frontend/privacy.html')


@frontend_bp.route('/delivery-policy')
def delivery_policy():
    """Delivery policy page."""
    return render_template('frontend/delivery_policy.html')


@frontend_bp.route('/pricing-policy')
def pricing_policy():
    """Pricing policy page."""
    return render_template('frontend/pricing_policy.html')


@frontend_bp.route('/refund-policy')
def refund_policy():
    """Refund and return policy page."""
    return render_template('frontend/refund_policy.html')


@frontend_bp.route('/quality-standards')
def quality_standards():
    """Quality standards page."""
    return render_template('frontend/quality_standards.html')


@frontend_bp.route('/water-delivery-faq')
def water_delivery_faq():
    """Public FAQ page for delivery and product buying questions."""
    return render_template('frontend/water_delivery_faq.html')


@frontend_bp.route('/login')
def login():
    """Login page"""
    # If user is already logged in, redirect to next URL or account
    try:
        verify_jwt_in_request(optional=True)
        if get_jwt_identity():
            # Respect the next parameter if provided
            next_url = request.args.get('next')
            if next_url and next_url.startswith('/') and not next_url.startswith('//'):
                return redirect(next_url)
            return redirect(url_for('frontend.my_account'))
    except:
        pass
    
    return render_template('frontend/login.html')


@frontend_bp.route('/register')
def register():
    """Registration page"""
    # If user is already logged in, redirect to next URL or account
    try:
        verify_jwt_in_request(optional=True)
        if get_jwt_identity():
            # Respect the next parameter if provided
            next_url = request.args.get('next')
            if next_url and next_url.startswith('/') and not next_url.startswith('//'):
                return redirect(next_url)
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
    """Set user language preference via URL redirect

    This endpoint:
    1. Validates the language code
    2. Stores in session (immediate effect, highest priority after URL param)
    3. Persists to user's DB profile if logged in (for cross-session persistence)
    4. Redirects back to the referring page
    """
    import os

    # Validate language
    if language not in current_app.config['LANGUAGES']:
        language = current_app.config['DEFAULT_LANGUAGE']

    # Store in session (this is checked BEFORE DB preference in before_request)
    session['language'] = language
    session.permanent = True  # Persist session across browser sessions
    session.modified = True  # Ensure session is marked as modified
    g.language = language  # Also set in g for immediate use in this request

    # Also persist to user profile if logged in (for cross-session persistence)
    try:
        verify_jwt_in_request(optional=True)
        current_user_id = get_jwt_identity()
        if current_user_id:
            user = User.query.get(current_user_id)
            if user:
                old_pref = user.preferred_language
                user.preferred_language = language
                db.session.commit()
    except Exception as exc:
        # Ensure we don't leave a broken transaction
        db.session.rollback()

    redirect_url = request.referrer or url_for('frontend.index')

    # Keep language explicit in URL to avoid language drift when session
    # cookies are unavailable or inconsistent across hosts/proxies.
    try:
        parsed = urlsplit(redirect_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query['lang'] = language
        redirect_url = urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment
        ))
    except Exception:
        # Keep original fallback redirect URL if parsing fails for any reason.
        pass

    resp = redirect(redirect_url)
    
    # Nuclear Option: Explicitly unset potential conflicting cookies
    # If there were old cookies with different names (session vs __Secure-session)
    # or domains, they might be causing the toggling.
    
    current_cookie_name = current_app.config.get('SESSION_COOKIE_NAME', 'session')
    
    # List of cookies to kill: defaults that might be lingering
    cookies_to_kill = ['session', '__Secure-session', 'remembe_token']
    
    # Don't kill the one we are actually using!
    if current_cookie_name in cookies_to_kill:
        cookies_to_kill.remove(current_cookie_name)
        
    # Domains to attempt cleaning on
    domains = [
        None, # Host only
        '.bluestream.uz',
        'bluestream.uz',
        '.localhost',
        'localhost'
    ]
    
    for cookie_name in cookies_to_kill:
        for domain in domains:
            try:
                resp.delete_cookie(cookie_name, domain=domain, path='/')
            except:
                pass

    return resp


# Context processor for global template variables
@frontend_bp.context_processor
def inject_global_vars():
    """Inject global variables into all templates"""
    from flask import g

    class MomentJS:
        def format(self, fmt):
            if fmt == 'YYYY':
                return datetime.now(UTC).year
            return datetime.now(UTC).strftime(fmt)

    # Get current language
    from business_app.utils.helpers import get_current_language
    language = get_current_language()
    request_id = getattr(g, 'request_id', 'N/A')

    # DEBUG: Always log context processor language
    current_app.logger.info(f"[CONTEXT-DEBUG] [REQ:{request_id}] inject_global_vars() called")
    current_app.logger.info(f"[CONTEXT-DEBUG] [REQ:{request_id}] g.language: '{getattr(g, 'language', None)}'")
    current_app.logger.info(f"[CONTEXT-DEBUG] [REQ:{request_id}] get_current_language() returned: '{language}'")
    current_app.logger.info(f"[CONTEXT-DEBUG] [REQ:{request_id}] session.get('language'): '{session.get('language')}'")
    current_app.logger.info(f"[CONTEXT-DEBUG] [REQ:{request_id}] Returning current_language='{language}' to template")
    
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
    
    # Determine base URLs for cross-domain navigation
    # When on cabinet subdomain, nav links should go to main site
    host = request.host.lower() if request.host else ''
    is_cabinet_subdomain = host.startswith('cabinet.')
    is_admin_subdomain = host.startswith('admin.')

    # Check if we're in local development (localhost or 127.0.0.1)
    is_local_dev = 'localhost' in host or '127.0.0.1' in host

    # Use HTTPS for production, check scheme from X-Forwarded-Proto
    scheme = request.headers.get('X-Forwarded-Proto', 'https')

    if is_local_dev:
        # In local development, use relative URLs (empty string)
        main_site_url = ''
        cabinet_site_url = ''
    else:
        # In production, use absolute URLs for cross-subdomain navigation
        main_site_url = f"{scheme}://bluestream.uz"
        cabinet_site_url = f"{scheme}://cabinet.bluestream.uz"

    supported_languages = list(current_app.config['LANGUAGES'].keys())
    default_language = current_app.config.get('DEFAULT_LANGUAGE', 'uz')
    default_meta_description = (
        "Blue Stream Group - Premium drinking water delivery, subscriptions, and water products."
    )
    noindex_endpoints = {
        'frontend.login',
        'frontend.register',
        'frontend.verify_email',
        'frontend.verify_phone',
        'frontend.forgot_password',
        'frontend.reset_password',
        'frontend.checkout',
        'frontend.order_confirmation',
        'frontend.payment_success',
        'frontend.payment_cancel',
        'frontend.order_tracking',
        'frontend.order_detail',
        'frontend.my_account',
        'frontend.my_orders',
        'frontend.my_subscriptions',
        'frontend.my_loyalty',
        'frontend.addresses',
        'frontend.profile_settings',
        'frontend.account_security',
        'frontend.cart',
    }
    default_meta_robots = None
    if request.endpoint in noindex_endpoints:
        default_meta_robots = 'noindex,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1'

    def localized_url_for_lang(language_code):
        """Build current page URL with an explicit `lang` query param for hreflang tags."""
        if language_code not in current_app.config['LANGUAGES']:
            language_code = default_language

        query_args = request.args.to_dict(flat=False)
        query_args['lang'] = [language_code]

        try:
            if request.endpoint:
                base_url = url_for(request.endpoint, _external=True, **(request.view_args or {}))
            else:
                base_url = _absolute_public_url(request.path)
        except Exception:
            base_url = _absolute_public_url(request.path)

        query_string = urlencode(query_args, doseq=True)
        if query_string:
            return f"{base_url}?{query_string}"
        return base_url

    def external_url_for_lang(endpoint, **kwargs):
        """Build external endpoint URL preserving current valid language query param."""
        return _build_external_url(endpoint, **kwargs)
    
    return {
        'current_language': language,
        'supported_languages': supported_languages,
        'default_language': default_language,
        'default_meta_description': default_meta_description,
        'default_canonical_url': _default_canonical_url(),
        'default_meta_robots': default_meta_robots,
        'localized_url_for_lang': localized_url_for_lang,
        'external_url_for_lang': external_url_for_lang,
        'nav_categories': categories,
        'current_user': user_info,
        'company_name': 'Blue Stream Group',
        'company_phone': '+998 94 524 4680',
        'company_email': 'info@bluestream.uz',
        'moment': lambda: MomentJS(),
        'min': min,
        'max': max,
        # Cross-domain navigation
        'main_site_url': main_site_url,
        'cabinet_site_url': cabinet_site_url,
        'is_cabinet_subdomain': is_cabinet_subdomain,
        'is_admin_subdomain': is_admin_subdomain
    }


# Sitemap routes


@frontend_bp.route('/sitemap.xml')
def sitemap_index():
    """Sitemap index that points to segmented sitemap files."""
    now = _format_lastmod(datetime.now(UTC))
    entries = [
        {'loc': _absolute_public_url('/sitemap-static.xml'), 'lastmod': now},
        {'loc': _absolute_public_url('/sitemap-products.xml'), 'lastmod': now},
        {'loc': _absolute_public_url('/sitemap-blog.xml'), 'lastmod': now},
    ]
    return _render_sitemap_index(entries)


@frontend_bp.route('/sitemap-static.xml')
def sitemap_static():
    """Sitemap for static/public marketing pages."""
    now = _format_lastmod(datetime.now(UTC))
    paths = [
        '/',
        '/shop',
        '/subscriptions',
        '/services',
        '/about',
        '/contact',
        '/gallery',
        '/blog',
        '/terms',
        '/privacy',
        '/delivery-policy',
        '/pricing-policy',
        '/refund-policy',
        '/quality-standards',
        '/water-delivery-faq',
    ]
    entries = [
        {
            'loc': _absolute_public_url(path),
            'lastmod': now,
            'changefreq': 'weekly',
            'priority': '0.8' if path == '/' else '0.6',
        }
        for path in paths
    ]
    return _render_sitemap_urlset(entries)


@frontend_bp.route('/sitemap-products.xml')
def sitemap_products():
    """Sitemap for active product pages."""
    products = Product.query.filter_by(is_active=True).all()
    entries = []
    for product in products:
        if product.slug:
            product_path = url_for('frontend.product_detail_slug', slug=product.slug)
        else:
            product_path = url_for('frontend.product_detail', product_id=product.id)

        entries.append(
            {
                'loc': _absolute_public_url(product_path),
                'lastmod': _format_lastmod(getattr(product, 'updated_at', None) or getattr(product, 'created_at', None)),
                'changefreq': 'weekly',
                'priority': '0.7',
            }
        )
    return _render_sitemap_urlset(entries)


@frontend_bp.route('/sitemap-blog.xml')
def sitemap_blog():
    """Sitemap for published blog pages."""
    posts = BlogPost.query.filter(
        BlogPost.status == BlogStatus.PUBLISHED,
        BlogPost.published_at <= datetime.now(UTC)
    ).order_by(desc(BlogPost.published_at)).all()

    entries = [
        {
            'loc': _absolute_public_url(url_for('frontend.blog_detail', slug=post.slug)),
            'lastmod': _format_lastmod(post.published_at or getattr(post, 'updated_at', None)),
            'changefreq': 'monthly',
            'priority': '0.6',
        }
        for post in posts
    ]
    return _render_sitemap_urlset(entries)


@frontend_bp.route('/feeds/google-products.xml')
def google_products_feed():
    """Google Merchant-compatible product feed (XML RSS)."""
    language = current_app.config.get('DEFAULT_LANGUAGE', 'uz')
    products = Product.query.filter_by(is_active=True).all()

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">',
        '<channel>',
        f'<title>{escape("Blue Stream Group Product Feed")}</title>',
        f'<link>{escape(_absolute_public_url("/shop"))}</link>',
        f'<description>{escape("Blue Stream catalog for shopping discovery and product search.")}</description>',
    ]

    for product in products:
        product_data = product.to_dict(language=language)
        product_name = product_data.get('name') or f'Product {product.id}'
        product_description = (
            product_data.get('meta_description')
            or product_data.get('short_description')
            or product_data.get('description')
            or product_name
        )
        base_price_raw = product_data.get('base_price') or 0
        discount_price_raw = product_data.get('discount_price')
        base_price_value = float(base_price_raw) if base_price_raw is not None else 0.0
        discount_price_value = float(discount_price_raw) if discount_price_raw is not None else 0.0
        if base_price_value <= 0 and discount_price_value > 0:
            base_price_value = discount_price_value
            discount_price_value = 0.0
        use_sale_price = discount_price_value > 0 and discount_price_value < base_price_value
        product_availability = 'in stock' if (product.stock_quantity or 0) > 0 else 'out of stock'

        if product.slug:
            product_link = _absolute_public_url(url_for('frontend.product_detail_slug', slug=product.slug))
        else:
            product_link = _absolute_public_url(url_for('frontend.product_detail', product_id=product.id))

        images = product_data.get('images') or []
        image_link = _as_absolute_url(images[0]) if images else _as_absolute_url(url_for('static', filename='images/logo.png'))
        additional_images = []
        for image in images[1:11]:
            absolute_image = _as_absolute_url(image)
            if absolute_image:
                additional_images.append(absolute_image)
        mpn = (product.sku or '').strip() if getattr(product, 'sku', None) else None
        gtin = _normalize_feed_gtin(getattr(product, 'barcode', None))
        identifier_exists = bool(mpn or gtin)

        lines.extend([
            '<item>',
            f'<g:id>{product.id}</g:id>',
            f'<title>{escape(str(product_name))}</title>',
            f'<description>{escape(str(product_description))}</description>',
            f'<link>{escape(product_link)}</link>',
            f'<g:price>{_format_feed_price(base_price_value)} UZS</g:price>',
            f'<g:availability>{product_availability}</g:availability>',
            '<g:condition>new</g:condition>',
            f'<g:brand>{escape("Blue Stream Group")}</g:brand>',
            f'<g:google_product_category>{escape("Food, Beverages & Tobacco > Beverages > Water")}</g:google_product_category>',
        ])

        if use_sale_price:
            lines.append(f'<g:sale_price>{_format_feed_price(discount_price_value)} UZS</g:sale_price>')

        if image_link:
            lines.append(f'<g:image_link>{escape(image_link)}</g:image_link>')

        for additional_image in additional_images:
            lines.append(f'<g:additional_image_link>{escape(additional_image)}</g:additional_image_link>')

        if product_data.get('category') and product_data['category'].get('name'):
            lines.append(f'<g:product_type>{escape(str(product_data["category"]["name"]))}</g:product_type>')

        if mpn:
            lines.append(f'<g:mpn>{escape(mpn)}</g:mpn>')
        if gtin:
            lines.append(f'<g:gtin>{escape(gtin)}</g:gtin>')
        lines.append(f'<g:identifier_exists>{"yes" if identifier_exists else "no"}</g:identifier_exists>')

        lines.append('</item>')

    lines.extend(['</channel>', '</rss>'])
    return Response('\n'.join(lines), mimetype='application/xml')


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


# ============================================================================
# BLOG ROUTES
# ============================================================================

@frontend_bp.route('/blog')
def blog_list():
    """Blog listing page with filtering"""
    language = get_current_language()
    page = request.args.get('page', 1, type=int)
    category = request.args.get('category', None)
    tag = request.args.get('tag', None)
    per_page = 9

    # Build query for published posts
    query = BlogPost.query.filter(
        BlogPost.status == BlogStatus.PUBLISHED,
        BlogPost.published_at <= datetime.now(UTC)
    )

    # Apply filters
    if category:
        try:
            from business_app.models.blog import BlogCategory
            category_enum = BlogCategory(category)
            query = query.filter(BlogPost.category == category_enum)
        except ValueError:
            pass  # Invalid category, ignore filter

    if tag:
        query = query.filter(BlogPost.tags.ilike(f'%{tag}%'))

    # Order by published date
    query = query.order_by(desc(BlogPost.published_at))

    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    base_listing_params = {}
    if category:
        base_listing_params['category'] = category
    if tag:
        base_listing_params['tag'] = tag

    canonical_params = {}
    if category:
        canonical_params['category'] = category
    if tag:
        canonical_params['tag'] = tag
    if page > 1:
        canonical_params['page'] = page

    prev_page_url = None
    next_page_url = None
    if pagination.has_prev:
        prev_page_url = _build_external_url('frontend.blog_list', page=pagination.prev_num, **base_listing_params)
    if pagination.has_next:
        next_page_url = _build_external_url('frontend.blog_list', page=pagination.next_num, **base_listing_params)

    return render_template(
        'frontend/blog_list.html',
        posts=pagination.items,
        pagination=pagination,
        current_category=category,
        current_tag=tag,
        canonical_url=_build_external_url('frontend.blog_list', **canonical_params),
        prev_page_url=prev_page_url,
        next_page_url=next_page_url
    )


@frontend_bp.route('/blog/<slug>')
def blog_detail(slug):
    """Blog detail page"""
    language = get_current_language()

    # Find post by slug
    post = BlogPost.query.filter(
        BlogPost.slug == slug,
        BlogPost.status == BlogStatus.PUBLISHED,
        BlogPost.published_at <= datetime.now(UTC)
    ).first_or_404()

    # Increment view count
    try:
        post.increment_views()
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Error incrementing blog view count: {e}")
        db.session.rollback()

    # Get recent posts from same category
    recent_posts = BlogPost.query.filter(
        BlogPost.status == BlogStatus.PUBLISHED,
        BlogPost.published_at <= datetime.now(UTC),
        BlogPost.id != post.id,
        BlogPost.category == post.category
    ).order_by(desc(BlogPost.published_at)).limit(5).all()

    # Add contextual product recommendations for stronger blog-to-product linking.
    recommended_products = []
    raw_tags = post.tags.split(',') if post.tags else []
    normalized_tags = [tag.strip() for tag in raw_tags if tag.strip()]

    if normalized_tags:
        tag_conditions = []
        for tag in normalized_tags[:6]:
            pattern = f'%{tag}%'
            tag_conditions.extend([
                Product.name.ilike(pattern),
                Product.short_description.ilike(pattern),
                Product.description.ilike(pattern),
            ])
        if tag_conditions:
            recommended_products = Product.query.filter(
                Product.is_active == True,
                or_(*tag_conditions)
            ).order_by(desc(Product.is_featured), desc(Product.created_at)).limit(4).all()

    if len(recommended_products) < 4:
        existing_ids = {product.id for product in recommended_products}
        fallback_query = Product.query.filter(Product.is_active == True)
        if existing_ids:
            fallback_query = fallback_query.filter(~Product.id.in_(existing_ids))
        fallback_products = fallback_query.order_by(desc(Product.is_featured), desc(Product.created_at)).limit(
            4 - len(recommended_products)
        ).all()
        recommended_products.extend(fallback_products)

    return render_template(
        'frontend/blog_detail.html',
        post=post,
        recent_posts=recent_posts,
        recommended_products=[product.to_dict(language=language) for product in recommended_products],
        canonical_url=_build_external_url('frontend.blog_detail', slug=post.slug)
    )
