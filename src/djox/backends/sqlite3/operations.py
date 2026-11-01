"""`DatabaseOperations`: Django's SQLite operations, with djox's compilers.

Django asks a backend's operations for the compiler classes that turn queries
into SQL, by module name. djox's compilers are Django's, plus a way to hand a
statement to async code (see `compiler.py`).
"""

from django.db.backends.sqlite3 import operations


class DatabaseOperations(operations.DatabaseOperations):
    """Django's SQLite `DatabaseOperations`, with djox's compilers."""

    compiler_module = "djox.backends.sqlite3.compiler"
