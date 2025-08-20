# BlueStream Platform - Pending TODO Items

This document contains all the pending tasks that need to be completed for the BlueStream water delivery platform.

## Medium Priority Tasks

### 75b. Audit Node.js dependencies for vulnerabilities
- **Description**: Scan admin_ui Node.js dependencies for known security vulnerabilities
- **Files**: `admin_ui/package.json`, `admin_ui/package-lock.json`
- **Actions**: Run `npm audit`, review and update vulnerable packages
- **Impact**: Security improvements for admin interface

### 75c. Update vulnerable dependencies with security patches
- **Description**: Apply security patches to vulnerable dependencies found in audit
- **Dependencies**: Completion of task 75b
- **Actions**: Update packages to patched versions, test compatibility
- **Impact**: Eliminate known security vulnerabilities

### 76. Review and tighten CORS configuration
- **Description**: Review and enhance Cross-Origin Resource Sharing settings
- **Files**: `business_app/config/*.py`, `business_app/__init__.py`
- **Current Issues**: May have overly permissive CORS settings
- **Actions**: Restrict origins, methods, and headers appropriately
- **Impact**: Improved security against cross-origin attacks

### 77. Audit and enhance rate limiting implementation
- **Description**: Review current rate limiting and add missing protections
- **Files**: `business_app/utils/decorators.py`, API endpoints
- **Actions**: Add rate limiting to all sensitive endpoints, tune limits
- **Impact**: Protection against abuse and DoS attacks

## Low Priority Tasks

### 29. Implement session cleanup for Telegram bot
- **Description**: Add session management and cleanup for Telegram bot users
- **Files**: `telegram_bot/`, `business_app/services/session_cleanup_service.py`
- **Actions**: Implement bot session tracking and cleanup mechanisms
- **Impact**: Better resource management for bot sessions

### 30. Add session size limits
- **Description**: Implement limits on session data size to prevent memory issues
- **Files**: Session management components
- **Actions**: Add validation for session data size
- **Impact**: Prevent memory exhaustion from large sessions

### 31. Implement repository pattern
- **Description**: Refactor data access to use repository pattern
- **Files**: `business_app/models/`, `business_app/services/`
- **Actions**: Create repository classes for each model
- **Impact**: Better separation of concerns and testability

### 32. Separate business logic from API logic
- **Description**: Move business logic from API endpoints to service layer
- **Files**: `business_app/api/`, `business_app/services/`
- **Actions**: Extract business logic to dedicated service methods
- **Impact**: Better code organization and reusability

### 33. Add error boundaries in React components
- **Description**: Implement error boundaries for better error handling in admin UI
- **Files**: `admin_ui/src/components/`
- **Actions**: Create error boundary components, wrap key UI sections
- **Impact**: Better user experience when errors occur

### 34. Implement retry logic for failed API calls
- **Description**: Add automatic retry mechanism for failed API requests
- **Files**: `admin_ui/src/services/`
- **Actions**: Implement exponential backoff retry logic
- **Impact**: Better resilience to temporary network issues

### 35. Update README and setup documentation
- **Description**: Comprehensive documentation for project setup and development
- **Files**: `README.md`, `docs/`
- **Actions**: Document installation, configuration, and development workflow
- **Impact**: Easier onboarding for new developers

### 36. Create architecture documentation
- **Description**: Document system architecture and design decisions
- **Files**: `docs/ARCHITECTURE.md`
- **Actions**: Create diagrams and explanations of system components
- **Impact**: Better understanding of system design

### 37. Write deployment guide
- **Description**: Document production deployment procedures
- **Files**: `docs/DEPLOYMENT.md`
- **Actions**: Step-by-step deployment instructions, environment setup
- **Impact**: Reliable production deployments

### 57. Fix order number generation to be less predictable
- **Description**: Implement secure order number generation
- **Files**: `business_app/services/order_service.py`
- **Current Issue**: Sequential order numbers are predictable
- **Actions**: Use UUID or secure random generation
- **Impact**: Prevent order number enumeration attacks

