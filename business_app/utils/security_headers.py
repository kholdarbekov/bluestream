"""
Security headers middleware for BlueStream Platform
Implements comprehensive security headers including Content Security Policy
"""
import re
from typing import Dict, List, Optional, Union
from flask import Flask, request, Response, current_app
from urllib.parse import urlparse


class SecurityHeadersConfig:
    """Configuration for security headers"""
    
    def __init__(self, environment: str = 'development'):
        self.environment = environment.lower()
        
        # Basic security headers
        self.x_content_type_options = 'nosniff'
        self.x_xss_protection = '1; mode=block'
        self.referrer_policy = 'strict-origin-when-cross-origin'
        self.permissions_policy = self._get_permissions_policy()
        
        # Environment-specific configurations
        if self.environment == 'production':
            self.x_frame_options = 'DENY'
            self.strict_transport_security = 'max-age=63072000; includeSubDomains; preload'
        elif self.environment == 'staging':
            self.x_frame_options = 'SAMEORIGIN'
            self.strict_transport_security = 'max-age=31536000; includeSubDomains'
        else:  # development
            self.x_frame_options = 'SAMEORIGIN'
            self.strict_transport_security = None  # No HSTS in development
    
    def _get_permissions_policy(self) -> str:
        """Get Permissions Policy (formerly Feature Policy)"""
        policies = [
            "accelerometer=()",
            "ambient-light-sensor=()",
            "autoplay=()",
            "battery=()",
            "bluetooth=()",
            "camera=()",
            "clipboard-read=()",
            "clipboard-write=(self)",
            "display-capture=()",
            "document-domain=()",
            "encrypted-media=()",
            "execution-while-not-rendered=()",
            "execution-while-out-of-viewport=()",
            "fullscreen=(self)",
            "geolocation=(self)",
            "gyroscope=()",
            "hid=()",
            "identity-credentials-get=()",
            "idle-detection=()",
            "local-fonts=()",
            "magnetometer=()",
            "microphone=()",
            "midi=()",
            "navigation-override=()",
            "payment=(self)",
            "picture-in-picture=()",
            "publickey-credentials-get=()",
            "screen-wake-lock=()",
            "serial=()",
            "speaker-selection=()",
            "storage-access=()",
            "usb=()",
            "web-share=(self)",
            "xr-spatial-tracking=()"
        ]
        return ", ".join(policies)


