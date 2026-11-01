"""Building a `QuerySet`'s results from rows djox already fetched.

Django turns rows into results with an "iterable class": `ModelIterable`
makes model instances (with `select_related()` objects and annotations),
`ValuesIterable` makes dicts, `ValuesListIterable` tuples, and so on. Each one
asks the query for a compiler, asks the compiler to run the SQL, and builds
the results from the rows.

`results_from_rows()` lets Django's iterable class do exactly that, on the
same code path as `list(queryset)`, but with the SQL already compiled and run
by djox. It gives the class the compiler that compiled the SQL, set up to hand
back the rows instead of running anything. So nothing is compiled twice, and
results come out the same as Django's on every Django version.
"""

from collections.abc import Iterable
from typing import Any, cast

from django.db import models
from django.db.models.sql.compiler import SQLCompiler
from django.db.models.sql.constants import GET_ITERATOR_CHUNK_SIZE


def results_from_rows(queryset: models.QuerySet[Any], compiler: SQLCompiler, rows: Any) -> list[Any]:
    """The results of `queryset`, built by Django from `rows`.

    `queryset` must be a private copy (`queryset._chain()`) whose query
    `compiler` compiled; `rows` is what `compiler.execute_sql()` would return.

    Setting a method on one object (`query.get_compiler = ...`) changes that
    object only, not its class: like in any Python code. The copy is thrown
    away afterwards, so nothing else sees the change.
    """

    def get_compiler(*args: Any, **kwargs: Any) -> SQLCompiler:
        return compiler

    def execute_sql(*args: Any, **kwargs: Any) -> Any:
        return rows

    queryset.query.get_compiler = get_compiler  # type: ignore[method-assign]
    compiler.execute_sql = execute_sql  # type: ignore[method-assign]
    iterable = queryset._iterable_class(queryset, chunked_fetch=False, chunk_size=GET_ITERATOR_CHUNK_SIZE)
    # django-stubs doesn't say the iterable classes can be iterated; they can.
    return list(cast("Iterable[Any]", iterable))
