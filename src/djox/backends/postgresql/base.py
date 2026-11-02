"""`DatabaseWrapper`: Django's Postgres connection manager, with djox's async database.

Django makes one `DatabaseWrapper` per database alias and thread. For sync
code it opens psycopg connections exactly like Django's own Postgres backend.
djox adds five things:

- djox's hooks around every statement Django's cursors run (`djox.hooks`);
- a note of each transaction it opens, so async code started inside a sync
  `atomic()` block (like Django's `TestCase`) runs in that transaction, on
  its psycopg connection (see `djox._running`);
- `open_async_database()`, the pool of Rust connections `djox.connections[alias]`
  gives async code, opened from the same settings;
- `rows_from_async_database()`, which gives that pool's rows the time zone
  psycopg would give them;
- `wrap_database_errors`, which turns djox's exceptions into Django's, as
  well as psycopg's.
"""

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db.backends.postgresql import base
from django.utils.functional import cached_property

import djox
from djox import _running, hooks
from djox.backends.engine import DjoxEngine
from djox.backends.postgresql.creation import DatabaseCreation
from djox.backends.postgresql.errors import DatabaseErrors
from djox.backends.postgresql.operations import DatabaseOperations
from djox.backends.postgresql.options import connection_words, read_options, tls_settings, without_djox_options
from djox.backends.postgresql.time_zones import needs_moving, rows_in_time_zone

if TYPE_CHECKING:
    # `Row` exists only in the type hints of `djox._core` (`_core.pyi`).
    from djox._core import Row


class DatabaseWrapper(DjoxEngine, base.DatabaseWrapper):
    """Django's Postgres `DatabaseWrapper`, with djox's async database."""

    creation_class = DatabaseCreation
    ops_class = DatabaseOperations

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # djox's hooks around every statement Django's cursors run (see `djox.hooks`).
        self.execute_wrappers.append(hooks.execute_with_hooks)

    @cached_property
    def wrap_database_errors(self) -> DatabaseErrors:
        """Turns psycopg's exceptions and djox's into Django's (see `errors.py`)."""
        return DatabaseErrors(self)

    def get_connection_params(self) -> dict[str, Any]:
        """Django's psycopg connection settings, without djox's own options."""
        read_options(self.alias, self.settings_dict["OPTIONS"])  # checked here, early
        return without_djox_options(super().get_connection_params())

    def set_autocommit(self, autocommit: bool, force_begin_transaction_with_broken_autocommit: bool = False) -> None:
        """Django's `set_autocommit()`, which `atomic()` calls when the
        outermost block starts (`False`) and ends (`True`). djox also notes the
        open transaction, so async queries started inside it run in it (see
        `djox._running`)."""
        super().set_autocommit(autocommit, force_begin_transaction_with_broken_autocommit)
        _running.note_sync_transaction(self.alias, self, is_open=not autocommit)

    def open_async_database(self) -> djox.Database:
        """Open the async `djox.Database` for this alias: a pool of
        connections to the same server and database, in the same time zone
        as Django's own connections. `djox.connections[alias]` calls it."""
        options = read_options(self.alias, self.settings_dict["OPTIONS"])
        return djox.Database.postgres(
            connection_words(self.alias, self.settings_dict),
            pool_size=options.async_pool_size,
            time_zone=self.timezone_name,
            # The same TLS as Django's psycopg connections (`sslmode`, `sslrootcert`...).
            **tls_settings(self.settings_dict["OPTIONS"]),
        )

    def rows_from_async_database(self, rows: "list[Row]") -> "list[Row]":
        """The async database's rows, with each `timestamptz` in the
        connection's time zone, as psycopg gives it (see `time_zones.py`)."""
        if not rows or not needs_moving(self.timezone_name, settings.USE_TZ):
            return rows
        return rows_in_time_zone(rows, self.timezone_name, keeps_zone=settings.USE_TZ)
