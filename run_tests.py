#!/usr/bin/env python
"""
TestFortge — Test Runner
Runs the full test suite: unit → integration → functional → E2E.

Usage:
    python run_tests.py              # run all tests
    python run_tests.py unit         # run only unit tests
    python run_tests.py integration  # run only integration tests
    python run_tests.py functional   # run only functional tests
    python run_tests.py e2e          # run only E2E tests
"""

import subprocess
import sys
import time

# Map short names to test file paths
TEST_FILES = {
    "unit": "tests/test_unit.py",
    "integration": "tests/test_integration.py",
    "functional": "tests/test_functional.py",
    "e2e": "tests/test_e2e.py",
}


def run_tests(scope: str | None = None) -> int:
    """Run pytest with the given scope. Returns exit code."""
    cmd = [sys.executable, "-m", "pytest", "-v", "--tb=short"]

    if scope and scope in TEST_FILES:
        cmd.append(TEST_FILES[scope])
        label = f"{scope.upper()} tests"
    else:
        cmd.append("tests/")
        label = "ALL tests"

    print(f"\n{'=' * 60}")
    print(f"  TestFortge — Running {label}")
    print(f"{'=' * 60}\n")

    start = time.time()
    result = subprocess.run(cmd)
    elapsed = time.time() - start

    print(f"\n{'=' * 60}")
    status = "PASSED" if result.returncode == 0 else "FAILED"
    print(f"  {label}: {status}  ({elapsed:.1f}s)")
    print(f"{'=' * 60}\n")

    return result.returncode


if __name__ == "__main__":
    scope = sys.argv[1].lower() if len(sys.argv) > 1 else None

    if scope and scope not in TEST_FILES:
        print(f"Unknown scope: {scope}")
        print(f"Available: {', '.join(TEST_FILES.keys())}")
        sys.exit(1)

    sys.exit(run_tests(scope))
