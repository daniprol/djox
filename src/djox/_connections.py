"""`djox.connections`: the async database of each Django database alias.

Django's `django.db.connections["default"]` gives the sync connection of the
current thread. `djox.connections["default"]` gives the async `djox.Database`
for the same settings: one per alias and process, shared by every thread and
event loop, opened the first time it is asked for.

```python
import djox

database = djox.connections["default"]
rows = await database.fetch_all("SELECT id, title FROM library_book WHERE pages > %s", [300])
```

It needs the alias's `ENGINE` to be one of djox's: `"djox.backends.sqlite3"`
(then Django's sync connections and this async database share djox's single
SQLite) or `"djox.backends.postgresql"` (a pool of connections to the same server).

If the alias's `NAME` changes (Django's test runner switches to the test
database), or its connections' time zone does (a test changes `TIME_ZONE`),
the next lookup closes the old database and opens a new one.
"""

import threading
from dataclasses import dataclass
from typing import Any

from djox._core import Database


@dataclass
class OpenDatabase:
    """An async database, and the `NAME` and time zone it was opened for."""

    name: str
    time_zone: str
    database: Database

    def is_open_for(self, name: str, time_zone: str) -> bool:
        """Whether this database is still open, for this `NAME` and time zone."""
        return (self.name, self.time_zone) == (name, time_zone) and not self.database.closed


class Connections:
    """The async database of each Django alias: `djox.connections["default"]`."""

    def __init__(self) -> None:
        self._open: dict[str, OpenDatabase] = {}
        # Two threads asking for the same alias at once must not open it twice.
        self._lock = threading.Lock()

    def __getitem__(self, alias: str) -> Database:
        # Imported here so `import djox` works without Django.
        from django.core.exceptions import ImproperlyConfigured
        from django.db import connections as django_connections

        from djox.backends.engine import DjoxEngine

        wrapper = django_connections[alias]
        if not isinstance(wrapper, DjoxEngine):
            raise ImproperlyConfigured(
                f"djox.connections[{alias!r}] needs settings.DATABASES[{alias!r}]['ENGINE'] to be "
                f"'djox.backends.sqlite3' or 'djox.backends.postgresql', not {wrapper.settings_dict['ENGINE']!r}."
            )
        return self.database_for(wrapper)

    def database_for(self, wrapper: Any) -> Database:
        """The async database of `wrapper`'s alias, for code that has the
        alias's Django `DatabaseWrapper` already (one of a djox `ENGINE`)."""
        name = str(wrapper.settings_dict["NAME"])
        time_zone = wrapper.timezone_name
        # Usually the database is open already. Reading a dict needs no lock
        # (the GIL makes `dict.get()` safe); only opening one does.
        opened = self._open.get(wrapper.alias)
        if opened is not None and opened.is_open_for(name, time_zone):
            return opened.database
        with self._lock:
            opened = self._open.get(wrapper.alias)
            if opened is not None and opened.is_open_for(name, time_zone):
                return opened.database  # another thread opened it meanwhile
            if opened is not None:
                opened.database.close()
            database: Database = wrapper.open_async_database()
            self._open[wrapper.alias] = OpenDatabase(name, time_zone, database)
            return database

    def close(self, alias: str) -> None:
        """Close the async database of `alias`, if it is open. Statements
        already sent still finish."""
        with self._lock:
            opened = self._open.pop(alias, None)
        if opened is not None:
            opened.database.close()

    def close_all(self) -> None:
        """Close every open async database."""
        with self._lock:
            aliases = list(self._open)
        for alias in aliases:
            self.close(alias)


connections = Connections()
