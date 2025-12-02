"""
Business App Package
Water delivery business management system
"""
import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, UTC
from sqlalchemy import text
from flask import Flask, jsonify, render_template, request, g, session
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_jwt_extended import JWTManager, verify_jwt_in_request, get_jwt_identity
from flask_cors import CORS
from flask_limiter import Limiter
from flask_caching import Cache
from flask_mail import Mail
from flask_limiter.util import get_remote_address
import redis
from flasgger import Swagger
import click
from business_app.config import get_config
from business_app.utils.exceptions import ValidationError, NotFoundError, UnauthorizedError
from business_app.utils.helpers import set_language
from business_app.utils.template_helpers import register_multilingual_filters, register_multilingual_globals

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
jwt = JWTManager()
cors = CORS()
limiter = Limiter(key_func=get_remote_address)
cache = Cache()
mail = Mail()
redis_client = redis.from_url(os.environ.get('REDIS_URL', 'redis://redis:6379/0'))


def setup_logging(app):
    """Configure application logging with enhanced structured logging"""
    # Use the new enhanced logging system
    from business_app.utils.logging_config import setup_enhanced_logging
    setup_enhanced_logging(app)


def register_blueprints(app: Flask):
    """Register application blueprints"""
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
    from business_app.api.session_management import session_management_bp
    
    # API blueprints
    api_prefix = app.config['API_PREFIX']
    app.register_blueprint(auth_bp, url_prefix=f'{api_prefix}/auth')
    app.register_blueprint(products_bp, url_prefix=f'{api_prefix}/products')
    app.register_blueprint(orders_bp, url_prefix=f'{api_prefix}/orders')
    app.register_blueprint(cart_bp, url_prefix=f'{api_prefix}/cart')
    app.register_blueprint(payments_bp, url_prefix=f'{api_prefix}/payments')
    app.register_blueprint(delivery_bp, url_prefix=f'{api_prefix}/delivery')
    app.register_blueprint(subscriptions_bp, url_prefix=f'{api_prefix}/subscriptions')
    app.register_blueprint(loyalty_bp, url_prefix=f'{api_prefix}/loyalty')
    app.register_blueprint(notifications_bp, url_prefix=f'{api_prefix}/notifications')
    app.register_blueprint(analytics_bp, url_prefix=f'{api_prefix}/analytics')
    app.register_blueprint(admin_bp, url_prefix=f'{api_prefix}/admin')
    app.register_blueprint(session_management_bp, url_prefix=f'{api_prefix}/session')
    
    # Frontend blueprint for web interface
    from business_app.frontend import frontend_bp
    app.register_blueprint(frontend_bp)


def register_error_handlers(app):
    """Register application error handlers"""
    # Import and register the new standardized error handlers
    from business_app.utils.error_handlers import register_error_handlers as register_new_handlers
    register_new_handlers(app)


def register_cli_commands(app):
    """Register CLI commands"""
    
    # Register timezone management commands
    from business_app.cli.timezone_commands import register_timezone_commands
    register_timezone_commands(app)
    
    @app.cli.command()
    def init_db():
        """Initialize the database"""
        from flask_migrate import upgrade
        upgrade()
        click.echo('Database initialized.')
    
    @app.cli.command()
    def seed_data():
        """Seed initial data"""
        from scripts.seed_data import seed_all_data
        seed_all_data()
        click.echo('Database seeded.')
    
    @app.cli.command()
    def create_admin():
        """Create admin user"""
        from business_app.services.auth_service import AuthService
        
        email = click.prompt('Admin email')
        password = click.prompt('Admin password', hide_input=True)
        
        admin_user = AuthService.create_admin_user(email, password)
        click.echo(f'Admin user created: {admin_user.email}')
    
    # Initialize configuration CLI commands
    from business_app.cli import init_app as init_cli
    init_cli(app)


