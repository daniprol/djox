"""Feature flags of djox's SQLite backend.

Django's SQLite backend reads a few flags from the version of the SQLite
inside Python's `sqlite3` module when it is imported. djox runs every query
on its own bundled SQLite (3.54 or newer), so those flags are set from that.
"""

from django.db.backends.sqlite3 import features


class DatabaseFeatures(features.DatabaseFeatures):
    """Django's SQLite features, for the SQLite bundled in djox."""

    # SQLite 3.44+ accepts `ORDER BY` inside aggregates, like `GROUP_CONCAT(x ORDER BY y)`.
    supports_aggregate_order_by_clause = True
