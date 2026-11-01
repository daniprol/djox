"""Type hints for the Rust extension `djox._core` (see `src/*.rs`)."""

import asyncio
import datetime
import decimal
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from os import PathLike
from types import TracebackType
from typing import Any, Protocol, Self, TypeAlias

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
# A row, as `fetch_all()` and the cursors return it.
Row: TypeAlias = tuple[RowValue, ...]
# The parameters of one statement, for a `Cursor`: `%s` with a sequence, `%(name)s` with a mapping.
Parameters: TypeAlias = Sequence[Parameter] | Mapping[str, Parameter]

# The version of the SQLite bundled in djox, like "3.54.1".
sqlite_version: str

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
    def sqlite(
        path: str | PathLike[str],
        *,
        readers: int | None = None,
        timeout: float = 5.0,
        prepare: Callable[[Connection], object] | None = None,
    ) -> Database:
        """Open a SQLite database file (created if missing) and put it in WAL mode.

        Writes run one at a time on one writer connection; reads run in
        parallel on `readers` reader connections (default: one per CPU core,
        at most 4; 0 means the writer runs everything). `timeout` is how many
        seconds to wait for a lock held by another connection. In-memory
        databases raise `ValueError`: their connections can't share data.

        `prepare`, if given, is called with each new connection before it
        starts working, to register SQL functions or run `PRAGMA`s. The
        connection is only lent: it can't be used after `prepare` returns.
        """

    def fetch_all(self, sql: str, parameters: Sequence[Parameter] | None = None) -> asyncio.Future[list[Row]]:
        """Run a query and return all its rows. A statement that writes
        (like `INSERT ... RETURNING`) still works: it runs on the writer."""

    def fetch_one(self, sql: str, parameters: Sequence[Parameter] | None = None) -> asyncio.Future[Row | None]:
        """Run a query and return its first row, or `None`."""

    def stream_rows(
        self, sql: str, parameters: Sequence[Parameter] | None = None, *, chunk_size: int = 2000
    ) -> RowStream:
        """Run a query and hand its rows over `chunk_size` at a time. The
        stream reads on a connection of its own, so other statements never
        wait for it. Only queries that read can be streamed."""

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

class RowStream:
    """The rows of one query, a chunk at a time (from `Database.stream_rows()`).

    When it is garbage collected, its connection is let go, even if not
    every row was read.
    """

    @property
    def chunk_size(self) -> int:
        """The most rows a chunk has."""

    def next_chunk(self) -> asyncio.Future[list[Row]]:
        """The next chunk of rows. A chunk with fewer than `chunk_size` rows
        is the last one; asking again raises `DatabaseClosedError`."""

class Executed:
    """What a statement that changes data gives back."""

    @property
    def rows_affected(self) -> int:
        """How many rows the statement inserted, changed or deleted."""

    @property
    def last_insert_id(self) -> int:
        """The id of the last inserted row (SQLite's `rowid`)."""

class AggregateClass(Protocol):
    """What `Connection.create_aggregate()` takes: a class whose instances
    have `step(*arguments)` and `finalize()`."""

    def __call__(self) -> Any: ...

class Connection:
    """One SQLite connection for sync code, shaped like Python's
    `sqlite3.Connection`, on the SQLite bundled in djox. Django's sync ORM uses
    it through `"ENGINE": "djox.backends.sqlite3"`.

    Every method blocks the calling thread until SQLite is done, but lets
    other Python threads run meanwhile.
    """

    @staticmethod
    def sqlite(path: str | PathLike[str], *, timeout: float = 5.0) -> Connection:
        """Open a SQLite database file (created if missing) and put it in WAL
        mode. `":memory:"` opens a private in-memory database."""

    def cursor(self) -> Cursor:
        """A new cursor."""

    def execute(self, sql: str, parameters: Parameters | None = None) -> Cursor:
        """Shortcut for `connection.cursor().execute(sql, parameters)`."""

    def executemany(self, sql: str, parameter_lists: Iterable[Parameters]) -> Cursor:
        """Shortcut for `connection.cursor().executemany(sql, parameter_lists)`."""

    def commit(self) -> None:
        """Commit the open transaction, if there is one."""

    def rollback(self) -> None:
        """Roll back the open transaction, if there is one."""

    def close(self) -> None:
        """Close the connection; an open transaction is rolled back. Closing twice does nothing."""

    @property
    def closed(self) -> bool:
        """Whether `close()` was called."""

    @property
    def in_transaction(self) -> bool:
        """Whether a transaction is open."""

    autocommit: bool
    """`True` (the default): each statement commits on its own, unless you run
    `BEGIN`. `False`: a write opens a transaction that stays open until
    `commit()` or `rollback()`."""

    def create_function(
        self, name: str, argument_count: int, function: Callable[..., Any], *, deterministic: bool = False
    ) -> None:
        """Register a Python function that SQL can call. `argument_count` -1 means any number."""

    def create_aggregate(self, name: str, argument_count: int, aggregate_class: AggregateClass) -> None:
        """Register a Python aggregate class, with `step(*arguments)` and `finalize()`."""

    def getlimit(self, category: int) -> int:
        """One of SQLite's limits, like `sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER`."""

    def setlimit(self, category: int, limit: int) -> int:
        """Change one of SQLite's limits for this connection; returns the old value."""

