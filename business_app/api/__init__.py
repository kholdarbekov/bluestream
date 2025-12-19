"""
API Blueprint Registration
This file registers all API blueprints for the Water Business Platform
"""
from flask import Blueprint

def register_api_blueprints(app):
    """Register all API blueprints with the Flask app"""
    
    # Create main API blueprint
    api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
    
    # Import and register all API blueprints
    from business_app.api.auth import auth_bp
    from business_app.api.products import products_bp
    from business_app.api.orders import orders_bp
    from business_app.api.carts import cart_bp
    from business_app.api.payments import payments_bp
    from business_app.api.delivery import delivery_bp
    from business_app.api.subscriptions import subscriptions_bp
    from business_app.api.loyalty import loyalty_bp
    from business_app.api.notifications import notifications_bp
    from business_app.api.analytics import analytics_bp
    from business_app.api.admin import admin_bp
    from business_app.api.blog import blog_bp
    from business_app.api.addresses import addresses_bp
    from business_app.api.bot import bot_bp
    from business_app.api.translations import translations_bp

    # Register blueprints with URL prefixes
    api_bp.register_blueprint(auth_bp, url_prefix='/auth')
    api_bp.register_blueprint(products_bp, url_prefix='/products')
    api_bp.register_blueprint(orders_bp, url_prefix='/orders')
    api_bp.register_blueprint(cart_bp, url_prefix='/cart')
    api_bp.register_blueprint(payments_bp, url_prefix='/payments')
    api_bp.register_blueprint(delivery_bp, url_prefix='/delivery')
    api_bp.register_blueprint(subscriptions_bp, url_prefix='/subscriptions')
    api_bp.register_blueprint(loyalty_bp, url_prefix='/loyalty')
    api_bp.register_blueprint(notifications_bp, url_prefix='/notifications')
    api_bp.register_blueprint(analytics_bp, url_prefix='/analytics')
    api_bp.register_blueprint(admin_bp, url_prefix='/admin')
    api_bp.register_blueprint(blog_bp, url_prefix='/blog')
    api_bp.register_blueprint(addresses_bp, url_prefix='/addresses')
    api_bp.register_blueprint(bot_bp, url_prefix='/bot')
    api_bp.register_blueprint(translations_bp, url_prefix='/translations')

    # Register main API blueprint with app
    app.register_blueprint(api_bp)
    
    return api_bp