class CSPBuilder:
    """Content Security Policy builder"""
    
    def __init__(self, environment: str = 'development'):
        self.environment = environment.lower()
        self.directives = {}
        self._setup_default_policy()
    
    def _setup_default_policy(self):
        """Setup default CSP based on environment"""
        if self.environment == 'production':
            self.directives = {
                'default-src': ["'self'"],
                'script-src': ["'self'", "'unsafe-inline'", 'https://unpkg.com'],  # Allow inline scripts for frontend + Leaflet
                'style-src': ["'self'", "'unsafe-inline'", 'https://fonts.googleapis.com', 'https://unpkg.com'],  # Leaflet CSS
                'font-src': ["'self'", 'https://fonts.gstatic.com', 'data:'],
                'img-src': ["'self'", 'data:', 'https:', 'blob:'],
                'media-src': ["'self'"],
                'object-src': ["'none'"],
                'frame-src': ["'none'"],
                'frame-ancestors': ["'none'"],
                'form-action': ["'self'"],
                'base-uri': ["'self'"],
                'connect-src': ["'self'", 'wss:', 'ws:'],
                'worker-src': ["'self'"],
                'manifest-src': ["'self'"],
                'upgrade-insecure-requests': None
            }
        elif self.environment == 'staging':
            self.directives = {
                'default-src': ["'self'"],
                'script-src': ["'self'", "'unsafe-inline'", "'unsafe-eval'", 'https://unpkg.com'],  # More permissive for testing + Leaflet
                'style-src': ["'self'", "'unsafe-inline'", 'https://fonts.googleapis.com', 'https://unpkg.com'],  # Leaflet CSS
                'font-src': ["'self'", 'https://fonts.gstatic.com', 'data:'],
                'img-src': ["'self'", 'data:', 'https:', 'blob:'],
                'media-src': ["'self'"],
                'object-src': ["'none'"],
                'frame-src': ["'self'"],
                'frame-ancestors': ["'self'"],
                'form-action': ["'self'"],
                'base-uri': ["'self'"],
                'connect-src': ["'self'", 'wss:', 'ws:'],
                'worker-src': ["'self'"],
                'manifest-src': ["'self'"]
            }
        else:  # development
            self.directives = {
                'default-src': ["'self'"],
                'script-src': ["'self'", "'unsafe-inline'", "'unsafe-eval'", 'localhost:*'],
                'style-src': ["'self'", "'unsafe-inline'", 'fonts.googleapis.com'],
                'font-src': ["'self'", 'fonts.gstatic.com', 'data:'],
                'img-src': ["'self'", 'data:', 'https:', 'http:', 'blob:'],
                'media-src': ["'self'"],
                'object-src': ["'none'"],
                'frame-src': ["'self'", 'localhost:*'],
                'frame-ancestors': ["'self'"],
                'form-action': ["'self'"],
                'base-uri': ["'self'"],
                'connect-src': ["'self'", 'wss:', 'ws:', 'localhost:*', '127.0.0.1:*'],
                'worker-src': ["'self'"],
                'manifest-src': ["'self'"]
            }
    
    def add_source(self, directive: str, source: Union[str, List[str]]):
        """Add source(s) to a CSP directive"""
        if directive not in self.directives:
            self.directives[directive] = []
        
        if isinstance(source, str):
            source = [source]
        
        for src in source:
            if src not in self.directives[directive]:
                self.directives[directive].append(src)
    
    def remove_source(self, directive: str, source: str):
        """Remove a source from a CSP directive"""
        if directive in self.directives and source in self.directives[directive]:
            self.directives[directive].remove(source)
    
    def set_directive(self, directive: str, sources: List[str]):
        """Set a CSP directive with specific sources"""
        self.directives[directive] = sources
    
    def build(self) -> str:
        """Build the CSP header string"""
        policy_parts = []
        
        for directive, sources in self.directives.items():
            if sources is None:
                # Directive without sources (like upgrade-insecure-requests)
                policy_parts.append(directive)
            elif sources:
                policy_parts.append(f"{directive} {' '.join(sources)}")
        
        return "; ".join(policy_parts)
    
    def build_report_only(self, report_uri: str = None) -> str:
        """Build CSP for report-only mode"""
        policy = self.build()
        if report_uri:
            policy += f"; report-uri {report_uri}"
        return policy


