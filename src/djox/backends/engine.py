"""`DjoxEngine`: what every Django `DatabaseWrapper` of a djox `ENGINE` has.

djox has one `ENGINE` per database (`djox.backends.sqlite3`,
`djox.backends.postgresql`). Each one's `DatabaseWrapper` subclasses Django's
own and this small class, so the rest of djox can ask "does this alias run
on djox?" with one `isinstance()`, and open the alias's async database
without knowing which database it is.
"""

from functools import cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import djox

    # `Row` exists only in the type hints of `djox._core` (`_core.pyi`).
    from djox._core import Row


class DjoxEngine:
    """The part of a djox `DatabaseWrapper` that is the same for every database."""

    def open_async_database(self) -> "djox.Database":
        """Open the async `djox.Database` for this alias's settings.
        `djox.connections[alias]` calls it."""
        raise NotImplementedError

    def rows_from_async_database(self, rows: "list[Row]") -> "list[Row]":
        """The rows the async database returned, as this alias's sync
        connections would return them. The same rows, unless the database
        needs a change (Postgres moves dates and times into the connection's
        time zone)."""
        return rows


def runs_on_djox(wrapper: Any) -> bool:
    """Whether `wrapper` (a Django `DatabaseWrapper`) belongs to a djox `ENGINE`."""
    return isinstance(wrapper, DjoxEngine)


def alias_runs_on_djox(alias: str) -> bool:
    """Whether the Django database `alias` uses one of djox's `ENGINE`s.

    The same answer as `runs_on_djox(connections[alias])`, from the alias's
    settings rather than its connection. Django keeps a connection per thread
    (in asgiref's `Local`, a thread-local that also follows async code), and
    finding this thread's one costs a few microseconds: more than this check
    is worth, when every query of `djox.QuerySet` makes it.
    """
    from django.db import connections

    settings_dict = connections.settings.get(alias)
    if settings_dict is None:
        return runs_on_djox(connections[alias])  # Django raises its own error for an unknown alias
    return engine_runs_on_djox(settings_dict["ENGINE"])


# `@cache` remembers the answer for each argument, like a dict filled on first use.
@cache
def engine_runs_on_djox(engine: str) -> bool:
    """Whether the `ENGINE` named `engine` is one of djox's."""
    from django.db.utils import load_backend

    return issubclass(load_backend(engine).DatabaseWrapper, DjoxEngine)
