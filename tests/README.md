# BlueStream Testing Framework

Comprehensive automated testing pipeline for the BlueStream water delivery platform.

## Overview

This testing framework provides:
- **Unit Tests**: Test individual components and functions
- **Integration Tests**: Test component interactions and API flows
- **Security Tests**: Test for vulnerabilities and security compliance
- **Performance Tests**: Test response times and throughput
- **End-to-End Tests**: Test complete user workflows

## Quick Start

### Running All Tests
```bash
python run_tests.py
```

### Running Specific Test Suites
```bash
# Unit tests only
python run_tests.py --suite unit

# Security tests only  
python run_tests.py --suite security

# Performance tests only
python run_tests.py --suite performance
```

### Common Testing Scenarios

```bash
# Fast feedback during development
python run_tests.py --suite unit --fast

# Security-focused testing
python run_tests.py --security-only

# Critical tests only
python run_tests.py --critical-only

# CI/CD environment
python run_tests.py --env ci --no-parallel

# Performance testing without coverage
python run_tests.py --suite performance --no-coverage
```

## Test Structure

```
tests/
├── conftest.py                 # Test configuration and fixtures
├── pipeline/
│   └── test_runner.py          # Automated test pipeline
├── unit/                       # Unit tests
│   ├── test_auth_service.py
│   ├── test_order_service.py
│   ├── test_payment_service.py
│   └── ...
├── integration/                # Integration tests
│   ├── test_api_endpoints.py
│   ├── test_order_flow.py
│   └── ...
├── security/                   # Security tests
│   └── test_security_vulnerabilities.py
├── performance/                # Performance tests
│   ├── test_api_performance.py
│   └── baseline.json
└── README.md                   # This file
```

## Test Categories and Markers

Tests are organized using pytest markers:

- `@pytest.mark.unit` - Unit tests
- `@pytest.mark.integration` - Integration tests  
- `@pytest.mark.security` - Security tests
- `@pytest.mark.performance` - Performance tests
- `@pytest.mark.critical` - Critical business logic tests
- `@pytest.mark.slow` - Long-running tests
- `@pytest.mark.api` - API endpoint tests
- `@pytest.mark.auth` - Authentication tests
- `@pytest.mark.payment` - Payment-related tests
- `@pytest.mark.order` - Order-related tests

## Configuration

### Test Configuration File (`test_config.json`)

```json
{
  "parallel_execution": true,
  "max_workers": 4,
  "fail_fast": false,
  "strict_coverage": true,
  "security_scan_enabled": true,
  "coverage_requirements": {
    "unit": 85,
    "integration": 75,
    "security": 90,
    "overall": 80
  }
}
```

### Environment Variables

Required for testing:
```bash
SECRET_KEY=test-secret-key-for-testing-32-chars-long
JWT_SECRET_KEY=test-jwt-secret-key-for-testing
DB_PASSWORD=test_password
FLASK_ENV=testing
REDIS_URL=redis://localhost:6379/15
DATABASE_URL=postgresql://test_user:test_password@localhost:5432/test_db
```

## Test Data and Fixtures

### Available Fixtures

- `app` - Flask application instance
- `client` - Test client for API calls
- `db` - Database with clean state
- `sample_user` - Test user account
- `admin_user` - Admin user account
- `delivery_driver` - Delivery driver account
- `sample_product` - Test product
- `sample_order` - Test order
- `sample_payment` - Test payment
- `auth_headers` - Authentication headers
- `mock_redis` - Mocked Redis client
- `malicious_payloads` - Security test payloads

### Creating Test Data

```python
def test_order_creation(client, db, sample_user, sample_product):
    """Test order creation with fixtures"""
    order_data = {
        'items': [{'product_id': sample_product.id, 'quantity': 2}],
        'delivery_address': {'address_line1': '123 Test St'}
    }
    
    response = client.post('/api/v1/orders', json=order_data)
    assert response.status_code == 201
```

## Coverage Requirements

- **Unit Tests**: 85% minimum coverage
- **Integration Tests**: 75% minimum coverage  
- **Security Tests**: 90% minimum coverage
- **Overall**: 80% minimum coverage

Coverage reports are generated in:
- `test_reports/htmlcov/` - HTML reports
- `test_reports/*_coverage.xml` - XML reports

## Security Testing

### Automated Security Scans

The pipeline includes:
- **Bandit**: Static security analysis
- **Safety**: Dependency vulnerability scanning
- **Custom Security Tests**: SQL injection, XSS, authentication bypass

### Security Test Examples

```python
@pytest.mark.security
def test_sql_injection_protection(client, malicious_payloads):
    """Test SQL injection protection"""
    for payload in malicious_payloads['sql_injection']:
        response = client.get(f'/api/v1/products?search={payload}')
        assert response.status_code != 500  # Should not cause server error
```

## Performance Testing

### Performance Metrics

- **Response Time**: P95 < 2 seconds
- **Throughput**: > 100 requests/second
- **Error Rate**: < 5%
- **Memory Usage**: < 50MB increase over 200 requests

### Load Testing

