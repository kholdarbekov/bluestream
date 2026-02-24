"""Performance smoke tests for key API paths."""

import statistics
import time

import pytest

from business_app.models.product import Product


def _measure_endpoint(client, method, path, *, json_body=None, headers=None, count=20):
    times_ms = []
    statuses = []

    for _ in range(count):
        start = time.perf_counter()
        if method == 'GET':
            response = client.get(path, headers=headers)
        elif method == 'POST':
            response = client.post(path, json=json_body, headers=headers)
        else:
            raise ValueError(f'Unsupported method: {method}')
        elapsed_ms = (time.perf_counter() - start) * 1000

        times_ms.append(elapsed_ms)
        statuses.append(response.status_code)

    p95 = sorted(times_ms)[int(len(times_ms) * 0.95) - 1]
    return {
        'mean_ms': statistics.mean(times_ms),
        'p95_ms': p95,
        'statuses': statuses,
    }


@pytest.mark.performance
class TestAPIPerformance:
    def test_products_list_performance(self, client, sample_product):
        stats = _measure_endpoint(client, 'GET', '/api/v1/products/', count=25)

        assert all(code == 200 for code in stats['statuses'])
        assert stats['p95_ms'] < 1200

    def test_product_detail_performance(self, client, sample_product):
        stats = _measure_endpoint(client, 'GET', f'/api/v1/products/{sample_product.id}', count=25)

        assert all(code == 200 for code in stats['statuses'])
        assert stats['p95_ms'] < 1200

    def test_orders_list_performance_authenticated(self, client, auth_headers, sample_order):
        stats = _measure_endpoint(client, 'GET', '/api/v1/orders/', headers=auth_headers, count=20)

        assert all(code == 200 for code in stats['statuses'])
        assert stats['p95_ms'] < 1500

    def test_health_check_performance(self, client):
        stats = _measure_endpoint(client, 'GET', '/health', count=20)

        assert all(code == 200 for code in stats['statuses'])
        assert stats['p95_ms'] < 500


@pytest.mark.performance
class TestDatabasePerformance:
    def test_repeated_product_queries(self, db, sample_product):
        start = time.perf_counter()

        for _ in range(100):
            products = Product.query.limit(10).all()
            assert len(products) >= 1

        elapsed = time.perf_counter() - start
        assert elapsed < 2.0


@pytest.mark.performance
class TestMemoryUsage:
    def test_memory_usage_stays_reasonable_over_repeated_requests(self, client, sample_product):
        try:
            import os
            import psutil
        except Exception:
            pytest.skip('psutil is not available')

        process = psutil.Process(os.getpid())
        initial = process.memory_info().rss

        for _ in range(100):
            response = client.get('/api/v1/products/')
            assert response.status_code == 200

        final = process.memory_info().rss
        increase = final - initial

        # Keep this as a smoke threshold to detect obvious leaks.
        assert increase < 100 * 1024 * 1024
