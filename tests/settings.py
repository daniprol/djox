"""Django settings for the tests, and for mypy's Django plugin.

The plugin imports this module to learn the installed apps and their models,
so it must stay free of side effects: no files are created here.

The database uses djox's ENGINE. Tests that touch it get a test database (a
file, `test_djox-tests.sqlite3` in the temp folder) from pytest-django.
"""

import sys
import tempfile
from pathlib import Path

import django

# Django's own tests (tests/django_suite/README.md) are apps named like in
# Django's test suite (`backends`, `transactions`), so their folder must be on
# the import path. pytest and mypy's Django plugin both import this module.
DJANGO_SUITE = str(Path(__file__).parent / "django_suite")
if DJANGO_SUITE not in sys.path:
    sys.path.append(DJANGO_SUITE)

# The Django version the tests in `tests/django_suite` were copied from (the
# one in uv.lock). On other versions they are not collected (see its conftest.py).
DJANGO_TESTS_PORTED_FROM = (6, 1)

# Django's tests of the QuerySet language (filters, expressions, aggregates,
# writes, relations), ported and run on `djox.QuerySet` (see DJOX_MODELS).
DJANGO_QUERY_APPS = [
    "aggregation",
    "aggregation_regress",
    "annotations",
    "bulk_create",
    "custom_managers",
    "dates",
    "datetimes",
    "defer",
    "delete",
    "expressions",
    "expressions_case",
    "from_db_value",
    "get_earliest_or_latest",
    "get_or_create",
    "many_to_many",
    "many_to_one",
    "null_queries",
    "or_lookups",
    "queries",
    "reverse_lookup",
    "update",
    "update_only_fields",
]
if django.VERSION[:2] != DJANGO_TESTS_PORTED_FROM:
    # Their models use what that Django version has (like `models.DB_CASCADE`).
    DJANGO_QUERY_APPS = []

INSTALLED_APPS = [
    "django.contrib.contenttypes",  # Django's own `backends` tests use generic relations
    "django.contrib.auth",  # a library's models, switched to djox by DJOX_MODELS
    "django.contrib.sites",  # used by some of Django's query tests (`extra(tables=["django_site"])`)
    "djox",
    "tests.e2e.library",
    "tests.e2e.shelf",
    # Django's own tests, ported (see tests/django_suite/README.md)
    "backends",
    "transactions",
    "db_functions",
    "async",
    "basic",
    "lookup",
    "ordering",
    *DJANGO_QUERY_APPS,
]
DATABASES = {
    "default": {
        "ENGINE": "djox.backends.sqlite3",
        "NAME": Path(tempfile.gettempdir()) / "djox-tests.sqlite3",
    },
    # A second database, like in Django's own test settings: some of Django's
    # tests (`databases = {"default", "other"}`) check routing between two.
    "other": {
        "ENGINE": "djox.backends.sqlite3",
        "NAME": Path(tempfile.gettempdir()) / "djox-tests-other.sqlite3",
    },
}
# The models djox switches over (`djox.install`): Django's `auth` app,
# `shelf`, whose models are written as if djox didn't exist, and the models of
# Django's own `basic`, `lookup`, `ordering` and query tests, so that they test
# `djox.QuerySet` (and the fast lane's notes). Not "__all__", so the other
# ported tests keep testing what they were ported for.
DJOX_MODELS = ["auth", "shelf", "basic", "lookup", "ordering", *DJANGO_QUERY_APPS]
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