def setup_request_handlers(app):
    """Setup request context handlers"""
    
    @app.before_request
    def before_request():
        """Execute before each request - Check URL params, session, and user preferences"""
        # Set request start time for performance monitoring
        g.start_time = datetime.now(UTC)

        # Skip logging for healthcheck endpoints
        is_healthcheck = request.path in ['/health', '/healthz', '/api/health']

        # Get language from URL parameter first
        lang = request.args.get('lang', None)

        # 1. Check URL parameter first
        if lang and lang in app.config['LANGUAGES']:
            pass  # Use URL language
        else:
            # 2. Check if user is logged in and has a preferred language
            try:
                verify_jwt_in_request(optional=True)
                current_user_id = get_jwt_identity()
                if current_user_id:
                    from business_app.models.user import User
                    user = User.query.get(current_user_id)
                    if user and user.preferred_language:
                        lang = user.preferred_language
            except Exception:
                pass  # Continue with other methods

            # 3. Check session language (from set-language endpoint)
            if not lang:
                session_lang = session.get('language')
                if session_lang and session_lang in app.config['LANGUAGES']:
                    lang = session_lang

            # 4. Fall back to browser Accept-Language header
            if not lang:
                browser_lang = request.headers.get('Accept-Language', '')
                if browser_lang and browser_lang[:2] in app.config['LANGUAGES']:
                    lang = browser_lang[:2]

        # 5. Use default language if nothing else worked
        if not lang or lang not in app.config['LANGUAGES']:
            lang = app.config['DEFAULT_LANGUAGE']

        # Set the language in request context
        g.language = lang
    
    @app.after_request
    def after_request(response):
        """Execute after each request"""
        # Note: Security headers are now handled by SecurityHeadersMiddleware
        
        # Add performance monitoring
        if hasattr(g, 'start_time'):
            duration = (datetime.now(UTC) - g.start_time).total_seconds() * 1000
            response.headers['X-Response-Time'] = f'{duration:.2f}ms'
        
        return response
    
    @app.teardown_appcontext
    def close_db(error):
        """Close database connections"""
        if error:
            db.session.rollback()
        db.session.close()


def setup_jwt_handlers(app):
    """Setup JWT-related handlers"""
    
    @jwt.expired_token_loader
    def expired_token_callback(jwt_header, jwt_payload):
        app.logger.error(f'JWT Expired Token: header={jwt_header}, payload={jwt_payload}')
        return jsonify({
            'error': 'Token Expired',
            'message': 'The token has expired.'
        }), 401
    
    @jwt.invalid_token_loader
    def invalid_token_callback(error):
        app.logger.error(f'JWT Invalid Token Error: {error}')
        app.logger.error(f'Error type: {type(error)}')
        return jsonify({
            'error': 'Invalid Token',
            'message': 'The token is invalid.'
        }), 401
    
    @jwt.unauthorized_loader
    def missing_token_callback(error):
        app.logger.error(f'JWT Missing Token Error: {error}')
        return jsonify({
            'error': 'Authorization Required',
            'message': 'Request does not contain an access token.'
        }), 401
    
    @jwt.revoked_token_loader
    def revoked_token_callback(jwt_header, jwt_payload):
        app.logger.error(f'JWT Revoked Token: header={jwt_header}, payload={jwt_payload}')
        return jsonify({
            'error': 'Token Revoked',
            'message': 'The token has been revoked.'
        }), 401


