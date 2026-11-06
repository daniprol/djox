"""Django settings for the django-bolt benchmark: the same database as the web app.

`STACK=django` is stock Django; `STACK=djox` adds `"djox"` to `INSTALLED_APPS`, which
switches the `shop` models to djox, as in `web/app.py`. Only SQLite here.
"""

import os
from pathlib import Path

STACK = os.environ.get("STACK", "django")  # "django" or "djox"
SQLITE_FILE = Path(os.environ.get("SQLITE_FILE", "/tmp/djox-web-benchmark.sqlite3"))
ENGINE_MODULE = "djox.backends" if STACK == "djox" else "django.db.backends"

DEBUG = False
SECRET_KEY = "only-for-the-benchmark"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
MIDDLEWARE: list[str] = []  # no middleware: the handlers alone are measured
INSTALLED_APPS = [
    "django_bolt",
    "shop",
    "bolt_app",
    *(["djox"] if STACK == "djox" else []),
]
DATABASES = {
    "default": {
        "ENGINE": f"{ENGINE_MODULE}.sqlite3",
        "NAME": SQLITE_FILE,
        # Same SQLite settings as the web app: WAL (stored in the file) and synchronous=NORMAL.
        "OPTIONS": {"init_command": "PRAGMA synchronous = NORMAL"},
        # Each request uses the connection of its own thread; keep none open between requests.
        "CONN_MAX_AGE": 0,
    }
}
ROOT_URLCONF = "bolt_app.urls"
USE_TZ = True
TIME_ZONE = "UTC"
