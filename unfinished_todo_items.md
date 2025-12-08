# Unfinished TODO Items

## 🟠 HIGH Priority

- [ ] Unify session management - Link bot_sessions to user_sessions
- [ ] Implement cross-platform session revocation
- [ ] Add logout from all devices functionality
- [ ] Fix password reset for telegram-originated users
- [ ] Add telegram notification for password set/change events
- [ ] Complete check_token_blacklist decorator implementation
- [ ] Apply token blacklist middleware to all protected endpoints
- [ ] Test token revocation thoroughly across platforms

## 🟡 MEDIUM Priority

- [ ] Implement account_locked_until in _increment_failed_attempts
- [ ] Add unlock mechanism for admins
- [ ] Add notification when account is locked
- [ ] Reject requests on role mismatch instead of just logging
- [ ] Force token refresh on role change
- [ ] Add admin audit log for role changes
- [ ] Implement token invalidation on role/permission changes
- [ ] Log all authentication events (login, logout, failures)
- [ ] Log authorization failures to audit trail
- [ ] Add retention policy for audit logs
- [ ] Add JSON schema validation for bot state
- [ ] Create typed state classes for bot
- [ ] Implement state migration system for bot

## 🔵 LOW Priority

- [ ] Add monitoring for password rehash failures
- [ ] Log successful password rehashes
- [ ] Add admin view to see users needing password rehash
- [ ] Update email validator to flag placeholder emails
- [ ] Add email_is_placeholder boolean field to users table
- [ ] Enforce real email collection for certain operations
- [ ] Add platform-specific token expiry (longer for telegram)
- [ ] Implement remember me functionality
- [ ] Add refresh token rotation
- [ ] Implement sliding token expiration

## ⚙️ INFRASTRUCTURE

- [ ] Set up Flask-Migrate properly with migrations/ directory
- [ ] Add migration testing workflow
- [ ] Document database migration strategy
- [ ] Fix unused postgres_data volume in docker-compose
- [ ] Remove .env files from git tracking (security risk)
- [ ] Consolidate multiple .env files into proper structure
- [ ] Review and update .env.example with all required vars
- [ ] Add health check endpoint implementation
- [ ] Fix telegram_bot healthcheck - currently depends on business_app
- [ ] Review and optimize Docker resource limits
- [ ] Enable and configure nginx reverse proxy
- [ ] Add SSL/TLS certificate configuration

## 🔒 SECURITY

- [ ] Implement rate limiting on auth endpoints
- [ ] Add CSRF protection for state-changing operations
- [ ] Implement multi-factor authentication (MFA)
- [ ] Add IP whitelisting for admin operations
- [ ] Implement session fingerprinting to detect hijacking
- [ ] Complete security headers implementation
- [ ] Implement Content Security Policy for API
- [ ] Add request signature validation for telegram webhook
- [ ] Implement API key rotation mechanism
- [ ] Add secrets scanning in CI/CD

## 🔧 CLEANUP

- [ ] Consolidate duplicate password validation logic
- [ ] Remove redundant user validation between models and services
- [ ] Consolidate Redis client initialization across services
- [ ] Remove duplicate error handling decorators
- [ ] Review and remove unused API endpoints
- [ ] Consolidate auth decorators - too many similar ones

## 📊 SYNC

- [ ] Add automatic account linking suggestions UI
- [ ] Implement seamless platform switching
- [ ] Add platform activity indicators
- [ ] Synchronize user preferences in real-time
- [ ] Add cross-platform notification settings sync
- [ ] Implement unified user profile view

## 🧪 TESTING

- [ ] Add integration tests for auth flows
- [ ] Add tests for cross-platform sync
- [ ] Add tests for token blacklisting
- [ ] Add tests for session management
- [ ] Add security penetration tests

## 📝 DOCUMENTATION

- [ ] Document authentication architecture
- [ ] Document cross-platform sync flow
- [ ] Add API documentation for all endpoints
- [ ] Document deployment process
- [ ] Create troubleshooting guide

## 🔄 MONITORING

- [ ] Set up error tracking with Sentry
- [ ] Add performance monitoring
- [ ] Set up log aggregation
- [ ] Add alerting for critical errors
- [ ] Implement metrics dashboard
