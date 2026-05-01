"""
WSGI entry point for the Water Business Platform
"""

from business_app import create_app
from business_app.config import get_config

# Create application instance
config_class = get_config()
app = create_app(config_class)

if __name__ == "__main__":
    app.run()
