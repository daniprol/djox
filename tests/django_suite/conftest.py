"""Markers for tests ported from Django's own test suite (see README.md)."""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """A test marked `django_unsupported` is expected to fail, strictly: if it
    passes, pytest reports it, so the marker gets removed."""
    for item in items:
        unsupported = item.get_closest_marker("django_unsupported")
        if unsupported is not None:
            reason = unsupported.args[0] if unsupported.args else "not supported by djox yet"
            item.add_marker(pytest.mark.xfail(reason=reason, strict=True))
