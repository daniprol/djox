"""Reads `DATABASES[alias]` for djox's async Postgres database.

`OPTIONS` belong to psycopg (Django passes them to `psycopg.connect()`),
plus one of djox's own:

| Option            | Default | What it does                                       |
|-------------------|---------|----------------------------------------------------|
| `async_pool_size` | `10`    | The most connections of the async database (`djox.connections`) |

djox removes its option before Django sees `OPTIONS`, since psycopg
refuses names it doesn't know.

The async database connects with the same `NAME`, `USER`, `PASSWORD`,
`HOST` and `PORT`, and the `OPTIONS` that are connection settings libpq
and djox both understand (`sslmode`, `connect_timeout`...). A few psycopg
options change how the connection behaves in ways djox can't follow yet:
those raise `ImproperlyConfigured` rather than being quietly dropped.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ImproperlyConfigured

DJOX_OPTIONS = ("async_pool_size",)

# libpq connection settings the async database passes on as they are.
CONNECTION_OPTIONS = (
    "sslmode",
    "connect_timeout",
    "application_name",
    "options",
    "keepalives",
    "keepalives_idle",
    "keepalives_interval",
    "keepalives_retries",
    "target_session_attrs",
    "channel_binding",
)

# Options djox's async database can't honour yet, and why.
UNSUPPORTED_OPTIONS = {
    "service": "djox can't read pg_service.conf: give NAME, USER, HOST... instead",
    "assume_role": "djox's async connections can't switch roles yet",
}

# `sslmode` values that work without TLS (djox can't encrypt connections yet).
SSLMODES_WITHOUT_TLS = ("disable", "allow", "prefer")


@dataclass(frozen=True)
class PostgresOptions:
    """djox's own options of one database alias."""

    async_pool_size: int = 10


def read_options(alias: str, options: Mapping[str, object]) -> PostgresOptions:
    """djox's options in `options` (the `OPTIONS` of `alias`), checked."""
    pool_size = options.get("async_pool_size", 10)
    if isinstance(pool_size, bool) or not isinstance(pool_size, int) or pool_size < 1:
        raise ImproperlyConfigured(
            f"settings.DATABASES[{alias!r}]['OPTIONS']['async_pool_size'] must be a number, 1 or more."
        )
    return PostgresOptions(async_pool_size=pool_size)


def without_djox_options(options: dict[str, Any]) -> dict[str, Any]:
    """`options` without djox's own, which psycopg doesn't know."""
    return {name: value for name, value in options.items() if name not in DJOX_OPTIONS}


def connection_words(alias: str, settings_dict: Mapping[str, Any]) -> str:
    """The async database's connection settings, as libpq's `key=value` words:
    `"dbname='app' user='django' host='localhost'"`."""
    options = settings_dict["OPTIONS"]
    for name, reason in UNSUPPORTED_OPTIONS.items():
        if options.get(name):
            raise ImproperlyConfigured(f"settings.DATABASES[{alias!r}]['OPTIONS'][{name!r}]: {reason}.")
    sslmode = options.get("sslmode")
    if sslmode is not None and sslmode not in SSLMODES_WITHOUT_TLS:
        raise ImproperlyConfigured(
            f"settings.DATABASES[{alias!r}]['OPTIONS']['sslmode'] is {sslmode!r}, but djox's async "
            f"connections can't use TLS yet. Use one of: {', '.join(SSLMODES_WITHOUT_TLS)}."
        )

    settings = {
        # `NAME = None` connects to the default `postgres` database, like Django.
        "dbname": settings_dict["NAME"] or "postgres",
        "user": settings_dict["USER"],
        "password": settings_dict["PASSWORD"],
        "host": settings_dict["HOST"],
        "port": settings_dict["PORT"],
    }
    for name in CONNECTION_OPTIONS:
        settings[name] = options.get(name)
    words = []
    for name, value in settings.items():
        if value is not None and value != "":
            words.append(f"{name}={quoted(str(value))}")
    return " ".join(words)


def quoted(value: str) -> str:
    """A value quoted for libpq's `key=value` words: in single quotes, with
    backslashes and quotes escaped by a backslash."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"
