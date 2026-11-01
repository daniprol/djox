"""`DatabaseWrapper`: Django's SQLite connection manager, on djox's SQLite.

Django makes one `DatabaseWrapper` per database alias and thread. It opens the
connection, starts and ends transactions (`atomic()`), and hands out cursors.
Django's SQLite wrapper does all of that through a `sqlite3.Connection`; this
one opens a `djox.Connection` instead, which has the same methods but runs on
the SQLite bundled in djox. Everything else is Django's own code.

Every new connection is prepared like Django prepares its own (see
`prepare_connection`), and so is every connection of the async database that
`djox.connections[alias]` opens, so both understand the same SQL.
"""

from collections.abc import Callable, Iterable
from importlib import import_module
from typing import Any, cast

from django.core.exceptions import ImproperlyConfigured
from django.db.backends.sqlite3 import base
from django.utils.asyncio import async_unsafe

import djox
from djox.backends.sqlite3 import dbapi
from djox.backends.sqlite3.creation import DatabaseCreation
from djox.backends.sqlite3.features import DatabaseFeatures
from djox.backends.sqlite3.options import SqliteOptions, read_options


class DatabaseWrapper(base.DatabaseWrapper):
    """Django's SQLite `DatabaseWrapper`, opening `djox.Connection`s."""

    Database = dbapi
    creation_class = DatabaseCreation
    features_class = DatabaseFeatures

    # `connection` is the open `djox.Connection` (or `None`); Django's stubs call it `Any`.
    connection: djox.Connection | None

    def get_connection_params(self) -> dict[str, Any]:
        """Read and check the settings. Django calls this before each new connection."""
        name = self.settings_dict["NAME"]
        if not name:
            raise ImproperlyConfigured("settings.DATABASES is improperly configured. Please supply the NAME value.")
        options = self.options()
        # Django's own code reads these two: `transaction_mode` when `atomic()` starts a transaction.
        self.transaction_mode = options.transaction_mode
        self.init_commands = list(options.init_commands)
        return {"path": str(name), "timeout": options.timeout}

    @async_unsafe
    def get_new_connection(self, conn_params: dict[str, Any]) -> djox.Connection:
        """Open and prepare a new `djox.Connection`. (`conn_params` is Django's name.)"""
        connection = djox.Connection.sqlite(conn_params["path"], timeout=conn_params["timeout"])
        prepare_connection(connection, self.init_commands)
        return connection

    def create_cursor(self, name: str | None = None) -> djox.Cursor:
        """A new cursor. Django wraps it in its own `CursorWrapper`."""
        assert self.connection is not None, "Django opens the connection before asking for a cursor"
        return self.connection.cursor()

    def _set_autocommit(self, autocommit: bool) -> None:
        """Django's `transaction.set_autocommit()`, on a `djox.Connection`."""
        assert self.connection is not None, "Django opens the connection before changing autocommit"
        with self.wrap_database_errors:
            self.connection.autocommit = autocommit

    def get_database_version(self) -> tuple[int, ...]:
        """The version of the SQLite that runs the queries: djox's own."""
        return dbapi.sqlite_version_info

    def options(self) -> SqliteOptions:
        """This alias's checked `OPTIONS`."""
        return read_options(self.alias, self.settings_dict["OPTIONS"])

    def open_async_database(self) -> djox.Database:
        """Open the async `djox.Database` for this alias's file, prepared like
        Django's own connections. `djox.connections[alias]` calls it."""
        options = self.options()
        init_commands = options.init_commands

        def prepare(connection: djox.Connection) -> None:
            prepare_connection(connection, init_commands)

        return djox.Database.sqlite(
            str(self.settings_dict["NAME"]), readers=options.readers, timeout=options.timeout, prepare=prepare
        )


def prepare_connection(connection: djox.Connection, init_commands: Iterable[str] = ()) -> None:
    """Set up a new connection the way Django's SQLite backend does.

    - Register Django's SQL functions (`django_date_extract`, `REGEXP`,
      `STDDEV_POP`...). The SQL Django writes for date lookups, regular
      expressions and some aggregates calls them. djox calls Django's own
      `register()`, so they are always the ones of the installed Django.
    - Turn on foreign key checks, and turn off `legacy_alter_table` (some
      SQLite builds turn it on; it breaks renaming tables in migrations).
    - Run the `init_command` option.
    """
    register_django_functions(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA legacy_alter_table = OFF")
    for command in init_commands:
        connection.execute(command)


def register_django_functions(connection: djox.Connection) -> None:
    """Call `django.db.backends.sqlite3._functions.register(connection)`.

    The module is private to Django and has no type hints in django-stubs, so
    it is imported by name and its `register` given a type here.
    """
    functions = import_module("django.db.backends.sqlite3._functions")
    register = cast("Callable[[djox.Connection], None]", functions.register)
    register(connection)