### 58. Add constraints to prevent negative quantities in orders
- **Description**: Add database and application constraints for order quantities
- **Files**: `schema.sql`, `business_app/models/order.py`
- **Actions**: Add CHECK constraints and validation logic
- **Impact**: Data integrity for order quantities

### 59. Improve payment ID generation to be less predictable
- **Description**: Enhance payment ID generation security
- **Files**: `business_app/services/payment_service.py`
- **Current Issue**: Predictable payment IDs
- **Actions**: Use cryptographically secure random generation
- **Impact**: Prevent payment ID guessing attacks

### 60. Add proper JSON validation for bot_state field
- **Description**: Validate JSON structure for Telegram bot state
- **Files**: `business_app/models/user.py`, `telegram_bot/`
- **Actions**: Add JSON schema validation for bot_state
- **Impact**: Data integrity for bot conversation state

### 61. Remove hardcoded localhost from CORS origins in production
- **Description**: Environment-specific CORS configuration
- **Files**: `business_app/config/production.py`
- **Current Issue**: Localhost in production CORS origins
- **Actions**: Use environment variables for CORS origins
- **Impact**: Proper production security configuration

### 62. Add proper error filtering for Sentry configuration
- **Description**: Configure Sentry to filter out noise and focus on important errors
- **Files**: `business_app/config/*.py`
- **Actions**: Add error filtering rules, set up proper tags
- **Impact**: Better error monitoring signal-to-noise ratio

### 63. Expand weak password list and make it configurable
- **Description**: Enhance password security with comprehensive weak password detection
- **Files**: `business_app/utils/password_security.py`
- **Actions**: Add more weak passwords, make list configurable
- **Impact**: Better protection against weak passwords

### 64. Make order quantity validation business rules configurable
- **Description**: Make order validation rules configurable per deployment
- **Files**: `business_app/utils/order_validators.py`
- **Actions**: Move validation rules to configuration
- **Impact**: Flexible business rule management

### 65. Fix rate limiting fallback behavior when Redis is down
- **Description**: Improve rate limiting resilience when Redis is unavailable
- **Files**: `business_app/utils/decorators.py`
- **Current Issue**: Rate limiting may fail when Redis is down
- **Actions**: Implement fallback rate limiting mechanism
- **Impact**: Better system resilience

### 66. Improve exception handling to avoid masking errors
- **Description**: Review and improve exception handling throughout the application
- **Files**: Multiple service and API files
- **Actions**: Remove overly broad exception handlers, improve error reporting
- **Impact**: Better debugging and error visibility

### 67. Implement proper IP whitelist blocking instead of just logging
- **Description**: Enhance IP blocking from logging to actual blocking
- **Files**: Security middleware components
- **Actions**: Implement proper IP blocking mechanism
- **Impact**: Active protection against malicious IPs

### 78. Fix telegram bot token exposure in logs
- **Description**: Prevent bot token from appearing in log files
- **Files**: `telegram_bot/`, logging configuration
- **Actions**: Add token sanitization in logging
- **Impact**: Prevent credential exposure in logs

### 79. Enable Redis authentication and network restrictions
- **Description**: Secure Redis instance with authentication and network controls
- **Files**: `redis/redis.conf`, `docker-compose.yml`
- **Actions**: Configure Redis AUTH, bind to specific interfaces
- **Impact**: Enhanced Redis security

## Critical Issue Currently Being Resolved

### Database Schema Mismatch (In Progress)
- **Issue**: Product model uses `category_id` foreign key but schema has `category` enum
- **Status**: Schema has been corrected, containers restarted, database initialization pending
- **Next Steps**: Initialize database with corrected schema and test functionality

## Notes

- All tasks should be thoroughly tested before deployment
- Security-related tasks should be prioritized
- Consider impact on existing functionality when implementing changes
- Update this document as tasks are completed

---

**Created**: August 18, 2025  
**Last Updated**: August 18, 2025  
**Total Pending Tasks**: 25