class Cursor:
    """Runs statements on a `Connection` and hands back their rows (PEP 249).

    Placeholders follow Django: with a sequence of parameters write `%s`, with
    a mapping write `%(name)s`, and in both cases `%%` for a literal `%`.
    Without parameters the SQL runs exactly as written.
    """

    arraysize: int
    """How many rows `fetchmany()` returns by default (1)."""

    def execute(self, sql: str, parameters: Parameters | None = None) -> Self:
        """Run a statement. Returns the cursor."""

    def executemany(self, sql: str, parameter_lists: Iterable[Parameters]) -> Self:
        """Run an `INSERT`, `UPDATE`, `DELETE` or `REPLACE` once per item. Returns the cursor."""

    def fetchone(self) -> Row | None:
        """The next row, or `None`."""

    def fetchmany(self, size: int | None = None) -> list[Row]:
        """The next `size` rows (default: `arraysize`)."""

    def fetchall(self) -> list[Row]:
        """All the remaining rows."""

    def close(self) -> None:
        """Close the cursor."""

    @property
    def description(self) -> tuple[tuple[str, None, None, None, None, None, None], ...] | None:
        """One 7-tuple per column (only the name is filled), or `None` for a statement without rows."""

    @property
    def rowcount(self) -> int:
        """How many rows the last `INSERT`, `UPDATE`, `DELETE` or `REPLACE` changed, or -1."""

    @property
    def lastrowid(self) -> int | None:
        """The id of the last inserted row."""

    @property
    def connection(self) -> Connection:
        """The connection this cursor runs statements on."""

    def setinputsizes(self, *sizes: object) -> None: ...
    def setoutputsize(self, *arguments: object) -> None: ...
    def __iter__(self) -> Iterator[Row]: ...
    def __next__(self) -> Row: ...

class ResultBuilder:
    """Turns result rows into what a `QuerySet` returns, for the fast lane
    (`djox._fast_lane`): model instances (what Django's `from_db()` would
    make), tuples (`values_list()`) or dicts (`values()`), built in Rust."""

    def __init__(
        self,
        names: Sequence[str],
        converters: Sequence[tuple[Sequence[Callable[[Any, Any, Any], Any]], Any] | None],
        conversions: Sequence[tuple[Any, ...] | None],
        connection: Any,
        model: type[Any] | None = None,
        model_state: type[Any] | None = None,
        related: Sequence[RelatedModel] = (),
    ) -> None:
        """`names`: per column, the attribute of an instance or the key of a
        dict. `converters`: per column, `None` or `(functions, expression)`,
        like Django's `get_converters()`. `conversions`: per column, `None` or
        the Rust conversion doing the same (`djox._fast_lane.conversions`).
        `model` and `model_state` are needed for `instances()` only.
        `related`: the models `select_related()` adds; the model's own columns
        end where the first one's begin."""

    def instances(self, rows: list[Row], alias: str, fetch_mode: Any = None) -> list[Any]:
        """One model instance per row, from the database `alias`, with its
        `select_related()` objects cached on it."""

    def tuples(self, rows: list[Row]) -> list[tuple[Any, ...]]:
        """One tuple of converted values per row."""

    def dicts(self, rows: list[Row]) -> list[dict[str, Any]]:
        """One dict of converted values per row, keyed by `names`."""

    @property
    def column_count(self) -> int:
        """How many columns each row must have."""

class RelatedModel:
    """A model `select_related()` adds to each instance a `ResultBuilder`
    makes, like the author of a book: where its columns are in the row, and
    Django's functions that cache it on its parent (and the parent on it)."""

    def __init__(
        self,
        model: type[Any],
        first_column: int,
        end_column: int,
        primary_key_column: int,
        set_on_parent: Callable[[Any, Any], Any],
        set_on_related: Callable[[Any, Any], Any],
        related: Sequence[RelatedModel] = (),
    ) -> None:
        """Its columns are `row[first_column:end_column]`; a `NULL` at
        `primary_key_column` means no related object. `related`: the models
        `select_related()` adds to this one in turn."""

# The exceptions follow PEP 249, like Python's `sqlite3`.

class Error(Exception):
    """Base class for all djox errors."""

class InterfaceError(Error):
    """A problem with djox itself rather than with the database."""

class DatabaseClosedError(InterfaceError):
    """The database or connection was closed."""

class DatabaseError(Error):
    """Base class for errors reported by the database."""

class DataError(DatabaseError):
    """A value doesn't fit its column."""

class OperationalError(DatabaseError):
    """The database could not run the statement: a syntax error, a missing table, a lock timeout..."""

class IntegrityError(DatabaseError):
    """A constraint failed: a duplicate unique value, a missing foreign key row..."""

class InternalError(DatabaseError):
    """The database's own state is broken."""

class ProgrammingError(DatabaseError):
    """The caller's mistake: a wrong number of parameters, a statement that returns rows sent to execute()..."""

class NotSupportedError(DatabaseError):
    """The database doesn't support this."""
