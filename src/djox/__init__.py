"""djox: a truly async Django ORM with a Rust core. SQLite and Postgres.

djox runs SQL on connections that Rust owns, with the SQLite bundled in djox.

- `djox.Database` runs SQL from async code, without ever blocking Python's
  event loop.
- `djox.Connection` is one connection for sync code, shaped like Python's
  `sqlite3.Connection`.
- `"ENGINE": "djox.backends.sqlite3"` makes Django use djox's SQLite too, and
  `djox.connections["default"]` gives the async database for a Django alias.
- `djox.Manager` (with `djox.QuerySet`) runs Django's async QuerySet methods
  (`aget()`, `acount()`, `async for`...) on djox, and `djox.Model` adds the
  model's own `asave()`, `adelete()` and `arefresh_from_db()`.
- `djox.atomic()` and `djox.on_commit()` are Django's transactions, for
  async code (`async with djox.atomic(): ...`).
- `djox.aprefetch_related_objects()` is Django's prefetching, for async
  code, on djox's own threads (`djox.prefetch`).
- `djox.add_hooks(djox.Hooks(before_query=..., after_query=...))` calls your
  functions around every SQL statement (`djox.hooks`).

```python
import djox

with djox.Database.sqlite("app.sqlite3") as database:
    rows = await database.fetch_all("SELECT id, title FROM book WHERE pages > %s", [100])
```

Use djox's `ENGINE` whenever djox and Django share a database file: Python's
`sqlite3` bundles a second copy of SQLite, and two copies in one process
can't see each other's locks, so writes can get lost.
"""

from importlib.metadata import version
from typing import TYPE_CHECKING, Any

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
    RowStream,
    sqlite_version,
)
from djox.hooks import Hooks, Query, QueryFinished, add_hooks, remove_hooks

# The version as pip and PyPI write it (`0.1.0`). It comes from Cargo.toml, its one source.
__version__ = version("djox")

if TYPE_CHECKING:
    from djox.manager import Manager
    from djox.models import Model
    from djox.prefetch import aprefetch_related_objects
    from djox.query import QuerySet
    from djox.transaction import atomic, on_commit


def __getattr__(name: str) -> Any:
    """Import the parts of djox that need Django the first time they are used.

    Python calls a module's `__getattr__` for names the module doesn't have.
    Importing them lazily keeps `import djox` working without Django, or
    before Django's settings are ready (in `settings.py` itself).
    """
    if name == "Manager":
        from djox.manager import Manager

        return Manager
    if name == "Model":
        from djox.models import Model

        return Model
    if name == "QuerySet":
        from djox.query import QuerySet

        return QuerySet
    if name == "atomic":
        from djox.transaction import atomic

        return atomic
    if name == "aprefetch_related_objects":
        from djox.prefetch import aprefetch_related_objects

        return aprefetch_related_objects
    if name == "on_commit":
        from djox.transaction import on_commit

        return on_commit
    raise AttributeError(f"module 'djox' has no attribute {name!r}")


__all__ = [
    "Connection",
    "Cursor",
    "DataError",
    "Database",
    "DatabaseClosedError",
    "DatabaseError",
    "Error",
    "Executed",
    "Hooks",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "Manager",
    "Model",
    "NotSupportedError",
    "OperationalError",
    "ProgrammingError",
    "Query",
    "QueryFinished",
    "QuerySet",
    "RowStream",
    "__version__",
    "add_hooks",
    "aprefetch_related_objects",
    "atomic",
    "connections",
    "on_commit",
    "remove_hooks",
    "sqlite_version",
]
