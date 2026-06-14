"""Shared pytest fixtures for the k8si test suite."""

import pytest


@pytest.fixture(autouse=True)
def clear_running_set():
    """Clear the _running set before and after each test to prevent cross-test contamination."""
    import k8si.operator.main as main_module

    main_module._running.clear()
    yield
    main_module._running.clear()
