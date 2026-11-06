"""Django settings for the django-bolt benchmark (`load_test.py`).

The same project on two stacks, chosen by `STACK`: stock Django (`django`) or
djox (`djox`). Only the database `ENGINE` and `"djox"` in `INSTALLED_APPS`
differ. The `shop` models come from the web benchmark (`../web/shop`).
"""

import os
import sys
from pathlib import Path

# The shop app of the web benchmark: the same authors and books.
sys.path.insert(0, str(Path(__file__).parent.parent / "web"))

STACK = os.environ.get("STACK", "django")  # "django" or "djox"
SQLITE_FILE = Path(os.environ.get("SQLITE_FILE", "/tmp/djox-bolt-benchmark.sqlite3"))

SECRET_KEY = "only-for-the-benchmark"
DEBUG = False
ALLOWED_HOSTS = ["*"]
USE_TZ = True
TIME_ZONE = "UTC"
INSTALLED_APPS = ["django_bolt", "shop", "bolt_shop", *(["djox"] if STACK == "djox" else [])]
DATABASES = {
    "default": {
        "ENGINE": "djox.backends.sqlite3" if STACK == "djox" else "django.db.backends.sqlite3",
        "NAME": SQLITE_FILE,
        "OPTIONS": {"init_command": "PRAGMA synchronous = NORMAL"},
    }
}

# django-bolt reads the URLconf to find where the project keeps its `api.py`.
ROOT_URLCONF = "urls"
