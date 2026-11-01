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

It needs the alias's `ENGINE` to be `"djox.backends.sqlite3"`: then Django's
sync connections and this async database share djox's single SQLite.

If the alias's `NAME` changes (Django's test runner switches to the test
database), the next lookup closes the old database and opens the new one.
"""

import threading
from dataclasses import dataclass

from djox._core import Database


@dataclass
class OpenDatabase:
    """An async database, and the `NAME` it was opened for."""

    name: str
    database: Database


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

        from djox.backends.sqlite3.base import DatabaseWrapper

        wrapper = django_connections[alias]
        if not isinstance(wrapper, DatabaseWrapper):
            raise ImproperlyConfigured(
                f"djox.connections[{alias!r}] needs settings.DATABASES[{alias!r}]['ENGINE'] "
                f"to be 'djox.backends.sqlite3', not {wrapper.settings_dict['ENGINE']!r}."
            )
        name = str(wrapper.settings_dict["NAME"])
        with self._lock:
            opened = self._open.get(alias)
            if opened is not None and opened.name == name and not opened.database.closed:
                return opened.database
            if opened is not None:
                opened.database.close()
            database = wrapper.open_async_database()
            self._open[alias] = OpenDatabase(name, database)
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
