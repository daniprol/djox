"""Django settings for the tests, and for mypy's Django plugin.

The plugin imports this module to learn the installed apps and their models,
so it must stay free of side effects: no files are created here.

The database uses djox's SQLite ENGINE, or its Postgres one with
`DJOX_TEST_DATABASE=postgres`. Tests that touch it get a test database (a
file, `test_djox-tests.sqlite3` in the temp folder, or `test_djox_tests` on
the Postgres server) from pytest-django.
"""

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

# Django's own tests (tests/django_suite/README.md) are apps named like in
# Django's test suite (`backends`, `transactions`), so their folder must be on
# the import path. pytest and mypy's Django plugin both import this module.
DJANGO_SUITE = str(Path(__file__).parent / "django_suite")
if DJANGO_SUITE not in sys.path:
    sys.path.append(DJANGO_SUITE)

INSTALLED_APPS = [
    "django.contrib.contenttypes",  # Django's own `backends` tests use generic relations
    "django.contrib.auth",  # a library's models, switched to djox by DJOX_MODELS
    "djox",
    "tests.e2e.library",
    "tests.e2e.shelf",
    "tests.e2e.kinds",
    # Django's own tests, ported (see tests/django_suite/README.md)
    "backends",
    "transactions",
    "db_functions",
    "async",
    "basic",
    "lookup",
    "ordering",
]
DATABASES: dict[str, dict[str, object]] = {
    "default": {
        "ENGINE": "djox.backends.sqlite3",
        "NAME": Path(tempfile.gettempdir()) / "djox-tests.sqlite3",
    }
}
# `DJOX_TEST_DATABASE=postgres uv run pytest` runs the tests on Postgres
# instead, on the server `DJOX_POSTGRES_URL` names (the test database is
# made and dropped there by Django, like any Django test run).
if os.environ.get("DJOX_TEST_DATABASE") == "postgres":
    postgres = urlsplit(os.environ.get("DJOX_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/postgres"))
    DATABASES["default"] = {
        "ENGINE": "djox.backends.postgresql",
        "NAME": "djox_tests",
        "USER": postgres.username or "",
        "PASSWORD": postgres.password or "",
        "HOST": postgres.hostname or "",
        "PORT": postgres.port or "",
    }
    # The ported `backends` tests have no migrations, and foreign keys to
    # `contenttypes`, which has. Postgres checks that a foreign key's table
    # exists, so, like Django's own test runner, the tables of
    # `contenttypes` and `auth` (whose migrations need `contenttypes`'s) are
    # made from their models, together with theirs.
    MIGRATION_MODULES = {"auth": None, "contenttypes": None}
# The models djox switches over (`djox.install`): Django's `auth` app,
# `shelf`, whose models are written as if djox didn't exist, and the models of
# Django's own `basic`, `lookup` and `ordering` tests, so that they test
# `djox.QuerySet` (and the fast lane's notes). Not "__all__", so the other
# ported tests keep testing what they were ported for.
DJOX_MODELS = ["auth", "shelf", "basic", "lookup", "ordering"]
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
