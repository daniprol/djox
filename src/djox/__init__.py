"""djox: a truly async Django ORM with a Rust core. SQLite first.

djox runs SQL on connections that Rust owns, with the SQLite bundled in djox.

- `djox.Database` runs SQL from async code, without ever blocking Python's
  event loop.
- `djox.Connection` is one connection for sync code, shaped like Python's
  `sqlite3.Connection`.
- `"ENGINE": "djox.backends.sqlite3"` makes Django use djox's SQLite too, and
  `djox.connections["default"]` gives the async database for a Django alias.

```python
import djox

with djox.Database.sqlite("app.sqlite3") as database:
    rows = await database.fetch_all("SELECT id, title FROM book WHERE pages > %s", [100])
```

Use djox's `ENGINE` whenever djox and Django share a database file: Python's
`sqlite3` bundles a second copy of SQLite, and two copies in one process
can't see each other's locks, so writes can get lost.
"""

from djox._connections import connections
from djox._core import (
    Connection,
    Cursor,
    Database,
    DatabaseClosedError,
    DatabaseError,
    DataError,
    Error,
    Executed,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    sqlite_version,
)

__all__ = [
    "Connection",
    "Cursor",
    "DataError",
    "Database",
    "DatabaseClosedError",
    "DatabaseError",
    "Error",
    "Executed",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "NotSupportedError",
    "OperationalError",
    "ProgrammingError",
    "connections",
    "sqlite_version",
]
