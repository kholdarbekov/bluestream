# Configuration Management

This document explains how to manage environment configurations for the BlueStream Water Platform.

## Overview

The application supports multiple environments with separate configuration files:

- **Development**: Local development with debug features
- **Staging**: Pre-production testing environment  
- **Production**: Live production environment
- **Testing**: Automated testing environment

## Environment Files

Each environment has its own configuration file:

- `.env.development` - Development environment
- `.env.staging` - Staging environment  
- `.env.production` - Production environment
- `.env.testing` - Testing environment

## Quick Start

### 1. Set Up Development Environment

```bash
# Copy development template to .env
cp .env.development .env

# Edit .env with your local settings
nano .env

# Validate configuration
flask config validate

# Check configuration info
flask config info --detailed
```

### 2. Switch Environments

```bash
# Switch to staging
flask config switch staging

# Switch to production (be careful!)
flask config switch production --force

# Check current environment
flask config info
```

### 3. Validate Configuration

```bash
# Validate current environment
flask config validate

# Check specific environment without switching
flask config check staging

# Security check
flask config security-check --check-secrets
```

## Configuration Structure

### Base Configuration

All environments inherit from `BaseConfig` in `business_app.config.base`:

```python
from business_app.config import get_config

config = get_config()  # Gets config based on FLASK_ENV
```

### Environment-Specific Settings

#### Development (`DevelopmentConfig`)
- Debug mode enabled
- SQL logging enabled
- Lenient rate limiting (1000/hour)
- Local file storage
- File-based email backend
- Test payment gateways

#### Staging (`StagingConfig`)
- Production-like security
- Test payment gateways
- S3 file storage
- Sentry error tracking
- Enhanced security headers

#### Production (`ProductionConfig`)
- Maximum security settings
- Live payment gateways
- Strict rate limiting (100/hour)
- Required error tracking
- SSL/HTTPS enforcement
- Comprehensive monitoring

#### Testing (`TestingConfig`)
- In-memory SQLite database
- Synchronous task execution
- Disabled external services
- Mock services for testing

## Environment Variables

### Required Variables

All environments require:
```bash
SECRET_KEY=your-secret-key-32-chars-minimum
DB_PASSWORD=your-database-password
```

Production additionally requires:
```bash
REDIS_URL=redis://your-redis-host:6379/0
SENTRY_DSN=https://your-sentry-dsn@sentry.io/project
JWT_SECRET_KEY=your-jwt-secret-key
SENDGRID_API_KEY=your-sendgrid-api-key
```

### Security Best Practices

1. **Never commit secrets to version control**
2. **Use environment-specific secret management**
3. **Rotate secrets regularly**
4. **Use different secrets for each environment**

### Database Configuration

#### Development
```bash
DB_HOST=localhost
DB_USER=postgres
DB_PASSWORD=postgres
DB_NAME=bluestream_dev
```

#### Production
```bash
# Use DATABASE_URL for production
DATABASE_URL=postgresql://user:pass@host:5432/bluestream_prod

# Or individual components
DB_HOST=your-prod-db-host
DB_USER=bluestream_prod_user
DB_PASSWORD=secure-production-password
DB_NAME=bluestream_prod
```

## CLI Commands

### Configuration Management

```bash
# Show current configuration
flask config info

# Show detailed configuration
flask config info --detailed

# Validate configuration
flask config validate

# List available environments
flask config list-envs

# Switch environment
flask config switch <environment>

# Check environment without switching
flask config check <environment>

# Get specific configuration value
flask config get-value SECRET_KEY
flask config get-value SECRET_KEY staging

# Security check
flask config security-check
flask config security-check --check-secrets
```

### Database Commands

```bash
# Initialize database
flask init-db

# Seed with test data
flask seed-data

# Create admin user
flask create-admin
```

## Deployment

### Docker Environment

```dockerfile
# Set environment in Dockerfile
ENV FLASK_ENV=production

# Or pass via docker run
docker run -e FLASK_ENV=production your-app
```

### Environment Detection

The application checks these variables in order:
1. `FLASK_ENV`
2. `APP_ENV` 
3. `ENVIRONMENT`

Falls back to `development` if none are set.

### Production Checklist

Before deploying to production:

1. ✅ Set `FLASK_ENV=production`
2. ✅ Replace all placeholder secrets
3. ✅ Configure database with SSL
4. ✅ Set up Redis with authentication
5. ✅ Configure S3 bucket and IAM
6. ✅ Set up Sentry error tracking
7. ✅ Configure email service (SendGrid)
8. ✅ Set up SMS service (Twilio)
9. ✅ Configure payment gateways
10. ✅ Run security check: `flask config security-check`

## Troubleshooting

### Common Issues

#### Missing Environment Variables
```bash
ValueError: Missing required environment variables: SECRET_KEY, DB_PASSWORD
```
**Solution**: Set required environment variables or copy appropriate `.env.{environment}` file.

#### Configuration Validation Errors
```bash
ValueError: DEBUG mode must be disabled in production environment
```
**Solution**: Check environment variables and ensure production settings are correct.

#### Database Connection Issues
```bash
sqlalchemy.exc.OperationalError: could not connect to server
```
**Solution**: Verify database credentials and network connectivity.

### Debug Configuration

```python
# In Python shell or script
from business_app.config import get_environment_info, validate_environment

# Get environment info
info = get_environment_info()
print(info)

# Validate configuration
is_valid, message = validate_environment()
print(f"Valid: {is_valid}, Message: {message}")
```

## Security Considerations

### Production Security

- All cookies marked as secure (HTTPS only)
- Strict Content Security Policy
- HSTS headers enabled
- XSS protection enabled
- No debug information exposed

### Staging Security

- Similar to production but with test payment gateways
- Relaxed CSP for testing
- Shorter session timeouts

### Development Security

- Relaxed security for development convenience
- Debug mode enabled
- Local file storage
- Mock external services

## Migration from Legacy Config

The new configuration system maintains backward compatibility:

```python
# Old way (still works)
from business_app.config import Config, get_config

# New way (recommended)
from business_app.config import DevelopmentConfig, ProductionConfig, get_config
```

Legacy `config.py` file redirects to new configuration package.