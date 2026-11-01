"""`DatabaseOperations`: Django's Postgres operations, with djox's compilers.

Django asks a backend's operations for the compiler classes that turn queries
into SQL, by module name. djox's compilers are Django's, plus a way to hand a
statement to async code (see `compiler.py`).
"""

from django.db.backends.postgresql import operations


class DatabaseOperations(operations.DatabaseOperations):
    """Django's Postgres `DatabaseOperations`, with djox's compilers."""

    compiler_module = "djox.backends.postgresql.compiler"
