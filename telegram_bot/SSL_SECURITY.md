# SSL Security Configuration for Telegram Bot

## Overview

The Telegram Bot API client now enforces SSL certificate validation by default to prevent man-in-the-middle attacks and ensure secure communication with the business application.

## Configuration Options

### Environment Variables

- `BUSINESS_API_SSL_VERIFY`: Enable/disable SSL verification (default: `true`)
- `BUSINESS_API_SSL_CERT_PATH`: Path to custom SSL certificate (optional)
- `BUSINESS_API_TIMEOUT`: HTTP request timeout in seconds (default: `30`)
- `BUSINESS_API_MAX_RETRIES`: Maximum retry attempts (default: `3`)

### Production Configuration (Recommended)

```bash
# Enable SSL verification (REQUIRED for production)
BUSINESS_API_SSL_VERIFY=true

# Use HTTPS URL for production
BUSINESS_APP_URL=https://api.bluestream.uz

# Optional: Custom certificate path
# BUSINESS_API_SSL_CERT_PATH=/etc/ssl/certs/bluestream.pem
```

### Development Configuration

```bash
# For development with self-signed certificates ONLY
BUSINESS_API_SSL_VERIFY=false
BUSINESS_APP_URL=http://localhost:80
```

## Security Implications

### SSL Verification Enabled (Recommended)
- ✅ Prevents man-in-the-middle attacks
- ✅ Validates server identity
- ✅ Ensures data encryption in transit
- ✅ Meets security compliance standards

### SSL Verification Disabled (HIGH RISK)
- ❌ Vulnerable to man-in-the-middle attacks
- ❌ No server identity validation
- ❌ Data could be intercepted
- ❌ Should NEVER be used in production

## Error Handling

### Common SSL Errors

1. **Certificate Verification Failed**
   ```
   SSL certificate validation failed: certificate verify failed: self signed certificate
   ```
   **Solution**: Obtain a valid SSL certificate or configure custom certificate path

2. **Hostname Mismatch**
   ```
   SSL certificate validation failed: hostname 'api.example.com' doesn't match certificate
   ```
   **Solution**: Ensure certificate matches the hostname in BUSINESS_APP_URL

3. **Certificate Expired**
   ```
   SSL certificate validation failed: certificate has expired
   ```
   **Solution**: Renew the SSL certificate

4. **Custom Certificate Not Found**
   ```
   SSL certificate file not found: /path/to/cert.pem
   ```
   **Solution**: Verify the certificate path and file permissions

## Best Practices

### For Production Deployments

1. **Always enable SSL verification**:
   ```bash
   BUSINESS_API_SSL_VERIFY=true
   ```

2. **Use HTTPS URLs**:
   ```bash
   BUSINESS_APP_URL=https://api.yourcompany.com
   ```

3. **Use valid SSL certificates** from a trusted CA

4. **Monitor SSL certificate expiration** and renew before expiry

5. **Test SSL configuration** during deployment

### For Development

1. **Use proper staging certificates** when possible

2. **Only disable SSL verification** for local development with self-signed certificates

3. **Never commit** `BUSINESS_API_SSL_VERIFY=false` to production branches

4. **Document** any SSL-related configuration changes

## Certificate Management

### Using Custom Certificates

If you need to use a custom certificate (e.g., corporate CA):

1. Place the certificate file on the server:
   ```bash
   /etc/ssl/certs/company-ca.pem
   ```

2. Configure the path:
   ```bash
   BUSINESS_API_SSL_CERT_PATH=/etc/ssl/certs/company-ca.pem
   ```

3. Ensure proper file permissions:
   ```bash
   chmod 644 /etc/ssl/certs/company-ca.pem
   ```

### Certificate Formats

Supported certificate formats:
- PEM (.pem, .crt)
- DER (.der)
- PKCS#7 (.p7b)

## Monitoring and Alerts

### Recommended Monitoring

1. **SSL Certificate Expiration**: Monitor certificate expiry dates
2. **Connection Failures**: Alert on SSL-related connection failures
3. **Configuration Changes**: Audit SSL configuration changes

### Health Checks

The API client performs an automatic SSL connection test on startup when SSL verification is enabled. Monitor startup logs for SSL-related errors.

## Troubleshooting

### Debug SSL Issues

Enable debug logging to troubleshoot SSL problems:

```python
import logging
logging.getLogger('api_client').setLevel(logging.DEBUG)
```

### Common Solutions

1. **Self-signed certificates in development**:
   ```bash
   BUSINESS_API_SSL_VERIFY=false  # Development only!
   ```

2. **Corporate firewall/proxy**:
   - Configure proxy settings in HTTP client
   - Add corporate CA certificates

3. **Load balancer issues**:
   - Ensure load balancer passes through SSL correctly
   - Check certificate chain completeness

## Security Audit Checklist

- [ ] SSL verification enabled in production
- [ ] Valid SSL certificates from trusted CA
- [ ] Certificate expiration monitoring in place
- [ ] No hardcoded SSL bypass in production code
- [ ] Regular security scans for SSL vulnerabilities
- [ ] Proper certificate chain configuration
- [ ] Strong cipher suites configured
- [ ] TLS version 1.2 or higher enforced

## Contact

For SSL-related security questions or issues, contact the security team or refer to the main security documentation.