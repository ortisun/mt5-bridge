"""Unit/integration split by module prefix, as in the platform suite (AGENTS.md).

Every item of an ``it_*.py`` module is marked ``integration`` and deselected by the
default ``addopts``; a hand-written ``integration`` mark elsewhere fails collection.
"""

import pytest

INTEGRATION_PREFIX = "it_"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    misplaced: list[str] = []
    for item in items:
        if item.path.name.startswith(INTEGRATION_PREFIX):
            item.add_marker(pytest.mark.integration)
        elif item.get_closest_marker("integration") is not None:
            misplaced.append(item.nodeid)
    if misplaced:
        raise pytest.UsageError(
            f"Mark integration tests only by the {INTEGRATION_PREFIX}*.py module prefix: "
            + ", ".join(misplaced)
        )
