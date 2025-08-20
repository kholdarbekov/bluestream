#!/usr/bin/env python3
"""
Simple test runner script for BlueStream project
Provides an easy way to run tests locally and in CI/CD
"""
import sys
import os
import argparse
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from tests.pipeline.test_runner import TestPipeline


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="BlueStream Test Runner",
        epilog="""
Examples:
  python run_tests.py                    # Run all tests
  python run_tests.py --suite unit       # Run only unit tests
  python run_tests.py --suite security   # Run only security tests
  python run_tests.py --fast             # Run with fail-fast enabled
  python run_tests.py --no-coverage      # Skip coverage requirements
  python run_tests.py --suite performance --no-coverage  # Performance tests without coverage
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # Test suite selection
    parser.add_argument(
        '--suite', 
        choices=['all', 'unit', 'integration', 'security', 'api', 'performance', 'security_scan'],
        default='all',
        help='Test suite to run (default: all)'
    )
    
    # Configuration options
    parser.add_argument(
        '--config',
        help='Path to test configuration file (default: test_config.json)'
    )
    
    parser.add_argument(
        '--fast', '--fail-fast',
        action='store_true',
        help='Stop on first test failure'
    )
    
    parser.add_argument(
        '--no-coverage',
        action='store_true',
        help='Skip coverage requirements'
    )
    
    parser.add_argument(
        '--parallel',
        action='store_true',
        help='Force parallel test execution'
    )
    
    parser.add_argument(
        '--no-parallel',
        action='store_true',
        help='Disable parallel test execution'
    )
    
    parser.add_argument(
        '--workers',
        type=int,
        help='Number of parallel workers (default: 4)'
    )
    
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Verbose output'
    )
    
    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Quiet output (minimal)'
    )
    
    # Environment setup
    parser.add_argument(
        '--env',
        choices=['development', 'ci', 'staging'],
        default='development',
        help='Test environment (default: development)'
    )
    
    # Security and performance options
    parser.add_argument(
        '--security-only',
        action='store_true',
        help='Run only security-related tests (unit + integration + security)'
    )
    
    parser.add_argument(
        '--critical-only',
        action='store_true',
        help='Run only critical tests (marked with @pytest.mark.critical)'
    )
    
    parser.add_argument(
        '--skip-security-scan',
        action='store_true',
        help='Skip external security scanning tools'
    )
    
    # Reporting options
    parser.add_argument(
        '--html-report',
        action='store_true',
        help='Generate HTML coverage report'
    )
    
    parser.add_argument(
        '--junit-xml',
        help='Path to save JUnit XML report'
    )
    
    args = parser.parse_args()
    
    # Validate arguments
    if args.parallel and args.no_parallel:
        parser.error("Cannot specify both --parallel and --no-parallel")
    
    if args.verbose and args.quiet:
        parser.error("Cannot specify both --verbose and --quiet")
    
    # Setup environment variables
    setup_test_environment(args.env)
    
    # Create test pipeline with configuration overrides
    config_overrides = {}
    
    if args.fast:
        config_overrides['fail_fast'] = True
    
    if args.no_coverage:
        config_overrides['strict_coverage'] = False
    
    if args.parallel:
        config_overrides['parallel_execution'] = True
    elif args.no_parallel:
        config_overrides['parallel_execution'] = False
    
    if args.workers:
        config_overrides['max_workers'] = args.workers
    
    if args.html_report:
        config_overrides['generate_html_report'] = True
    
    if args.skip_security_scan:
        config_overrides['security_scan_enabled'] = False
    
    # Load configuration
    config_file = args.config or 'test_config.json'
    pipeline = TestPipeline(config_file)
    pipeline.config.update(config_overrides)
    
    # Set verbosity
    if args.verbose:
        os.environ['PYTEST_VERBOSITY'] = '-vv'
    elif args.quiet:
        os.environ['PYTEST_VERBOSITY'] = '-q'
    
    # Determine which tests to run
    try:
        if args.security_only:
            print("🔒 Running security-focused test suite...")
            results = run_security_focused_tests(pipeline)
        elif args.critical_only:
            print("🚨 Running critical tests only...")
            results = run_critical_tests_only(pipeline)
        elif args.suite == 'all':
            print("🧪 Running complete test suite...")
            results = pipeline.run_all_tests()
        else:
            print(f"🎯 Running {args.suite} tests...")
            pipeline.run_suite_only(args.suite)
            return
        
        # Summary
        print_test_summary(results)
        
        # Exit with appropriate code
        critical_failures = any(
            result.failed > 0 or result.errors > 0 or result.critical_failures
            for result in results.values()
        )
        
        sys.exit(1 if critical_failures else 0)
        
    except KeyboardInterrupt:
        print("\n⏹️  Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Test execution failed: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


def setup_test_environment(env: str):
    """Setup environment variables for testing"""
    
    # Common test environment variables
    test_env = {
        'FLASK_ENV': 'testing',
        'TESTING': 'true',
        'SECRET_KEY': 'test-secret-key-for-testing-32-chars-long',
        'JWT_SECRET_KEY': 'test-jwt-secret-key-for-testing',
        'DB_PASSWORD': 'test_password',
        'CELERY_ALWAYS_EAGER': 'true',
        'WTF_CSRF_ENABLED': 'false'
    }
    
    # Environment-specific settings
    if env == 'development':
        test_env.update({
            'DATABASE_URL': 'sqlite:///test.db',
            'REDIS_URL': 'redis://localhost:6379/15'
        })
    elif env == 'ci':
        test_env.update({
            'DATABASE_URL': 'postgresql://test_user:test_password@localhost:5432/test_db',
            'REDIS_URL': 'redis://localhost:6379/15'
        })
    elif env == 'staging':
        test_env.update({
            'DATABASE_URL': os.getenv('STAGING_DATABASE_URL', 'postgresql://staging_user:staging_password@staging-db:5432/staging_db'),
            'REDIS_URL': os.getenv('STAGING_REDIS_URL', 'redis://staging-redis:6379/0')
        })
    
    # Set environment variables
    for key, value in test_env.items():
        os.environ.setdefault(key, value)
    
    print(f"🔧 Test environment configured for: {env}")


def run_security_focused_tests(pipeline):
    """Run security-focused test suites"""
    results = {}
    
    # Run security-related test suites
    security_suites = ['unit', 'integration', 'security']
    
    for suite in security_suites:
        print(f"🔒 Running {suite} tests (security focus)...")
        results[suite] = pipeline.run_test_suite(suite)
    
    # Run security scan
    if pipeline.config.get('security_scan_enabled'):
        results['security_scan'] = pipeline.run_security_scan()
    
    return results


def run_critical_tests_only(pipeline):
    """Run only tests marked as critical"""
    results = {}
    
    # Override pytest markers to run only critical tests
    original_suites = pipeline.test_suites.copy()
    
    for suite_name, suite_config in pipeline.test_suites.items():
        # Add critical marker to existing markers
        markers = suite_config.get('markers', [])
        if 'critical' not in markers:
            markers.append('critical')
        suite_config['markers'] = markers
    
    try:
        # Run test suites with critical marker
        for suite_name in ['unit', 'integration', 'security']:
            results[suite_name] = pipeline.run_test_suite(suite_name)
        
        return results
    finally:
        # Restore original suite configuration
        pipeline.test_suites = original_suites


def print_test_summary(results):
    """Print a summary of test results"""
    print("\n" + "="*60)
    print("📊 TEST EXECUTION SUMMARY")
    print("="*60)
    
    total_passed = sum(r.passed for r in results.values())
    total_failed = sum(r.failed for r in results.values()) 
    total_errors = sum(r.errors for r in results.values())
    total_skipped = sum(r.skipped for r in results.values())
    total_tests = total_passed + total_failed + total_errors + total_skipped
    
    print(f"Total Tests:    {total_tests}")
    print(f"✅ Passed:      {total_passed}")
    print(f"❌ Failed:      {total_failed}")
    print(f"💥 Errors:      {total_errors}")
    print(f"⏭️ Skipped:     {total_skipped}")
    
    if total_tests > 0:
        success_rate = (total_passed / total_tests) * 100
        print(f"📈 Success Rate: {success_rate:.1f}%")
    
    # Critical failures summary
    critical_failures = []
    for suite_name, result in results.items():
        if result.critical_failures:
            critical_failures.extend([f"{suite_name}: {failure}" for failure in result.critical_failures])
    
    if critical_failures:
        print(f"\n🚨 Critical Failures ({len(critical_failures)}):")
        for failure in critical_failures:
            print(f"  • {failure}")
    
    print("="*60)


if __name__ == '__main__':
    main()