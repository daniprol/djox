"""Django settings for the tests, and for mypy's Django plugin.

The plugin imports this module to learn the installed apps and their models,
so it must stay free of side effects: no files are created here.
"""

INSTALLED_APPS = ["tests.e2e.library"]
# Django only compiles SQL in these tests; its own connection never opens this database.
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
