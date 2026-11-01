"""djox: a truly async Django ORM with a Rust core. SQLite first.

This first release is the engine. `djox.Database` runs SQL from async code on
connections that Rust owns, so Python's event loop never waits for the
database. The Django-style API (`djox.Manager`, `atomic()`, hooks...) is built
on top of it next.

```python
import djox

with djox.Database.sqlite("app.sqlite3") as database:
    rows = await database.fetch_all("SELECT id, title FROM book WHERE pages > %s", [100])
```

Don't point it at a file that Django's own SQLite backend uses in the same
process yet: the two bundle separate copies of SQLite, which can't see each
other's locks. djox's Django `ENGINE` (next release) removes that limit.
"""

from djox._core import (
    Database,
    DatabaseClosedError,
    DatabaseError,
    Executed,
    IntegrityError,
    OperationalError,
)

__all__ = [
    "Database",
    "DatabaseClosedError",
    "DatabaseError",
    "Executed",
    "IntegrityError",
    "OperationalError",
]
