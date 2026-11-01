"""Type hints for the Rust extension `djox._core` (see `src/*.rs`)."""

import asyncio
import datetime
import decimal
import uuid
from collections.abc import Iterable, Sequence
from os import PathLike
from types import TracebackType
from typing import Self, TypeAlias

# What a query parameter can be (see `src/values.rs`).
Parameter: TypeAlias = (
    bool
    | int
    | float
    | str
    | bytes
    | bytearray
    | memoryview
    | datetime.datetime
    | datetime.date
    | datetime.time
    | decimal.Decimal
    | uuid.UUID
    | None
)
# What a row value can be: SQLite's storage classes.
RowValue: TypeAlias = int | float | str | bytes | None

class Database:
    """A database used from async code. Make one with a backend's constructor,
    like `Database.sqlite("app.sqlite3")`, or get one from Django's settings
    with `djox.connections["default"]`.

    SQL uses Django's `%s` placeholders; write `%%` for a literal `%`.

    The query methods send the statement right away and return an
    `asyncio.Future` to await, so statements reach the database in the order
    the methods were called. Await it, or pass it to `asyncio.gather()`; to
    run it as a task, use `asyncio.ensure_future()` (not `create_task()`,
    which only takes coroutines).
    """

    @staticmethod
    def sqlite(path: str | PathLike[str], *, readers: int | None = None, timeout: float = 5.0) -> Database:
        """Open a SQLite database file (created if missing) and put it in WAL mode.

        Writes run one at a time on one writer connection; reads run in
        parallel on `readers` reader connections (default: one per CPU core,
        at most 4; 0 means the writer runs everything). `timeout` is how many
        seconds to wait for a lock held by another process. In-memory
        databases raise `ValueError`: their connections can't share data.
        """

    def fetch_all(
        self, sql: str, parameters: Sequence[Parameter] | None = None
    ) -> asyncio.Future[list[tuple[RowValue, ...]]]:
        """Run a query and return all its rows. A statement that writes
        (like `INSERT ... RETURNING`) still works: it runs on the writer."""

    def fetch_one(
        self, sql: str, parameters: Sequence[Parameter] | None = None
    ) -> asyncio.Future[tuple[RowValue, ...] | None]:
        """Run a query and return its first row, or `None`."""

    def execute(self, sql: str, parameters: Sequence[Parameter] | None = None) -> asyncio.Future[Executed]:
        """Run a statement that changes data."""

    def execute_many(self, sql: str, parameter_lists: Iterable[Sequence[Parameter]]) -> asyncio.Future[Executed]:
        """Run a statement once per list of parameters, in one transaction:
        either all of them are kept or none is."""

    def close(self) -> None:
        """Close the database. Statements already sent still finish; new ones
        raise `DatabaseClosedError`. Closing twice does nothing."""

    @property
    def closed(self) -> bool:
        """Whether `close()` was called."""

    @property
    def backend(self) -> str:
        """The backend's short name, like `"sqlite"`."""

    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

class Executed:
    """What a statement that changes data gives back."""

    @property
    def rows_affected(self) -> int:
        """How many rows the statement inserted, changed or deleted."""

    @property
    def last_insert_id(self) -> int:
        """The id of the last inserted row (SQLite's `rowid`)."""

class DatabaseError(Exception):
    """Base class for all djox database errors."""

class IntegrityError(DatabaseError):
    """A constraint failed: a duplicate unique value, a missing foreign key row..."""

class OperationalError(DatabaseError):
    """The database could not run the statement: a syntax error, a missing table, a lock timeout..."""

class DatabaseClosedError(DatabaseError):
    """The database was closed."""
