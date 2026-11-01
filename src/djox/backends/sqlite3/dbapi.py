"""What Django looks up on a database driver.

Django's `DatabaseWrapper.Database` is the driver module, like Python's
`sqlite3`. Django reads two things from it: the PEP 249 exception classes, to
turn a driver's `IntegrityError` into `django.db.IntegrityError` and so on,
and the SQLite version. This module offers djox's.
"""

from djox._core import (
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    sqlite_version,
)

# How Django's `BinaryField` wraps bytes before saving them (like `sqlite3.Binary`).
Binary = memoryview

# The version of the SQLite bundled in djox, like `(3, 54, 1)`, which is the
# one that runs every query (Python's `sqlite3` may have another).
sqlite_version_info = tuple(int(part) for part in sqlite_version.split("."))

__all__ = [
    "Binary",
    "DataError",
    "DatabaseError",
    "Error",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "NotSupportedError",
    "OperationalError",
    "ProgrammingError",
    "sqlite_version",
    "sqlite_version_info",
]
