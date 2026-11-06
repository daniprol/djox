"""Feature flags of djox's SQLite backend.

Django's SQLite backend reads a few flags from the version of the SQLite
inside Python's `sqlite3` module when it is imported. djox runs every query
on its own bundled SQLite (3.54 or newer), so those flags are set from that.
"""

from django.db.backends.sqlite3 import features

# SQLite's default `SQLITE_MAX_VARIABLE_NUMBER`, which djox's bundled SQLite keeps.
BUNDLED_SQLITE_MAX_VARIABLE_NUMBER = 32766


class DatabaseFeatures(features.DatabaseFeatures):
    """Django's SQLite features, for the SQLite bundled in djox."""

    # SQLite 3.44+ accepts `ORDER BY` inside aggregates, like `GROUP_CONCAT(x ORDER BY y)`.
    supports_aggregate_order_by_clause = True

    @property
    def max_query_params(self) -> int:  # type: ignore[override]  # a property in Django too (not in django-stubs)
        """The most parameters one statement may have (`SQLITE_MAX_VARIABLE_NUMBER`).

        Django asks the open connection (a program may lower it there with
        `setlimit()`), and opens one to ask. Async code can't open one
        (`abulk_create()` splits its objects into batches by this number):
        without an open connection, this is the limit djox's bundled SQLite
        gives every new connection.
        """
        if self.connection.connection is None:
            return BUNDLED_SQLITE_MAX_VARIABLE_NUMBER
        limit: int = super().max_query_params
        return limit
