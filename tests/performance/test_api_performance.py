"""
Performance tests for API endpoints
Tests response times, throughput, and resource usage under load
"""
import pytest
import time
import threading
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any
import requests
import json
from decimal import Decimal
from unittest.mock import patch

from business_app import create_app
from business_app.models.user import User
from business_app.models.product import Product
from business_app.utils.constants import UserRole


@pytest.fixture(scope='class')
def performance_app():
    """Create app for performance testing"""
    class PerformanceConfig:
        TESTING = True
        WTF_CSRF_ENABLED = False
        SECRET_KEY = 'test-secret-key-for-testing-32-chars-long'
        SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        JWT_SECRET_KEY = 'test-jwt-secret-key-for-testing'
        REDIS_URL = 'redis://localhost:6379/15'
        CELERY_ALWAYS_EAGER = True
        
        @classmethod
        def validate_secret_key(cls):
            pass
        
        @classmethod
        def validate_debug_mode(cls):
            pass
    
    app = create_app(PerformanceConfig)
    
    with app.app_context():
        from business_app import db
        db.create_all()
        
        # Create test data
        user = User(
            email='perf@test.com',
            phone='+998901234567',
            password_hash='$2b$12$test.hash.for.testing.purposes.only',
            first_name='Performance',
            last_name='User',
            role=UserRole.CUSTOMER,
            is_verified=True
        )
        db.session.add(user)
        
        for i in range(50):  # Create 50 test products
            product = Product(
                name=f'Test Product {i}',
                description=f'Performance test product {i}',
                category='water',
                size='large',
                volume=Decimal('19.00'),
                volume_unit='L',
                base_price=Decimal('15000.00'),
                stock_quantity=100,
                is_active=True
            )
            db.session.add(product)
        
        db.session.commit()
        yield app


@pytest.fixture
def performance_client(performance_app):
    """Create performance test client"""
    return performance_app.test_client()