```python
@pytest.mark.performance
def test_api_load(performance_client):
    """Test API under load"""
    metrics = load_test_endpoint(
        performance_client,
        '/api/v1/products',
        concurrent_users=50,
        requests_per_user=10
    )
    
    assert metrics.get_statistics()['error_rate'] < 5
```

## CI/CD Integration

### GitHub Actions

The project includes a comprehensive GitHub Actions workflow (`.github/workflows/test_pipeline.yml`) that:

1. **Code Quality**: Linting, formatting, type checking
2. **Unit Tests**: Fast feedback with high coverage
3. **Integration Tests**: API and service integration  
4. **Security Tests**: Vulnerability scanning
5. **Performance Tests**: Load and stress testing
6. **Build Tests**: Docker and deployment validation

### Workflow Triggers

- **Push to main/develop**: Full test suite
- **Pull Requests**: Full test suite with PR comments
- **Scheduled**: Daily security and performance tests
- **Manual**: Custom test suite selection

### Environment Setup

Each CI job includes:
- PostgreSQL and Redis services
- Python environment with dependencies
- Test database initialization
- Environment variable configuration

## Local Development

### Prerequisites

1. **Python 3.12+**
2. **PostgreSQL** (for integration tests)
3. **Redis** (for caching tests)
4. **Dependencies**: `pip install -r requirements.txt`

### Running Tests Locally

```bash
# Install test dependencies
pip install pytest pytest-cov pytest-xdist pytest-timeout

# Run tests with coverage
python run_tests.py --html-report

# Run specific test file
pytest tests/unit/test_auth_service.py -v

# Run with markers
pytest -m "unit and critical" -v

# Debug failing test
pytest tests/unit/test_auth_service.py::TestUserAuthentication::test_login -vvs
```

### Test Development Guidelines

1. **Test Naming**: Use descriptive names that explain what is being tested
2. **Test Structure**: Follow Arrange-Act-Assert pattern
3. **Fixtures**: Use fixtures for common test data
4. **Mocking**: Mock external services and dependencies
5. **Assertions**: Use specific assertions with helpful error messages
6. **Coverage**: Aim for high coverage but focus on critical paths

### Example Test Structure

```python
@pytest.mark.unit
@pytest.mark.critical
@pytest.mark.auth
class TestUserAuthentication:
    """Test user authentication functionality"""
    
    def test_valid_login_returns_success(self, auth_service, sample_user):
        """Test that valid credentials return successful authentication"""
        # Arrange
        email = sample_user.email
        password = 'valid_password'
        
        # Act
        result = auth_service.authenticate_user(email, password)
        
        # Assert
        assert result['success'] is True
        assert result['user_id'] == sample_user.id
        assert 'access_token' in result
    
    def test_invalid_credentials_return_error(self, auth_service):
        """Test that invalid credentials return appropriate error"""
        # Arrange
        email = 'invalid@example.com'
        password = 'wrong_password'
        
        # Act
        result = auth_service.authenticate_user(email, password)
        
        # Assert
        assert result['success'] is False
        assert 'error' in result
        assert result['error'] == 'Invalid credentials'
```

## Troubleshooting

### Common Issues

**Tests failing with database errors:**
```bash
# Reset test database
python scripts/init_db.py --env testing
```

**Redis connection errors:**
```bash
# Start Redis for testing
redis-server --port 6379 --daemonize yes
```

**Import errors:**
```bash
# Ensure project root is in PYTHONPATH
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

**Coverage not generated:**
```bash
# Install coverage dependencies
pip install pytest-cov coverage[toml]
```

### Debug Mode

```bash
# Run with verbose output and no capture
pytest -vvs tests/unit/test_auth_service.py

# Run with pdb debugger
pytest --pdb tests/unit/test_auth_service.py

# Run single test with full output
pytest tests/unit/test_auth_service.py::test_specific_function -vvs
```

## Reporting and Monitoring

### Test Reports

Generated reports include:
- **JUnit XML**: `test_reports/*_results.xml`
- **Coverage HTML**: `test_reports/htmlcov/`
- **Coverage XML**: `test_reports/*_coverage.xml`
- **Security Reports**: `test_reports/bandit_report.json`
- **Performance Baseline**: `tests/performance/baseline.json`

### Monitoring

The test pipeline provides:
- **Test execution metrics**
- **Coverage trends**
- **Performance baselines**
- **Security vulnerability tracking**
- **Failure analysis and reporting**

## Contributing

When adding new tests:

1. **Choose appropriate test type** (unit/integration/security/performance)
2. **Add proper markers** (`@pytest.mark.unit`, etc.)
3. **Use existing fixtures** when possible
4. **Follow naming conventions** (`test_*` functions)
5. **Update documentation** for new test categories
6. **Ensure tests are deterministic** and don't depend on external state

### Test Review Checklist

- [ ] Tests are properly categorized with markers
- [ ] Tests use appropriate fixtures and mocks
- [ ] Tests are deterministic and repeatable
- [ ] Tests have clear, descriptive names
- [ ] Tests cover both happy path and error cases
- [ ] Tests include security considerations
- [ ] Tests maintain or improve coverage
- [ ] Tests run quickly (< 1s for unit tests)

## License

This testing framework is part of the BlueStream project and follows the same license terms.