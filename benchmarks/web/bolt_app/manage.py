#!/usr/bin/env python
"""Runs django-bolt's `runbolt` command for the benchmark (see `__init__.py`).

The `web` folder goes on the path so that `shop` and `bolt_app` can be imported.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the `web` folder, with `shop/`
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bolt_app.settings")

from django.core.management import execute_from_command_line

if __name__ == "__main__":
    execute_from_command_line(sys.argv)