class SecurityHeadersMiddleware:
    """Middleware for applying security headers"""
    
    def __init__(self, app: Flask = None, config: SecurityHeadersConfig = None):
        self.app = app
        self.config = config or SecurityHeadersConfig()
        
        if app is not None:
            self.init_app(app)
    
    def init_app(self, app: Flask):
        """Initialize the middleware with a Flask app"""
        self.app = app
        
        # Configure CSP based on app configuration
        self.csp_builder = CSPBuilder(app.config.get('FLASK_ENV', 'development'))
        
        # Add custom CSP sources from config
        if 'CSP_SOURCES' in app.config:
            for directive, sources in app.config['CSP_SOURCES'].items():
                self.csp_builder.add_source(directive, sources)
        
        # Setup after_request handler
        app.after_request(self._add_security_headers)
    
    def _add_security_headers(self, response: Response) -> Response:
        """Add security headers to response"""
        try:
            # Skip for certain content types or paths if needed
            if self._should_skip_headers(request.path, response):
                return response
            
            # Basic security headers
            response.headers['X-Content-Type-Options'] = self.config.x_content_type_options
            response.headers['X-XSS-Protection'] = self.config.x_xss_protection
            response.headers['X-Frame-Options'] = self.config.x_frame_options
            response.headers['Referrer-Policy'] = self.config.referrer_policy
            response.headers['Permissions-Policy'] = self.config.permissions_policy
            
            # HSTS (only in production/staging)
            if self.config.strict_transport_security:
                response.headers['Strict-Transport-Security'] = self.config.strict_transport_security
            
            # Content Security Policy
            csp_policy = self._get_csp_for_request(request)
            if csp_policy:
                if self.app.config.get('CSP_REPORT_ONLY', False):
                    response.headers['Content-Security-Policy-Report-Only'] = csp_policy
                else:
                    response.headers['Content-Security-Policy'] = csp_policy
            
            # Additional headers for API endpoints
            if request.path.startswith('/api/'):
                response.headers['X-Robots-Tag'] = 'noindex, nofollow'
                response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, proxy-revalidate'
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
            
        except Exception as e:
            # Log error but don't break the response
            current_app.logger.error(f"Error applying security headers: {e}")
        
        return response
    
    def _should_skip_headers(self, path: str, response: Response) -> bool:
        """Determine if headers should be skipped for this request"""
        # Skip for certain file types
        skip_extensions = ['.ico', '.png', '.jpg', '.jpeg', '.gif', '.css', '.js', '.woff', '.woff2', '.ttf', '.eot']
        if any(path.endswith(ext) for ext in skip_extensions):
            return True
        
        # Skip for certain response types
        content_type = response.headers.get('Content-Type', '')
        if any(ct in content_type for ct in ['image/', 'font/', 'application/font']):
            return True
        
        return False
    
    def _get_csp_for_request(self, request) -> str:
        """Get CSP policy based on request path"""
        # API endpoints get strict CSP
        if request.path.startswith('/api/'):
            return "default-src 'none'; frame-ancestors 'none'"
        
        # Health and metrics endpoints
        if request.path in ['/health', '/metrics']:
            return "default-src 'none'; frame-ancestors 'none'"
        
        # Admin endpoints get stricter CSP
        if request.path.startswith('/admin'):
            admin_csp = CSPBuilder(self.config.environment)
            admin_csp.set_directive('script-src', ["'self'", "'unsafe-inline'"])
            admin_csp.set_directive('style-src', ["'self'", "'unsafe-inline'"])
            admin_csp.set_directive('frame-ancestors', ["'none'"])
            return admin_csp.build()
        
        # Frontend gets the full CSP
        return self.csp_builder.build()


def setup_security_headers(app: Flask):
    """Setup security headers for the Flask application"""
    
    # Get environment
    environment = app.config.get('FLASK_ENV', 'development')
    
    # Create configuration
    config = SecurityHeadersConfig(environment)
    
    # Create and initialize middleware
    middleware = SecurityHeadersMiddleware(app, config)
    
    # Store reference for later access
    app.security_headers = middleware
    
    # Log security headers setup
    app.logger.info(f'Security headers configured for {environment} environment')
    
    return middleware


def configure_csp_sources(app: Flask, sources: Dict[str, List[str]]):
    """Configure additional CSP sources for the application"""
    if not hasattr(app, 'security_headers'):
        raise RuntimeError("Security headers middleware not initialized")
    
    for directive, source_list in sources.items():
        app.security_headers.csp_builder.add_source(directive, source_list)


def get_current_csp_policy(app: Flask) -> str:
    """Get the current CSP policy as a string"""
    if not hasattr(app, 'security_headers'):
        return "Security headers not configured"
    
    return app.security_headers.csp_builder.build()


# Content Security Policy violation reporting endpoint
def setup_csp_reporting(app: Flask):
    """Setup CSP violation reporting endpoint"""
    
    @app.route('/csp-report', methods=['POST'])
    def csp_report():
        """Handle CSP violation reports"""
        try:
            from flask import request, jsonify
            import json
            
            # Parse the CSP violation report
            report_data = request.get_json(force=True)
            
            # Log the violation
            app.logger.warning(
                "CSP Violation reported",
                extra={
                    'csp_report': report_data,
                    'user_agent': request.headers.get('User-Agent'),
                    'ip_address': request.remote_addr,
                    'referer': request.headers.get('Referer')
                }
            )
            
            # You could also store violations in database or send to monitoring service
            
            return jsonify({'status': 'reported'}), 204
            
        except Exception as e:
            app.logger.error(f"Error processing CSP report: {e}")
            return jsonify({'error': 'Report processing failed'}), 400


# Export main components
__all__ = [
    'SecurityHeadersConfig',
    'CSPBuilder', 
    'SecurityHeadersMiddleware',
    'setup_security_headers',
    'configure_csp_sources',
    'get_current_csp_policy',
    'setup_csp_reporting'
]