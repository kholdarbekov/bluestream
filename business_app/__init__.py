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
    from business_app.api.translations import translations_bp
    from business_app.api.blog import blog_bp
    from business_app.api.addresses import addresses_bp
    from business_app.api.bot import bot_bp
    from business_app.frontend import frontend_bp
    
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
    app.register_blueprint(blog_bp, url_prefix=f'{api_prefix}/blog')
    app.register_blueprint(addresses_bp, url_prefix=f'{api_prefix}/addresses')
    app.register_blueprint(bot_bp, url_prefix=f'{api_prefix}/bot')
    app.register_blueprint(translations_bp, url_prefix=f'{api_prefix}/translations')
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
        
        phone = click.prompt('Admin phone')
        email = click.prompt('Admin email')
        password = click.prompt('Admin password', hide_input=True)
        
        admin_user = AuthService.create_admin_user(phone, email, password)
        click.echo(f'Admin user created: {admin_user.email}')
    
    # Initialize configuration CLI commands
    from business_app.cli import init_app as init_cli
    init_cli(app)


def setup_request_handlers(app):
    """Setup request context handlers"""
    
    @app.before_request
    def before_request():
        """Execute before each request - Check URL params, session, and user preferences

        Language detection priority (highest to lowest):
        1. URL parameter (?lang=uz) - for explicit language switching
        2. Session language - most recent user preference from language switcher (PRIORITY)
        3. User's DB preferred_language - fallback for logged-in users
        4. Accept-Language header - browser preference
        5. Default language (uz)
        """
        # Set request start time for performance monitoring
        g.start_time = datetime.now(UTC)
        
        # Generate unique request ID for tracing
        import uuid
        g.request_id = str(uuid.uuid4())[:8]

        # Skip logging for healthcheck endpoints and static assets
        is_healthcheck = request.path in ['/health', '/healthz', '/api/health']
        is_static = request.path.startswith('/static/')
        should_log = not is_healthcheck and not is_static

        lang = None
        lang_source = None  # Track where the language came from

        # DEBUG: Log all available language sources
        url_lang = request.args.get('lang', None)
        session_lang = session.get('language')
        browser_lang = request.headers.get('Accept-Language', '')[:10] if request.headers.get('Accept-Language') else None

        # 1. Check URL parameter first (highest priority)
        if url_lang and url_lang in app.config['LANGUAGES']:
            lang = url_lang
            lang_source = "URL parameter"

        # 2. Check session language (from set-language endpoint) - PRIORITY over DB
        # This ensures explicit user language changes via UI take immediate effect
        if not lang:

            if session_lang and session_lang in app.config['LANGUAGES']:
                lang = session_lang
                lang_source = "Session"

        # 3. Check if user is logged in and has a preferred language in DB
        # This is a fallback for logged-in users who haven't set a session preference
        if not lang:
            try:
                verify_jwt_in_request(optional=True)
                current_user_id = get_jwt_identity()
                if current_user_id:
                    from business_app.models.user import User
                    user = User.query.get(current_user_id)
                    if user:
                        if user.preferred_language and user.preferred_language in app.config['LANGUAGES']:
                            lang = user.preferred_language
                            lang_source = "User DB preference"
            except Exception as e:
                pass  # Continue with other methods


        # 4. Fall back to browser Accept-Language header
        if not lang:
            full_browser_lang = request.headers.get('Accept-Language', '')
            if full_browser_lang and full_browser_lang[:2] in app.config['LANGUAGES']:
                lang = full_browser_lang[:2]
                lang_source = "Accept-Language header"

        # 5. Use default language if nothing else worked
        if not lang or lang not in app.config['LANGUAGES']:
            lang = app.config['DEFAULT_LANGUAGE']
            lang_source = "Default"

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
        
        # Add Cache-Control headers for dynamic content
        # This prevents browsers from caching the page when language changes
        if 'Cache-Control' not in response.headers:
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        
        # Ensure Vary: Cookie is set so caches know content depends on cookies
        if 'Components' not in response.headers.get('Vary', ''):
            response.headers['Vary'] = 'Cookie'
        
        # ===================================================================
        # JWT Implicit Token Refresh (Flask-JWT-Extended recommended approach)
        # https://flask-jwt-extended.readthedocs.io/en/stable/refreshing_tokens.html
        # ===================================================================
        # Auto-refresh tokens that are within 30 minutes of expiring.
        # This prevents users from being logged out during active sessions.
        try:
            from flask_jwt_extended import get_jwt, get_jwt_identity, create_access_token, set_access_cookies
            from datetime import timedelta, timezone
            
            exp_timestamp = get_jwt()["exp"]
            now = datetime.now(timezone.utc)
            target_timestamp = datetime.timestamp(now + timedelta(minutes=30))
            
            if target_timestamp > exp_timestamp:
                # Token is within 30 minutes of expiring - refresh it
                # IMPORTANT: Preserve original claims (especially 'role') when refreshing
                # Otherwise admin users lose their role and get 403 Forbidden errors
                jwt_data = get_jwt()
                additional_claims = {
                    key: jwt_data[key] 
                    for key in ['user_id', 'email', 'role', 'status', 'verified', 'platform', 'session_id']
                    if key in jwt_data
                }
                access_token = create_access_token(
                    identity=get_jwt_identity(),
                    additional_claims=additional_claims
                )
                set_access_cookies(response, access_token)
                app.logger.info(f"JWT auto-refreshed for user {get_jwt_identity()} with role={additional_claims.get('role')}")
        except (RuntimeError, KeyError):
            # No valid JWT in request (anonymous user, API call, etc.)
            # Just return the original response
            pass
        except Exception as e:
            # Log unexpected errors but don't break the response
            app.logger.warning(f"JWT auto-refresh error: {e}")
            
        return response
    
    @app.teardown_appcontext
    def close_db(error):
        """Close database connections safely"""
        try:
            # Remove session - this returns connections to the pool
            # SQLAlchemy handles rollback automatically if there was an error
            db.session.remove()
        except Exception as e:
            # Log but don't re-raise - connection may already be corrupted/closed
            # This prevents cascade failures during teardown
            app.logger.warning(f"Error during session cleanup: {e}")


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
        # Enhanced logging to debug CSRF issues
        from flask import request
        app.logger.error(f'JWT Missing Token Error: {error}')
        app.logger.error(f'Request cookies: {list(request.cookies.keys())}')
        app.logger.error(f'Request headers: Authorization={request.headers.get("Authorization")}, X-CSRF-TOKEN={request.headers.get("X-CSRF-TOKEN")}')
        app.logger.error(f'CSRF token cookie: {request.cookies.get("csrf_access_token")}')
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

    @jwt.token_in_blocklist_loader
    def check_if_token_revoked(jwt_header, jwt_payload):
        """
        Callback to check if a JWT token has been blacklisted/revoked.
        This is called on every request that requires JWT authentication.

        Args:
            jwt_header: The header of the JWT
            jwt_payload: The payload/claims of the JWT

        Returns:
            True if token is blacklisted, False otherwise
        """
        jti = jwt_payload['jti']

        try:
            # Import TokenService to check blacklist
            from business_app.services.token_service import TokenService
            token_service = TokenService()

            # Check if token is blacklisted
            is_blacklisted = token_service.is_token_blacklisted(jti)

            if is_blacklisted:
                app.logger.info(f'Blocked blacklisted token with JTI: {jti}')

            return is_blacklisted

        except Exception as e:
            app.logger.error(f'Error checking token blacklist for JTI {jti}: {e}')
            # Fail open to prevent authentication disruption
            # In production, you might want to fail closed (return True)
            return False


