# Water Business Platform - Project Structure

```
water_business_platform/
├── README.md
├── docker-compose.yml
├── .env.example
├── .gitignore
├── requirements.txt
├── Dockerfile
├── nginx.conf
│
├── business_app/                          # Flask Backend Application
│   ├── __init__.py
│   ├── app.py                            # Main Flask application
│   ├── config.py                         # Configuration settings
│   ├── wsgi.py                          # WSGI entry point
│   │
│   ├── models/                          # Database Models (fragmented)
│   │   ├── __init__.py
│   │   ├── user_models.py               # User, Customer, Admin models
│   │   ├── product_models.py            # Product, WaterType, Pricing models
│   │   ├── order_models.py              # Order, OrderItem, Payment models
│   │   ├── delivery_models.py           # Delivery, Route, TimeSlot models
│   │   ├── subscription_models.py       # Subscription, SubscriptionPlan models
│   │   ├── loyalty_models.py            # LoyaltyProgram, Points, Rewards models
│   │   ├── notification_models.py       # Notification, NotificationPreference models
│   │   └── analytics_models.py          # Analytics, Metrics, Reports models
│   │
│   ├── api/                             # API Routes
│   │   ├── __init__.py
│   │   ├── auth.py                      # Authentication endpoints
│   │   ├── products.py                  # Product management
│   │   ├── orders.py                    # Order management
│   │   ├── payments.py                  # Payment processing
│   │   ├── delivery.py                  # Delivery management
│   │   ├── subscriptions.py             # Subscription management
│   │   ├── loyalty.py                   # Loyalty program
│   │   ├── notifications.py             # Notification system
│   │   ├── analytics.py                 # Analytics endpoints
│   │   └── admin.py                     # Admin-specific endpoints
│   │
│   ├── serializers/                     # API Serializers
│   │   ├── __init__.py
│   │   ├── user_serializers.py
│   │   ├── product_serializers.py
│   │   ├── order_serializers.py
│   │   ├── payment_serializers.py
│   │   ├── delivery_serializers.py
│   │   ├── subscription_serializers.py
│   │   ├── loyalty_serializers.py
│   │   ├── notification_serializers.py
│   │   └── analytics_serializers.py
│   │
│   ├── services/                        # Business Logic Services
│   │   ├── __init__.py
│   │   ├── auth_service.py              # Authentication logic
│   │   ├── order_service.py             # Order processing
│   │   ├── payment_service.py           # Payment processing (Payme/Click)
│   │   ├── delivery_service.py          # Delivery management & routing
│   │   ├── subscription_service.py      # Subscription management
│   │   ├── loyalty_service.py           # Loyalty program logic
│   │   ├── notification_service.py      # Notification system
│   │   ├── analytics_service.py         # Analytics & insights
│   │   ├── file_storage_service.py      # File storage (cloud/local)
│   │   ├── maps_service.py              # Maps integration
│   │   └── prediction_service.py        # AI-powered predictions
│   │
│   ├── utils/                           # Utility Functions
│   │   ├── __init__.py
│   │   ├── decorators.py                # Custom decorators
│   │   ├── validators.py                # Data validation
│   │   ├── helpers.py                   # Helper functions
│   │   ├── constants.py                 # Application constants
│   │   ├── exceptions.py                # Custom exceptions
│   │   └── translations.py              # Multi-language support
│   │
│   ├── tasks/                           # Celery Tasks
│   │   ├── __init__.py
│   │   ├── celery_app.py               # Celery configuration
│   │   ├── payment_tasks.py            # Payment processing tasks
│   │   ├── notification_tasks.py       # Notification tasks
│   │   ├── delivery_tasks.py           # Delivery-related tasks
│   │   ├── analytics_tasks.py          # Analytics processing
│   │   └── subscription_tasks.py       # Subscription billing tasks
│   │
│   ├── templates/                       # Custom HTML Templates
│   │   ├── base.html
│   │   ├── index.html
│   │   ├── products.html
│   │   ├── orders.html
│   │   ├── profile.html
│   │   ├── subscription.html
│   │   └── ... (other custom templates)
│   │
│   ├── static/                          # Static Files
│   │   ├── css/
│   │   ├── js/
│   │   ├── images/
│   │   └── uploads/                     # Local file uploads
│   │
│   └── migrations/                      # Database Migrations
│       └── ... (Flask-Migrate files)
│
├── telegram_bot/                        # Telegram Bot
│   ├── __init__.py
│   ├── main.py                         # Bot entry point
│   ├── config.py                       # Bot configuration
│   │
│   ├── handlers/                       # Bot Command Handlers
│   │   ├── __init__.py
│   │   ├── start_handler.py
│   │   ├── product_handler.py
│   │   ├── order_handler.py
│   │   ├── payment_handler.py
│   │   ├── delivery_handler.py
│   │   ├── subscription_handler.py
│   │   ├── profile_handler.py
│   │   └── admin_handler.py
│   │
│   ├── keyboards/                      # Telegram Keyboards
│   │   ├── __init__.py
│   │   ├── main_keyboard.py
│   │   ├── product_keyboard.py
│   │   ├── order_keyboard.py
│   │   ├── payment_keyboard.py
│   │   └── settings_keyboard.py
│   │
│   ├── services/                       # Bot Services
│   │   ├── __init__.py
│   │   ├── bot_service.py
│   │   ├── message_service.py
│   │   └── webhook_service.py
│   │
│   └── utils/                          # Bot Utilities
│       ├── __init__.py
│       ├── decorators.py
│       ├── helpers.py
│       └── translations.py
│
├── admin_ui/                           # React Admin Dashboard
│   ├── package.json
│   ├── package-lock.json
│   ├── .gitignore
│   ├── public/
│   │   ├── index.html
│   │   └── favicon.ico
│   │
│   ├── src/
│   │   ├── index.js
│   │   ├── App.js
│   │   ├── index.css
│   │   │
│   │   ├── components/                 # Reusable Components
│   │   │   ├── common/
│   │   │   ├── charts/
│   │   │   ├── forms/
│   │   │   └── tables/
│   │   │
│   │   ├── pages/                     # Admin Pages
│   │   │   ├── Dashboard.js
│   │   │   ├── Orders.js
│   │   │   ├── Customers.js
│   │   │   ├── Products.js
│   │   │   ├── Delivery.js
│   │   │   ├── Analytics.js
│   │   │   ├── Settings.js
│   │   │   └── Reports.js
│   │   │
│   │   ├── services/                  # API Services
│   │   │   ├── api.js
│   │   │   ├── auth.js
│   │   │   └── endpoints.js
│   │   │
│   │   ├── utils/
│   │   │   ├── constants.js
│   │   │   ├── helpers.js
│   │   │   └── translations.js
│   │   │
│   │   └── hooks/                     # Custom React Hooks
│   │       ├── useAuth.js
│   │       ├── useApi.js
│   │       └── useTranslation.js
│   │
│   └── build/                         # Production build
│
├── shared/                            # Shared Code
│   ├── __init__.py
│   ├── constants.py                   # Shared constants
│   ├── enums.py                      # Enums used across services
│   └── utils.py                      # Shared utilities
│
├── tests/                            # Test Suite
│   ├── __init__.py
│   ├── conftest.py                   # Pytest configuration
│   ├── test_api/
│   ├── test_services/
│   ├── test_models/
│   └── test_bot/
│
├── docs/                             # Documentation
│   ├── API.md
│   ├── DEPLOYMENT.md
│   ├── DEVELOPMENT.md
│   └── FEATURES.md
│
├── scripts/                          # Deployment Scripts
│   ├── init_db.py
│   ├── migrate.py
│   ├── seed_data.py
│   └── backup.py
│
└── logs/                            # Application Logs
    ├── app.log
    ├── celery.log
    └── bot.log
```

