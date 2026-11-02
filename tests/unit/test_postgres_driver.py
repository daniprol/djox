"""djox's Postgres `ENGINE` needs psycopg 3, and says how to install it."""

import importlib
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured


def test_the_engine_asks_for_psycopg_3_when_it_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # `None` in `sys.modules` makes `import psycopg` raise `ImportError`, as if it weren't installed.
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.delitem(sys.modules, "djox.backends.postgresql", raising=False)
    monkeypatch.delitem(sys.modules, "djox.backends.postgresql.base", raising=False)

    with pytest.raises(ImproperlyConfigured, match=r'needs psycopg 3.*djox\[postgres\]'):
        importlib.import_module("djox.backends.postgresql.base")