def create_app(config_class=None):
    """
    Create Flask application factory
    """
    app = Flask(__name__)
    print(f"!!! CREATE_APP CALLED - app id: {id(app)}, config_class: {config_class}")
    
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
    # if app.debug:
    app.jinja_env.cache = None
    app.jinja_env.auto_reload = True
    app.jinja_env.cache_size = 0
    # Force template recompilation by modifying loader
    if hasattr(app.jinja_loader, '_mapping'):
        app.jinja_loader._mapping = {}
    
    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db, directory='business_app/migrations')
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

    # Warm translation cache on app startup (within app context)
    with app.app_context():
        try:
            from business_app.utils.translations import translation_service

            # Warm landing page translations (highest traffic, rarely changes)
            # These are cached for 3 days, significantly reducing DB load
            result = translation_service.warm_cache_for_category('landing')
            if result.get('success'):
                app.logger.info(
                    f"Landing cache warmed: {result.get('count')} translations "
                    f"(TTL: {result.get('ttl_seconds')}s)"
                )
            else:
                app.logger.warning(f"Landing cache warming failed: {result.get('reason')}")

            # Optionally warm UI translations (admin dashboard, moderate changes)
            # Uncomment to pre-load UI translations on startup
            # result = translation_service.warm_cache_for_category('ui')
            # if result.get('success'):
            #     app.logger.info(
            #         f"UI cache warmed: {result.get('count')} translations "
            #         f"(TTL: {result.get('ttl_seconds')}s)"
            #     )

        except Exception as e:
            # Don't fail app startup if cache warming fails
            app.logger.error(f"Translation cache warming error: {e}", exc_info=True)

    print(f"!!! CREATE_APP FINISHED - app id: {id(app)}, app.config['DEBUG']: {app.config.get('DEBUG')}")
    return app