def create_app(config_class=None):
    """
    Create Flask application factory
    """
    app = Flask(__name__)
    
    # Load configuration
    if config_class:
        if isinstance(config_class, type):
            # Create instance to resolve properties
            config_instance = config_class()
            # Copy all non-private attributes to the Flask config
            for attr in dir(config_instance):
                if not attr.startswith('_') and not callable(getattr(config_instance, attr)):
                    try:
                        value = getattr(config_instance, attr)
                        app.config[attr] = value
                    except Exception:
                        # Skip attributes that can't be accessed during startup
                        pass
        else:
            # Handle dictionary-style config (for backward compatibility)
            app.config.update(config_class)
            config_class = type('Config', (), config_class)
    else:
        config_class = get_config()
        # Create instance to resolve properties
        config_instance = config_class()
        # Copy all non-private attributes to the Flask config
        for attr in dir(config_instance):
            if not attr.startswith('_') and not callable(getattr(config_instance, attr)):
                try:
                    value = getattr(config_instance, attr)
                    app.config[attr] = value
                except Exception:
                    # Skip attributes that can't be accessed during startup
                    pass
    
    # Initialize configuration with app-specific setup
    if hasattr(config_class, 'init_app'):
        print(f"SENTRY_DSN: {config_class.SENTRY_DSN}")
        app.logger.info(f"SENTRY_DSN: {config_class.SENTRY_DSN}")
        config_class.init_app(app)
    else:
        # Legacy validation for backward compatibility
        if not app.testing:
            if hasattr(config_class, 'validate_secret_key'):
                config_class.validate_secret_key()
            if hasattr(config_class, 'validate_debug_mode'):
                config_class.validate_debug_mode()
            if hasattr(config_class, 'validate_production_settings'):
                config_class.validate_production_settings()
    
    # Additional environment validation on startup
    if not app.testing:
        from business_app.utils.env_validator import validate_environment_startup
        validation_passed = validate_environment_startup(app)
        
        # In production, fail hard if validation doesn't pass
        if not validation_passed and os.environ.get('FLASK_ENV') == 'production':
            raise RuntimeError("Environment validation failed in production")
    
    # Disable Jinja2 template caching completely for development
    if app.debug:
        app.jinja_env.cache = {}
        app.jinja_env.auto_reload = True
        app.jinja_env.cache_size = 0
        # Force template recompilation by modifying loader
        if hasattr(app.jinja_loader, '_mapping'):
            app.jinja_loader._mapping = {}
    
    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    cors.init_app(
        app, 
        origins=app.config['CORS_ORIGINS'],
        supports_credentials=True
    )
    limiter.init_app(app)
    cache.init_app(app)
    mail.init_app(app)

    # Initialize enhanced Swagger documentation
    from business_app.utils.swagger_config import get_swagger_template, get_swagger_config
    
    swagger_config = get_swagger_config()
    swagger_template = get_swagger_template()
    
    # Override config route if specified
    if 'API_DOCS_URL' in app.config:
        swagger_config['specs_route'] = app.config['API_DOCS_URL']
    
    Swagger(app, config=swagger_config, template=swagger_template)

    # Setup logging
    setup_logging(app)
    
    # Setup monitoring
    from business_app.utils.monitoring import setup_monitoring
    setup_monitoring(app)
    
    # Setup security headers
    from business_app.utils.security_headers import setup_security_headers, setup_csp_reporting
    setup_security_headers(app)
    setup_csp_reporting(app)
    
    # Setup CSRF protection
    from business_app.utils.csrf_protection import setup_csrf_protection
    setup_csrf_protection(app)
    
    # Setup password security
    from business_app.utils.password_security import setup_password_security
    setup_password_security(app)
    
    # Setup timezone handling
    from business_app.middleware.timezone_middleware import TimezoneMiddleware
    from business_app.utils.timezone_utils import setup_timezone_filters
    timezone_middleware = TimezoneMiddleware(app)
    setup_timezone_filters(app)
    
    # Register blueprints
    register_blueprints(app)
    
    # Register multilingual template helpers
    register_multilingual_filters(app)
    register_multilingual_globals(app)
    
    # Register error handlers
    register_error_handlers(app)
    
    # Register CLI commands
    register_cli_commands(app)
    
    # Request handlers
    setup_request_handlers(app)
    
    # JWT handlers
    setup_jwt_handlers(app)
    
    # Initialize service factory
    from business_app.utils.service_factory import init_service_factory
    init_service_factory(app)
    
    # Register health check endpoint (exempt from rate limiting)
    @app.route('/health')
    @limiter.exempt
    def health_check():
        """Health check endpoint - exempt from rate limiting for monitoring systems"""
        try:
            # Check database connection
            db.session.execute(text('SELECT 1'))

            # Check Redis connection
            redis_client.ping()

            return jsonify({
                'status': 'healthy',
                'timestamp': datetime.now(UTC).isoformat(),
                'version': '1.0.0',
                'services': {
                    'database': 'ok',
                    'redis': 'ok'
                }
            })
        except Exception as e:
            app.logger.error(f'Health check failed: {e}')
            return jsonify({
                'status': 'unhealthy',
                'timestamp': datetime.now(UTC).isoformat(),
                'error': str(e)
            }), 503

    # Serve uploaded files (high rate limit for images/static files)
    @app.route('/uploads/<path:filename>')
    @limiter.limit('5000/hour', key_func=lambda: request.remote_addr)
    def uploaded_file(filename):
        """Serve uploaded files from the uploads directory"""
        from flask import send_from_directory
        import os
        # Use the same upload path as FileStorageService
        upload_folder = app.config.get('UPLOAD_FOLDER', 'uploads/')
        uploads_dir = os.path.join(app.root_path, upload_folder)
        return send_from_directory(uploads_dir, filename)

    return app


