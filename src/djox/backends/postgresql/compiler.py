"""Django's SQL compilers for Postgres, able to hand their statement to djox's async code.

djox's compilers (`djox.backends.sqlite3.compiler`, see its docs for how a
statement is handed over) subclass Django's general compilers, which work
for every database: nothing in them is about SQLite. So Postgres uses them
as they are.

Only the `INSERT` compiler differs: Django's Postgres backend has its own,
which inserts many rows at once with `UNNEST`. This one is that compiler
with djox's `execute_sql()`.
"""

from django.db.backends.postgresql import compiler

from djox.backends.sqlite3.compiler import (
    SQLAggregateCompiler,
    SQLCompiler,
    SQLDeleteCompiler,
    SQLUpdateCompiler,
)
from djox.backends.sqlite3.compiler import SQLInsertCompiler as DjoxInsertCompiler

__all__ = ["SQLAggregateCompiler", "SQLCompiler", "SQLDeleteCompiler", "SQLInsertCompiler", "SQLUpdateCompiler"]


# A class with two parents looks a method up in the first, then the second:
# Django's Postgres compiler writes the SQL (`UNNEST`), djox's runs it.
class SQLInsertCompiler(compiler.SQLInsertCompiler, DjoxInsertCompiler):
    """Django's Postgres `INSERT` compiler, which can hand its statement to `capture()`."""