class PerformanceMetrics:
    """Container for performance metrics"""
    
    def __init__(self):
        self.response_times = []
        self.status_codes = []
        self.errors = []
        self.start_time = None
        self.end_time = None
    
    def add_response(self, response_time: float, status_code: int, error: str = None):
        """Add a response measurement"""
        self.response_times.append(response_time)
        self.status_codes.append(status_code)
        if error:
            self.errors.append(error)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Calculate performance statistics"""
        if not self.response_times:
            return {}
        
        total_requests = len(self.response_times)
        successful_requests = len([s for s in self.status_codes if 200 <= s < 300])
        error_rate = (total_requests - successful_requests) / total_requests * 100
        
        duration = self.end_time - self.start_time if self.start_time and self.end_time else 0
        throughput = total_requests / duration if duration > 0 else 0
        
        return {
            'total_requests': total_requests,
            'successful_requests': successful_requests,
            'error_rate': error_rate,
            'duration': duration,
            'throughput': throughput,  # requests per second
            'response_times': {
                'min': min(self.response_times),
                'max': max(self.response_times),
                'mean': statistics.mean(self.response_times),
                'median': statistics.median(self.response_times),
                'p95': self._percentile(self.response_times, 95),
                'p99': self._percentile(self.response_times, 99)
            },
            'errors': len(self.errors)
        }
    
    def _percentile(self, data: List[float], percentile: int) -> float:
        """Calculate percentile"""
        if not data:
            return 0
        sorted_data = sorted(data)
        index = int(len(sorted_data) * percentile / 100)
        return sorted_data[min(index, len(sorted_data) - 1)]


def load_test_endpoint(client, endpoint: str, method: str = 'GET', 
                      data: Dict = None, headers: Dict = None, 
                      concurrent_users: int = 10, requests_per_user: int = 10) -> PerformanceMetrics:
    """
    Perform load testing on an endpoint
    """
    metrics = PerformanceMetrics()
    metrics.start_time = time.time()
    
    def make_request():
        """Make a single request and measure performance"""
        start = time.time()
        error = None
        
        try:
            if method.upper() == 'GET':
                response = client.get(endpoint, headers=headers)
            elif method.upper() == 'POST':
                response = client.post(endpoint, json=data, headers=headers)
            elif method.upper() == 'PUT':
                response = client.put(endpoint, json=data, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            response_time = (time.time() - start) * 1000  # Convert to milliseconds
            status_code = response.status_code
            
        except Exception as e:
            response_time = (time.time() - start) * 1000
            status_code = 500
            error = str(e)
        
        return response_time, status_code, error
    
    def user_session():
        """Simulate a user making multiple requests"""
        for _ in range(requests_per_user):
            response_time, status_code, error = make_request()
            metrics.add_response(response_time, status_code, error)
            # Small delay between requests to simulate real user behavior
            time.sleep(0.1)
    
    # Run concurrent users
    with ThreadPoolExecutor(max_workers=concurrent_users) as executor:
        futures = [executor.submit(user_session) for _ in range(concurrent_users)]
        
        # Wait for all futures to complete
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                metrics.errors.append(str(e))
    
    metrics.end_time = time.time()
    return metrics


@pytest.mark.performance
@pytest.mark.slow
class TestAPIPerformance:
    """API performance tests"""
    
    def test_products_list_performance(self, performance_client, performance_test_data):
        """Test products list endpoint performance"""
        metrics = load_test_endpoint(
            performance_client,
            '/api/v1/products',
            concurrent_users=performance_test_data['concurrent_users'] // 2,
            requests_per_user=5
        )
        
        stats = metrics.get_statistics()
        
        # Assertions
        assert stats['error_rate'] < 5, f"Error rate too high: {stats['error_rate']}%"
        assert stats['response_times']['p95'] < performance_test_data['max_response_time'] * 1000, \
            f"P95 response time too high: {stats['response_times']['p95']}ms"
        assert stats['throughput'] > 50, f"Throughput too low: {stats['throughput']} req/s"
        
        print(f"📊 Products list performance: {stats['throughput']:.1f} req/s, "
              f"P95: {stats['response_times']['p95']:.1f}ms")
    
    def test_product_detail_performance(self, performance_client, performance_test_data):
        """Test product detail endpoint performance"""
        metrics = load_test_endpoint(
            performance_client,
            '/api/v1/products/1',
            concurrent_users=performance_test_data['concurrent_users'] // 4,
            requests_per_user=10
        )
        
        stats = metrics.get_statistics()
        
        # Assertions
        assert stats['error_rate'] < 5, f"Error rate too high: {stats['error_rate']}%"
        assert stats['response_times']['p95'] < performance_test_data['max_response_time'] * 1000, \
            f"P95 response time too high: {stats['response_times']['p95']}ms"
        
        print(f"📊 Product detail performance: {stats['throughput']:.1f} req/s, "
              f"P95: {stats['response_times']['p95']:.1f}ms")
    
    @patch('business_app.services.auth_service.AuthService.authenticate_user')
    def test_auth_login_performance(self, mock_auth, performance_client, performance_test_data):
        """Test authentication endpoint performance"""
        # Mock successful authentication
        mock_auth.return_value = {
            'success': True,
            'user_id': 1,
            'role': 'customer'
        }
        
        login_data = {
            'email': 'perf@test.com',
            'password': 'testpassword'
        }
        
        metrics = load_test_endpoint(
            performance_client,
            '/api/v1/auth/login',
            method='POST',
            data=login_data,
            concurrent_users=10,
            requests_per_user=5
        )
        
        stats = metrics.get_statistics()
        
        # Authentication should be fast and reliable
        assert stats['error_rate'] < 2, f"Auth error rate too high: {stats['error_rate']}%"
        assert stats['response_times']['p95'] < 1000, \
            f"Auth P95 response time too high: {stats['response_times']['p95']}ms"
        
        print(f"📊 Auth performance: {stats['throughput']:.1f} req/s, "
              f"P95: {stats['response_times']['p95']:.1f}ms")
    
    def test_health_check_performance(self, performance_client):
        """Test health check endpoint performance"""
        metrics = load_test_endpoint(
            performance_client,
            '/health',
            concurrent_users=20,
            requests_per_user=20
        )
        
        stats = metrics.get_statistics()
        
        # Health check should be very fast and reliable
        assert stats['error_rate'] == 0, f"Health check error rate: {stats['error_rate']}%"
        assert stats['response_times']['p95'] < 100, \
            f"Health check P95 too high: {stats['response_times']['p95']}ms"
        assert stats['throughput'] > 200, f"Health check throughput too low: {stats['throughput']} req/s"
        
        print(f"📊 Health check performance: {stats['throughput']:.1f} req/s, "
              f"P95: {stats['response_times']['p95']:.1f}ms")


@pytest.mark.performance
@pytest.mark.slow
class TestDatabasePerformance:
    """Database performance tests"""
    
    def test_query_performance(self, performance_app):
        """Test database query performance"""
        with performance_app.app_context():
            from business_app.models.product import Product
            from business_app import db
            
            # Test simple query performance
            start_time = time.time()
            for _ in range(100):
                products = Product.query.limit(10).all()
                assert len(products) <= 10
            query_time = time.time() - start_time
            
            # Should complete 100 queries in under 1 second
            assert query_time < 1.0, f"Query performance too slow: {query_time:.2f}s for 100 queries"
            
            print(f"📊 Database query performance: {100/query_time:.1f} queries/s")
    
    def test_bulk_insert_performance(self, performance_app):
        """Test bulk insert performance"""
        with performance_app.app_context():
            from business_app.models.product import Product
            from business_app import db
            
            # Test bulk insert
            start_time = time.time()
            
            products = []
            for i in range(100):
                product = Product(
                    name=f'Bulk Product {i}',
                    description=f'Bulk test product {i}',
                    category='water',
                    size='medium',
                    volume=Decimal('10.00'),
                    volume_unit='L',
                    base_price=Decimal('12000.00'),
                    stock_quantity=50,
                    is_active=True
                )
                products.append(product)
            
            db.session.bulk_save_objects(products)
            db.session.commit()
            
            insert_time = time.time() - start_time
            
            # Should insert 100 records in under 2 seconds
            assert insert_time < 2.0, f"Bulk insert too slow: {insert_time:.2f}s for 100 records"
            
            print(f"📊 Bulk insert performance: {100/insert_time:.1f} records/s")


@pytest.mark.performance
class TestMemoryUsage:
    """Memory usage and resource tests"""
    
    def test_memory_leak_detection(self, performance_client):
        """Test for memory leaks in repeated requests"""
        import psutil
        import os
        
        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss
        
        # Make many requests
        for _ in range(200):
            response = performance_client.get('/api/v1/products')
            assert response.status_code in [200, 404]  # 404 is OK if no products
        
        final_memory = process.memory_info().rss
        memory_increase = final_memory - initial_memory
        
        # Memory increase should be reasonable (less than 50MB)
        max_memory_increase = 50 * 1024 * 1024  # 50MB
        assert memory_increase < max_memory_increase, \
            f"Potential memory leak: {memory_increase / 1024 / 1024:.1f}MB increase"
        
        print(f"📊 Memory usage: {memory_increase / 1024 / 1024:.1f}MB increase over 200 requests")


def save_performance_baseline(results: Dict[str, Any], baseline_file: str = 'tests/performance/baseline.json'):
    """Save performance results as baseline for future comparisons"""
    import json
    from pathlib import Path
    
    baseline_path = Path(baseline_file)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    
    baseline_data = {
        'timestamp': time.time(),
        'results': results
    }
    
    with open(baseline_path, 'w') as f:
        json.dump(baseline_data, f, indent=2)


def compare_with_baseline(current_results: Dict[str, Any], baseline_file: str = 'tests/performance/baseline.json'):
    """Compare current results with baseline and detect regressions"""
    import json
    from pathlib import Path
    
    baseline_path = Path(baseline_file)
    if not baseline_path.exists():
        print("No performance baseline found, skipping comparison")
        return
    
    try:
        with open(baseline_path, 'r') as f:
            baseline_data = json.load(f)
        
        baseline_results = baseline_data['results']
        
        # Compare key metrics and detect regressions
        regressions = []
        
        for test_name, current in current_results.items():
            if test_name in baseline_results:
                baseline = baseline_results[test_name]
                
                # Check for response time regression (>20% increase)
                if current.get('p95_response_time', 0) > baseline.get('p95_response_time', 0) * 1.2:
                    regressions.append(f"{test_name}: P95 response time regression")
                
                # Check for throughput regression (>20% decrease)
                if current.get('throughput', 0) < baseline.get('throughput', 0) * 0.8:
                    regressions.append(f"{test_name}: Throughput regression")
        
        if regressions:
            print("⚠️  Performance regressions detected:")
            for regression in regressions:
                print(f"  - {regression}")
        else:
            print("✅ No performance regressions detected")
            
    except Exception as e:
        print(f"Warning: Could not compare with baseline: {e}")