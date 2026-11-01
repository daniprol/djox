"""Django settings for the tests, and for mypy's Django plugin.

The plugin imports this module to learn the installed apps and their models,
so it must stay free of side effects: no files are created here.

The database uses djox's ENGINE. Tests that touch it get a test database (a
file, `test_djox-tests.sqlite3` in the temp folder) from pytest-django.
"""

import sys
import tempfile
from pathlib import Path

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
    # Django's own tests, ported (see tests/django_suite/README.md)
    "backends",
    "transactions",
    "db_functions",
    "async",
    "basic",
    "lookup",
    "ordering",
]
DATABASES = {
    "default": {
        "ENGINE": "djox.backends.sqlite3",
        "NAME": Path(tempfile.gettempdir()) / "djox-tests.sqlite3",
    }
}
# The models djox switches over (`djox.install`): Django's `auth` app,
# `shelf`, whose models are written as if djox didn't exist, and the models of
# Django's own `basic`, `lookup` and `ordering` tests, so that they test
# `djox.QuerySet` (and the fast lane's notes). Not "__all__", so the other
# ported tests keep testing what they were ported for.
DJOX_MODELS = ["auth", "shelf", "basic", "lookup", "ordering"]
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