## Key Architecture Decisions

### 1. **Modular Design**
- Separated concerns into distinct modules
- Models fragmented by domain (user, product, order, etc.)
- Services layer for business logic
- Clear separation between API, bot, and admin UI

### 2. **Technology Stack**
- **Backend**: Flask + SQLAlchemy + PostgreSQL
- **Task Queue**: Celery + Redis
- **Bot**: python-telegram-bot
- **Admin UI**: React
- **Deployment**: Docker + Docker Compose
- **File Storage**: Configurable (AWS S3/Local)
- **Maps**: Configurable (Google Maps/Yandex/OpenStreetMap)

### 3. **Multi-language Support**
- Translation files in each service
- Support for Uzbek, Russian, English
- Consistent across web app, API, and bot

### 4. **Security & Compliance**
- Payment Links for PCI compliance
- Secure webhook handlers
- Admin access controls
- Data privacy settings

### 5. **Scalability Features**
- Celery for background tasks
- Redis for caching and sessions
- Modular architecture for easy scaling
- Docker containerization

### 6. **Advanced Features**
- AI-powered order prediction
- Real-time delivery tracking
- Dynamic pricing engine
- Comprehensive analytics
- Smart notifications

This structure ensures:
- **Maintainability**: Clear separation of concerns
- **Scalability**: Modular design allows independent scaling
- **Extensibility**: Easy to add new features
- **Testability**: Isolated components for unit testing
- **Production-ready**: Includes logging, monitoring, and deployment configs
