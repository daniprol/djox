"""Running Django's own tests, ported to djox (see README.md).

- A test marked `django_unsupported` is expected to fail, strictly: if it
  passes, pytest reports it, so the marker gets removed.
- Django's SQLite backend lists some of Django's tests that SQLite can't pass
  (`DatabaseFeatures.django_test_skips` and `django_test_expected_failures`).
  Django's test runner skips them; so do we.
- Django's deprecation warnings are errors, like in Django's own test runner
  (some tests check that a warning is raised).
- The tests were copied from one Django version (`PORTED_FROM`). Django's
  tests change with Django, so on other versions they are not collected; the
  rest of djox's tests still run there.
"""

import warnings
from collections.abc import Iterator

import django
import pytest
from django.db import connections
from django.utils.deprecation import RemovedAfterNextVersionWarning, RemovedInNextVersionWarning

# The Django version the tests here were copied from (the one in uv.lock).
PORTED_FROM = (6, 1)

# Read by pytest: files and folders it skips while collecting.
collect_ignore_glob = [] if django.VERSION[:2] == PORTED_FROM else ["*"]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark unsupported tests as expected failures, and skip the tests
    Django's SQLite backend says it can't pass."""
    features = connections["default"].features
    skips: dict[str, str] = {}
    for reason, test_ids in features.django_test_skips.items():
        for test_id in test_ids:
            skips[test_id] = reason
    expected_failures = set(features.django_test_expected_failures)

    for item in items:
        unsupported = item.get_closest_marker("django_unsupported")
        if unsupported is not None:
            reason = unsupported.args[0] if unsupported.args else "not supported by djox yet"
            item.add_marker(pytest.mark.xfail(reason=reason, strict=True))

        # A test's id in Django's suite, like "db_functions.math.test_round.RoundTests.test_float".
        test_class = getattr(item, "cls", None)
        if test_class is None:
            continue
        class_id = f"{test_class.__module__}.{test_class.__name__}"
        test_id = f"{class_id}.{item.name}"
        for listed_id in (test_id, class_id):
            if listed_id in skips:
                item.add_marker(pytest.mark.skip(reason=skips[listed_id]))
            if listed_id in expected_failures:
                item.add_marker(pytest.mark.xfail(reason="expected to fail on SQLite (Django's own list)", strict=True))


@pytest.fixture(autouse=True)
def django_deprecations_are_errors() -> Iterator[None]:
    """Turn Django's deprecation warnings into errors, like Django's test runner."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", RemovedInNextVersionWarning)
        warnings.simplefilter("error", RemovedAfterNextVersionWarning)
        yield
