# Docker Secrets Directory

This directory contains sensitive secrets for the Blue Stream Water Business Platform.

## Security Notice

- **NEVER** commit secret files to version control
- Keep this directory secure with appropriate file permissions (700)
- Use different secrets for different environments
- Rotate secrets regularly
- Use strong, randomly generated values

## Secret Files

Each file should contain only the secret value (no newlines or extra whitespace).

### Required Secrets:
- `postgres_password` - PostgreSQL database password
- `secret_key` - Flask application secret key
- `telegram_bot_token` - Telegram bot API token

### Optional Secrets:
- `payme_secret_key` - PayMe payment gateway secret
- `click_secret_key` - Click payment gateway secret
- `sendgrid_api_key` - SendGrid email API key
- `twilio_auth_token` - Twilio SMS API token
- `google_maps_api_key` - Google Maps API key
- `yandex_maps_api_key` - Yandex Maps API key
- `aws_secret_access_key` - AWS secret access key
- `stripe_secret_key` - Stripe payment secret key
- `encryption_key` - Application encryption key
- `redis_password` - Redis authentication password

## Usage

Use the `manage-secrets.sh` script to manage these secrets safely.
