"""Creating and destroying test databases.

Django's SQLite backend tests on an in-memory database by default. An
in-memory database belongs to the one connection that opened it, but djox's
async database (`djox.connections`) opens several connections to the same
file. So djox tests on a file instead:

| `NAME`                       | `TEST["NAME"]` | Test database                                  |
|------------------------------|----------------|------------------------------------------------|
| `BASE_DIR / "db.sqlite3"`    | not set        | `BASE_DIR / "test_db.sqlite3"`, like Django's other backends |
| `":memory:"`                 | not set        | `djox_test_default.sqlite3` in the temp folder |
| anything                     | set            | `TEST["NAME"]`, as in Django                   |

With WAL mode a database is up to three files: `db.sqlite3`, `db.sqlite3-wal`
(changes not copied into the database yet) and `db.sqlite3-shm` (an index of
the `-wal` file). Deleting a test database deletes all three: a leftover
`-wal` file would be replayed into the next database with the same name.
"""

import tempfile
from pathlib import Path

from django.db.backends.sqlite3 import creation

from djox._connections import connections


class DatabaseCreation(creation.DatabaseCreation):
    """Django's SQLite test database handling, with files instead of memory."""

    def _get_test_db_name(self) -> str:
        settings = self.connection.settings_dict
        if settings["TEST"]["NAME"]:
            # django-stubs leave out Django's private creation methods, hence the ignores below.
            return str(super()._get_test_db_name())  # type: ignore[misc]
        name = settings["NAME"]
        if self.is_in_memory_db(name):
            return str(Path(tempfile.gettempdir()) / f"djox_test_{self.connection.alias}.sqlite3")
        path = Path(name)
        return str(path.with_name(f"test_{path.name}"))

    def _create_test_db(self, verbosity: int, autoclobber: bool, keepdb: bool = False) -> str:
        test_database_name = str(super()._create_test_db(verbosity, autoclobber, keepdb))  # type: ignore[misc]
        if not keepdb and not self.is_in_memory_db(test_database_name):
            # Django deleted the old database file; delete what WAL mode left next to it.
            remove_wal_files(test_database_name)
        return test_database_name

    def _destroy_test_db(self, test_database_name: str, verbosity: int) -> None:
        # The async database still has the file open: close it first.
        connections.close(self.connection.alias)
        super()._destroy_test_db(test_database_name, verbosity)  # type: ignore[misc]
        if test_database_name and not self.is_in_memory_db(test_database_name):
            remove_wal_files(test_database_name)


def remove_wal_files(database_name: str) -> None:
    """Delete the `-wal` and `-shm` files of a database, if they exist."""
    for suffix in ("-wal", "-shm"):
        Path(database_name + suffix).unlink(missing_ok=True)
