"""`DjoxEngine`: what every Django `DatabaseWrapper` of a djox `ENGINE` has.

djox has one `ENGINE` per database (`djox.backends.sqlite3`,
`djox.backends.postgresql`). Each one's `DatabaseWrapper` subclasses Django's
own and this small class, so the rest of djox can ask "does this alias run
on djox?" with one `isinstance()`, and open the alias's async database
without knowing which database it is.
"""

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
