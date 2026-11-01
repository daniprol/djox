"""`Database.stream_rows()`: the rows of a query, a chunk at a time."""

import asyncio
from pathlib import Path

import pytest

import djox


async def add_books(database: djox.Database, count: int) -> None:
    await database.execute_many("INSERT INTO book (title) VALUES (%s)", [[f"Book {number}"] for number in range(count)])


async def chunk_sizes(stream: djox.RowStream) -> list[int]:
    """The size of each chunk, until the last one (shorter than `chunk_size`)."""
    sizes = []
    while True:
        rows = await stream.next_chunk()
        sizes.append(len(rows))
        if len(rows) < stream.chunk_size:
            return sizes


async def test_a_stream_hands_out_its_rows_chunk_by_chunk(books: djox.Database) -> None:
    await add_books(books, 5)
    stream = books.stream_rows("SELECT title FROM book WHERE id > %s ORDER BY id", [1], chunk_size=2)

    assert await stream.next_chunk() == [("Book 1",), ("Book 2",)]
    assert await stream.next_chunk() == [("Book 3",), ("Book 4",)]
    assert await stream.next_chunk() == []  # fewer than 2: the end
    with pytest.raises(djox.DatabaseClosedError):
        await stream.next_chunk()  # nothing more to ask for
    assert repr(stream) == "RowStream(chunk_size=2)"


async def test_the_last_chunk_is_the_shorter_one(books: djox.Database) -> None:
    await add_books(books, 5)
    assert await chunk_sizes(books.stream_rows("SELECT * FROM book", chunk_size=2)) == [2, 2, 1]
    assert await chunk_sizes(books.stream_rows("SELECT * FROM book", chunk_size=5)) == [5, 0]
    assert await chunk_sizes(books.stream_rows("SELECT * FROM book WHERE id > 99")) == [0]


async def test_other_statements_run_while_a_stream_is_read(tmp_path: Path) -> None:
    # One reader only: the stream must not hold it.
    with djox.Database.sqlite(tmp_path / "app.sqlite3", readers=1) as database:
        await database.execute("CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT)")
        await add_books(database, 10)
        stream = database.stream_rows("SELECT id FROM book ORDER BY id", chunk_size=3)
        assert await stream.next_chunk() == [(1,), (2,), (3,)]

        # The stream waits, half read; the reader and the writer are free.
        await asyncio.wait_for(database.execute("INSERT INTO book (title) VALUES ('New')"), timeout=5)
        assert await asyncio.wait_for(database.fetch_one("SELECT count(*) FROM book"), timeout=5) == (11,)

        # The stream still reads the database as it was when it started.
        assert await stream.next_chunk() == [(4,), (5,), (6,)]


async def test_a_stream_stopped_early_lets_its_connection_go(books: djox.Database) -> None:
    async def checkpoint_is_blocked() -> bool:
        # A checkpoint copies the WAL file's pages into the database, but not
        # the pages newer than what a reader still reads: those stay behind.
        _, pages_in_wal, pages_copied = await books.fetch_one("PRAGMA wal_checkpoint(PASSIVE)") or (0, 0, 0)
        return pages_copied < pages_in_wal  # type: ignore[operator]  # both are integers

    await add_books(books, 10)
    stream = books.stream_rows("SELECT * FROM book", chunk_size=2)
    await stream.next_chunk()
    await books.execute("INSERT INTO book (title) VALUES ('New')")
    assert await checkpoint_is_blocked()

    del stream  # its thread sees nobody will ask again, and closes its connection
    for _ in range(500):
        if not await checkpoint_is_blocked():
            return
        await asyncio.sleep(0.01)
    pytest.fail("the stream's connection was never let go")


async def test_a_stream_uses_connections_made_by_prepare(tmp_path: Path) -> None:
    def prepare(connection: djox.Connection) -> None:
        connection.create_function("answer", 0, lambda: 42)

    with djox.Database.sqlite(tmp_path / "app.sqlite3", prepare=prepare) as database:
        assert await database.stream_rows("SELECT answer()").next_chunk() == [(42,)]


async def test_a_stream_reports_errors(books: djox.Database) -> None:
    with pytest.raises(djox.OperationalError, match="no such table"):
        await books.stream_rows("SELECT * FROM author").next_chunk()
    with pytest.raises(djox.ProgrammingError, match="only a query that reads"):
        await books.stream_rows("INSERT INTO book (title) VALUES ('Dune') RETURNING id").next_chunk()
    assert await books.fetch_one("SELECT count(*) FROM book") == (0,)  # nothing was written
    with pytest.raises(ValueError, match="chunk_size"):
        books.stream_rows("SELECT * FROM book", chunk_size=0)


async def test_a_closed_database_streams_nothing(books: djox.Database) -> None:
    books.close()
    with pytest.raises(djox.DatabaseClosedError):
        await books.stream_rows("SELECT * FROM book").next_chunk()
