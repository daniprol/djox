"""How djox's Postgres `ENGINE` reads `OPTIONS` for its async database."""

from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from djox.backends.postgresql.options import connection_words, tls_settings

SETTINGS = {"NAME": "app", "USER": "django", "PASSWORD": "", "HOST": "localhost", "PORT": ""}


def test_tls_settings_go_to_the_async_database_as_they_are() -> None:
    options = {"sslmode": "verify-full", "sslrootcert": Path("/etc/ssl/rds.pem"), "connect_timeout": 5}
    assert tls_settings(options) == {
        "sslmode": "verify-full",
        "sslrootcert": "/etc/ssl/rds.pem",
        "sslcert": None,
        "sslkey": None,
    }


def test_tls_settings_stay_out_of_the_connection_words() -> None:
    options = {"sslmode": "verify-full", "sslrootcert": "/etc/ssl/rds.pem", "connect_timeout": 5}
    words = connection_words("default", {**SETTINGS, "OPTIONS": options})
    assert words == "dbname='app' user='django' host='localhost' connect_timeout='5'"


def test_an_encrypted_key_is_refused_with_a_reason() -> None:
    with pytest.raises(ImproperlyConfigured, match="sslpassword"):
        connection_words("default", {**SETTINGS, "OPTIONS": {"sslpassword": "secret"}})
