#!/usr/bin/env python3
"""
Automated Test Pipeline Runner
Orchestrates the execution of different test suites with proper reporting and CI/CD integration
"""
import os
import sys
import subprocess
import json
import time
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from datetime import datetime, UTC
import xml.etree.ElementTree as ET

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


@dataclass
class TestResult:
    """Test result data structure"""
    suite_name: str
    passed: int
    failed: int
    skipped: int
    errors: int
    duration: float
    coverage_percentage: Optional[float] = None
    critical_failures: List[str] = None

    def __post_init__(self):
        if self.critical_failures is None:
            self.critical_failures = []


class TestPipeline:
    """Automated test pipeline manager"""

    def __init__(self, config_file: Optional[str] = None):
        self.project_root = project_root
        self.reports_dir = self.project_root / "test_reports"
        self.reports_dir.mkdir(exist_ok=True)

        # Load configuration
        self.config = self._load_config(config_file)

        # Test suites configuration
        self.test_suites = {
            'unit': {
                'path': 'tests/unit',
                'markers': ['unit'],
                'critical': True,
                'timeout': 300,  # 5 minutes
                'required_coverage': 80
            },
            'integration': {
                'path': 'tests/integration',
                'markers': ['integration'],
                'critical': True,
                'timeout': 600,  # 10 minutes
                'required_coverage': 70
            },
            'security': {
                'path': 'tests/security',
                'markers': ['security'],
                'critical': True,
                'timeout': 900,  # 15 minutes
                'required_coverage': 90
            },
            'api': {
                'path': 'tests/integration',
                'markers': ['api'],
                'critical': True,
                'timeout': 600,
                'required_coverage': 85
            },
            'performance': {
                'path': 'tests/performance',
                'markers': ['performance', 'slow'],
                'critical': False,
                'timeout': 1800,  # 30 minutes
                'required_coverage': 60
            }
        }

        # CI/CD environment detection
        self.is_ci = any([
            os.getenv('CI'),
            os.getenv('GITHUB_ACTIONS'),
            os.getenv('GITLAB_CI'),
            os.getenv('JENKINS_URL'),
            os.getenv('TRAVIS'),
            os.getenv('CIRCLECI')
        ])

    def _load_config(self, config_file: Optional[str]) -> Dict[str, Any]:
        """Load test pipeline configuration"""
        default_config = {
            'parallel_execution': True,
            'max_workers': 4,
            'fail_fast': False,
            'generate_html_report': True,
            'send_notifications': False,
            'strict_coverage': True,
            'security_scan_enabled': True,
            'performance_baseline_file': 'tests/performance/baseline.json'
        }

        if config_file and Path(config_file).exists():
            try:
                with open(config_file, 'r') as f:
                    user_config = json.load(f)
                default_config.update(user_config)
            except Exception as e:
                print(f"Warning: Could not load config file {config_file}: {e}")

        return default_config

    def setup_test_environment(self):
        """Setup test environment"""
        print("🔧 Setting up test environment...")

        # Set test environment variables
        test_env = {
            'FLASK_ENV': 'testing',
            'SECRET_KEY': 'test-secret-key-for-testing-32-chars-long',
            'JWT_SECRET_KEY': 'test-jwt-secret-key-for-testing',
            'DB_PASSWORD': 'test_password',
            'TESTING': 'true',
            'REDIS_URL': 'redis://localhost:6379/15',
            'CELERY_ALWAYS_EAGER': 'true'
        }

        for key, value in test_env.items():
            os.environ.setdefault(key, value)

        # Create test directories
        for suite_name, config in self.test_suites.items():
            test_path = self.project_root / config['path']
            test_path.mkdir(parents=True, exist_ok=True)

        # Ensure test reports directory exists
        self.reports_dir.mkdir(exist_ok=True)

        # Pre-flight translation guardrail (missing keys + hardcoded user text checks)
        self._run_translation_guardrail()

        print("✅ Test environment setup complete")

    def _run_translation_guardrail(self):
        """Run translation coverage validation before tests."""
        print("🌐 Running translation coverage guardrail...")
        cmd = ['python', 'scripts/validate_translation_coverage.py', '--check', 'all']

        result = subprocess.run(
            cmd,
            cwd=self.project_root,
            capture_output=True,
            text=True
        )

        output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
        output = output.strip()

        # Keep a persistent log for CI artifacts/debugging
        log_path = self.reports_dir / 'translation_guardrail.log'
        with open(log_path, 'w', encoding='utf-8') as log_file:
            log_file.write(output + ("\n" if output else ""))

        if result.returncode != 0:
            print("❌ Translation coverage guardrail failed")
            if output:
                print(output)
            raise RuntimeError("Translation coverage guardrail failed")

        print("✅ Translation coverage guardrail passed")

    def run_test_suite(self, suite_name: str) -> TestResult:
        """Run a specific test suite"""
        if suite_name not in self.test_suites:
            raise ValueError(f"Unknown test suite: {suite_name}")

        suite_config = self.test_suites[suite_name]
        test_path = self.project_root / suite_config['path']

        if not test_path.exists():
            print(f"⚠️  Test path {test_path} does not exist, skipping {suite_name}")
            return TestResult(
                suite_name=suite_name,
                passed=0, failed=0, skipped=0, errors=0,
                duration=0.0
            )

        print(f"🧪 Running {suite_name} tests...")
        start_time = time.time()

        # Build pytest command
        cmd = [
            'python', '-m', 'pytest',
            str(test_path),
            '--verbose',
            '--tb=short',
            '--junitxml=' + str(self.reports_dir / f'{suite_name}_results.xml'),
            '--cov=business_app',
            '--cov-report=xml:' + str(self.reports_dir / f'{suite_name}_coverage.xml'),
            '--cov-report=html:' + str(self.reports_dir / f'{suite_name}_htmlcov'),
            '--cov-report=term-missing',
            f'--timeout={suite_config["timeout"]}'
        ]

        # Add markers if specified
        if suite_config.get('markers'):
            markers_expr = ' or '.join(suite_config['markers'])
            cmd.extend(['-m', markers_expr])

        # Add fail-fast if configured
        if self.config.get('fail_fast') and suite_config.get('critical'):
            cmd.append('-x')

        # Parallel execution is governed by pytest.ini's `-n auto --dist=loadfile`
        # (TST-006). Per-worker Redis isolation lives in tests/conftest.py.

        try:
            # Run the tests
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=suite_config['timeout']
            )

            duration = time.time() - start_time

            # Parse results
            test_result = self._parse_test_results(suite_name, result, duration)

            # Check coverage requirements
            if suite_config.get('required_coverage'):
                coverage_ok = self._check_coverage_requirements(
                    suite_name,
                    suite_config['required_coverage']
                )
                if not coverage_ok and self.config.get('strict_coverage'):
                    test_result.errors += 1
                    test_result.critical_failures.append(
                        f"Coverage below required {suite_config['required_coverage']}%"
                    )

            return test_result

        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            print(f"❌ {suite_name} tests timed out after {duration:.1f}s")
            return TestResult(
                suite_name=suite_name,
                passed=0, failed=0, skipped=0, errors=1,
                duration=duration,
                critical_failures=[f"Tests timed out after {suite_config['timeout']}s"]
            )
        except Exception as e:
            duration = time.time() - start_time
            print(f"❌ Error running {suite_name} tests: {e}")
            return TestResult(
                suite_name=suite_name,
                passed=0, failed=0, skipped=0, errors=1,
                duration=duration,
                critical_failures=[f"Test execution error: {str(e)}"]
            )

    def _parse_test_results(self, suite_name: str, result: subprocess.CompletedProcess, duration: float) -> TestResult:
        """Parse test results from pytest output"""
        # Try to parse JUnit XML if available
        junit_file = self.reports_dir / f'{suite_name}_results.xml'

        if junit_file.exists():
            try:
                tree = ET.parse(junit_file)
                root = tree.getroot()
                testsuite = root if root.tag == 'testsuite' else root.find('testsuite')

                if testsuite is not None:
                    passed = int(testsuite.get('tests', 0)) - int(testsuite.get('failures', 0)) - int(testsuite.get('errors', 0)) - int(testsuite.get('skipped', 0))
                    failed = int(testsuite.get('failures', 0))
                    errors = int(testsuite.get('errors', 0))
                    skipped = int(testsuite.get('skipped', 0))

                    # Parse critical failures
                    critical_failures = []
                    for testcase in testsuite.findall('testcase'):
                        failure = testcase.find('failure')
                        error = testcase.find('error')
                        if failure is not None or error is not None:
                            test_name = testcase.get('name', 'unknown')
                            if 'critical' in testcase.get('classname', '').lower():
                                critical_failures.append(test_name)

                    return TestResult(
                        suite_name=suite_name,
                        passed=passed,
                        failed=failed,
                        skipped=skipped,
                        errors=errors,
                        duration=duration,
                        critical_failures=critical_failures
                    )
            except Exception as e:
                print(f"Warning: Could not parse JUnit XML for {suite_name}: {e}")

        # Fallback to parsing stdout
        output = result.stdout + result.stderr

        # Basic regex parsing for pytest output
        import re

        # Look for summary line like "= 5 passed, 2 failed, 1 skipped in 10.5s ="
        summary_match = re.search(
            r'=+ (.+) in [\d.]+s =+',
            output
        )

        passed, failed, skipped, errors = 0, 0, 0, 0

        if summary_match:
            summary = summary_match.group(1)

            passed_match = re.search(r'(\d+) passed', summary)
            if passed_match:
                passed = int(passed_match.group(1))

            failed_match = re.search(r'(\d+) failed', summary)
            if failed_match:
                failed = int(failed_match.group(1))

            skipped_match = re.search(r'(\d+) skipped', summary)
            if skipped_match:
                skipped = int(skipped_match.group(1))

            error_match = re.search(r'(\d+) error', summary)
            if error_match:
                errors = int(error_match.group(1))

        # If no tests found, consider it an error
        if passed + failed + skipped + errors == 0:
            errors = 1

        return TestResult(
            suite_name=suite_name,
            passed=passed,
            failed=failed,
            skipped=skipped,
            errors=errors,
            duration=duration
        )

    def _check_coverage_requirements(self, suite_name: str, required_coverage: float) -> bool:
        """Check if coverage meets requirements"""
        coverage_file = self.reports_dir / f'{suite_name}_coverage.xml'

        if not coverage_file.exists():
            print(f"⚠️  No coverage report found for {suite_name}")
            return False

        try:
            tree = ET.parse(coverage_file)
            root = tree.getroot()
            coverage_elem = root.find('coverage')

            if coverage_elem is not None:
                line_rate = float(coverage_elem.get('line-rate', 0))
                coverage_percentage = line_rate * 100

                print(f"📊 {suite_name} coverage: {coverage_percentage:.1f}%")
                return coverage_percentage >= required_coverage

        except Exception as e:
            print(f"Warning: Could not parse coverage for {suite_name}: {e}")

        return False

    def run_security_scan(self) -> TestResult:
        """Run security-specific scans"""
        print("🔒 Running security scans...")
        start_time = time.time()

        # Run bandit security scan
        bandit_cmd = [
            'python', '-m', 'bandit',
            '-r', 'business_app/',
            '-f', 'json',
            '-o', str(self.reports_dir / 'bandit_report.json')
        ]

        security_issues = []

        try:
            result = subprocess.run(bandit_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                security_issues.append("Bandit security scan found issues")
        except Exception as e:
            security_issues.append(f"Bandit scan failed: {e}")

        # Run safety check for dependencies
        safety_cmd = ['python', '-m', 'safety', 'check', '--json']

        try:
            result = subprocess.run(safety_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                security_issues.append("Safety check found vulnerable dependencies")
        except Exception as e:
            print(f"Warning: Safety check failed: {e}")

        duration = time.time() - start_time

        return TestResult(
            suite_name='security_scan',
            passed=1 if not security_issues else 0,
            failed=len(security_issues),
            skipped=0,
            errors=0,
            duration=duration,
            critical_failures=security_issues
        )

    def run_all_tests(self) -> Dict[str, TestResult]:
        """Run all test suites"""
        print("🚀 Starting automated test pipeline...")
        pipeline_start = time.time()

        # Setup environment
        self.setup_test_environment()

        results = {}

        # Run test suites
        for suite_name in self.test_suites.keys():
            results[suite_name] = self.run_test_suite(suite_name)

        # Run security scan if enabled
        if self.config.get('security_scan_enabled'):
            results['security_scan'] = self.run_security_scan()

        # Generate reports
        self.generate_pipeline_report(results, time.time() - pipeline_start)

        return results

    def generate_pipeline_report(self, results: Dict[str, TestResult], total_duration: float):
        """Generate comprehensive test pipeline report"""
        print("\n📊 Generating test pipeline report...")

        # Console summary
        print("\n" + "="*80)
        print("🧪 TEST PIPELINE RESULTS")
        print("="*80)

        total_passed = sum(r.passed for r in results.values())
        total_failed = sum(r.failed for r in results.values())
        total_errors = sum(r.errors for r in results.values())
        total_skipped = sum(r.skipped for r in results.values())
        total_tests = total_passed + total_failed + total_errors + total_skipped

        print(f"📈 Total Tests: {total_tests}")
        print(f"✅ Passed: {total_passed}")
        print(f"❌ Failed: {total_failed}")
        print(f"⚠️  Errors: {total_errors}")
        print(f"⏭️  Skipped: {total_skipped}")
        print(f"⏱️  Duration: {total_duration:.1f}s")

        # Suite breakdown
        print(f"\n📋 Suite Breakdown:")
        for suite_name, result in results.items():
            status = "✅" if result.failed == 0 and result.errors == 0 else "❌"
            critical_mark = "🔴" if result.critical_failures else ""

            print(f"  {status} {critical_mark} {suite_name:15} | "
                  f"P:{result.passed:3} F:{result.failed:3} E:{result.errors:3} "
                  f"({result.duration:.1f}s)")

            if result.critical_failures:
                for failure in result.critical_failures:
                    print(f"    🚨 {failure}")

        # Generate JSON report
        json_report = {
            'timestamp': datetime.now(UTC).isoformat(),
            'total_duration': total_duration,
            'summary': {
                'total_tests': total_tests,
                'passed': total_passed,
                'failed': total_failed,
                'errors': total_errors,
                'skipped': total_skipped,
                'success_rate': (total_passed / total_tests * 100) if total_tests > 0 else 0
            },
            'suites': {
                name: {
                    'passed': result.passed,
                    'failed': result.failed,
                    'errors': result.errors,
                    'skipped': result.skipped,
                    'duration': result.duration,
                    'coverage_percentage': result.coverage_percentage,
                    'critical_failures': result.critical_failures
                }
                for name, result in results.items()
            },
            'environment': {
                'ci': self.is_ci,
                'python_version': sys.version,
                'platform': sys.platform
            }
        }

        with open(self.reports_dir / 'pipeline_report.json', 'w') as f:
            json.dump(json_report, f, indent=2)

        # Determine overall success
        critical_suites = [name for name, config in self.test_suites.items() if config.get('critical')]
        critical_failures = any(
            results[suite].failed > 0 or results[suite].errors > 0 or results[suite].critical_failures
            for suite in critical_suites
            if suite in results
        )

        if critical_failures:
            print("\n❌ PIPELINE FAILED - Critical test failures detected")
            sys.exit(1)
        else:
            print("\n✅ PIPELINE PASSED - All critical tests successful")

    def run_suite_only(self, suite_name: str):
        """Run only a specific test suite"""
        if suite_name not in self.test_suites and suite_name != 'security_scan':
            print(f"❌ Unknown test suite: {suite_name}")
            print(f"Available suites: {list(self.test_suites.keys()) + ['security_scan']}")
            sys.exit(1)

        self.setup_test_environment()

        if suite_name == 'security_scan':
            result = self.run_security_scan()
        else:
            result = self.run_test_suite(suite_name)

        # Generate mini report
        results = {suite_name: result}
        self.generate_pipeline_report(results, result.duration)


def main():
    """Main entry point for test pipeline"""
    import argparse

    available_suites = ['unit', 'integration', 'security', 'api', 'performance', 'security_scan', 'all']
    parser = argparse.ArgumentParser(description="BlueStream Automated Test Pipeline")
    parser.add_argument('--suite', choices=available_suites,
                       default='all', help='Test suite to run')
    parser.add_argument('--config', help='Path to configuration file')
    parser.add_argument('--fail-fast', action='store_true', help='Stop on first failure')
    parser.add_argument('--no-coverage', action='store_true', help='Skip coverage requirements')

    args = parser.parse_args()

    # Override config based on CLI args
    config_overrides = {}
    if args.fail_fast:
        config_overrides['fail_fast'] = True
    if args.no_coverage:
        config_overrides['strict_coverage'] = False

    # Create pipeline
    pipeline = TestPipeline(args.config)
    pipeline.config.update(config_overrides)

    try:
        if args.suite == 'all':
            pipeline.run_all_tests()
        else:
            pipeline.run_suite_only(args.suite)
    except KeyboardInterrupt:
        print("\n⏹️  Test pipeline interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Test pipeline failed with error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
