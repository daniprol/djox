"""`djox.Database` on real SQLite files: statements, errors, concurrency, closing."""

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

import djox


async def test_execute_and_fetch(books: djox.Database) -> None:
    executed = await books.execute("INSERT INTO book (title, pages) VALUES (%s, %s)", ["Dune", 412])
    assert (executed.rows_affected, executed.last_insert_id) == (1, 1)
    assert repr(executed) == "Executed(rows_affected=1, last_insert_id=1)"

    assert await books.fetch_all("SELECT id, title, pages FROM book") == [(1, "Dune", 412)]
    assert await books.fetch_one("SELECT title FROM book WHERE pages > %s", [400]) == ("Dune",)
    assert await books.fetch_one("SELECT title FROM book WHERE pages > %s", [1000]) is None


async def test_fetch_all_with_columns_gives_the_column_names_as_the_query_named_them(books: djox.Database) -> None:
    await books.execute("INSERT INTO book (title, pages) VALUES (%s, %s)", ["Dune", 412])

    columns, rows = await books.fetch_all_with_columns("SELECT title AS name, pages * 2 AS double, 7 FROM book")

    assert columns == ["name", "double", "7"]
    assert rows == [("Dune", 824, 7)]


async def test_fetch_all_with_columns_names_the_columns_of_a_query_with_no_rows(books: djox.Database) -> None:
    columns, rows = await books.fetch_all_with_columns("SELECT id, title FROM book")

    assert (columns, rows) == (["id", "title"], [])


async def test_fetch_all_runs_statements_that_write(books: djox.Database) -> None:
    rows = await books.fetch_all("INSERT INTO book (title) VALUES (%s) RETURNING id, title", ["Dune"])
    assert rows == [(1, "Dune")]


async def test_execute_many_keeps_all_or_nothing(books: djox.Database) -> None:
    sql = "INSERT INTO book (title) VALUES (%s)"
    executed = await books.execute_many(sql, [["A"], ["B"], ("C",)])
    assert executed.rows_affected == 3

    with pytest.raises(djox.IntegrityError):
        await books.execute_many(sql, [["D"], ["A"]])  # "A" is a duplicate, so "D" isn't kept either
    assert await books.fetch_one("SELECT count(*) FROM book") == (3,)


async def test_errors(books: djox.Database) -> None:
    await books.execute("INSERT INTO book (title) VALUES ('Dune')")
    with pytest.raises(djox.IntegrityError, match="UNIQUE"):
        await books.execute("INSERT INTO book (title) VALUES ('Dune')")
    with pytest.raises(djox.OperationalError, match="no such table"):
        await books.fetch_all("SELECT * FROM author")
    with pytest.raises(djox.OperationalError):
        await books.fetch_all("SELEKT 1")
    with pytest.raises(djox.DatabaseError, match="fetch_all"):
        await books.execute("INSERT INTO book (title) VALUES ('Emma') RETURNING id")


async def test_many_tasks_write_and_read_at_once_without_lock_errors(books: djox.Database) -> None:
    async def add_and_count(number: int) -> None:
        await books.execute("INSERT INTO book (title) VALUES (%s)", [f"Book {number}"])
        await books.fetch_all("SELECT count(*) FROM book")

    await asyncio.gather(*(add_and_count(number) for number in range(500)))

    assert await books.fetch_one("SELECT count(*) FROM book") == (500,)


async def test_a_slow_query_does_not_block_the_event_loop(database: djox.Database) -> None:
    slow_query = """
        WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM numbers WHERE n < 3000000)
        SELECT sum(n) FROM numbers
    """
    ticks = 0

    async def tick_until_done(query: asyncio.Future[Any]) -> None:
        nonlocal ticks
        while not query.done():
            ticks += 1
            await asyncio.sleep(0.001)

    query = asyncio.ensure_future(database.fetch_one(slow_query))
    await tick_until_done(query)

    assert await query == (4500001500000,)
    assert ticks > 5  # the loop kept running while SQLite worked


async def test_a_small_read_is_answered_right_away(books: djox.Database) -> None:
    await books.execute("INSERT INTO book (title) VALUES ('Dune')")
    answer = books.fetch_one("SELECT title FROM book WHERE id = %s", [1])
    # A quick read: run on this thread, faster than a trip to a reader thread.
    assert answer.done()
    assert await answer == ("Dune",)


async def test_a_slow_read_moves_to_a_reader_after_the_quick_read_limit(database: djox.Database) -> None:
    slow_query = """
        WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM numbers WHERE n < 3000000)
        SELECT count(*) FROM numbers
    """
    started = time.perf_counter()
    answer = database.fetch_one(slow_query)
    assert time.perf_counter() - started < 0.05  # stopped after 1 ms, not run to the end here
    assert not answer.done()
    assert await answer == (3000000,)


async def test_quick_reads_can_be_turned_off(tmp_path: Path) -> None:
    with djox.Database.sqlite(tmp_path / "app.sqlite3", quick_read_limit=0) as database:
        answer = database.fetch_one("SELECT 1")
        assert not answer.done()
        assert await answer == (1,)


async def test_a_small_write_is_done_right_away(books: djox.Database) -> None:
    written = books.execute("INSERT INTO book (title) VALUES (%s)", ["Dune"])
    # A quick write: run on this thread, on the writer connection itself.
    assert written.done()
    assert (await written).rows_affected == 1
    inserted = books.fetch_all("INSERT INTO book (title) VALUES ('Emma') RETURNING id")
    assert inserted.done()
    assert await inserted == [(2,)]


async def test_quick_writes_can_be_turned_off(tmp_path: Path) -> None:
    with djox.Database.sqlite(tmp_path / "app.sqlite3", quick_write_limit=0) as database:
        await database.execute("CREATE TABLE book (title TEXT)")
        written = database.execute("INSERT INTO book (title) VALUES ('Dune')")
        assert not written.done()
        assert (await written).rows_affected == 1


async def test_a_transaction_on_the_writer_connection_holds_back_other_writes(books: djox.Database) -> None:
    connection = books.writer_connection()
    cursor = connection.cursor()
    assert cursor.execute_quickly("BEGIN IMMEDIATE", [])
    cursor.execute("INSERT INTO book (title) VALUES (%s)", ["Dune"])
    other_write = books.execute("INSERT INTO book (title) VALUES ('Emma')")
    await asyncio.sleep(0.05)
    assert not other_write.done(), "the writer waits for the transaction to end"
    assert await books.fetch_one("SELECT count(*) FROM book") == (0,), "nothing is committed yet"
    assert cursor.execute_quickly("COMMIT", [])
    await other_write
    assert await books.fetch_one("SELECT count(*) FROM book") == (2,)
    connection.close()


async def test_closing_a_writer_connection_rolls_back_its_transaction(books: djox.Database) -> None:
    connection = books.writer_connection()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO book (title) VALUES ('Never')")
    connection.close()
    assert connection.closed
    await books.execute("INSERT INTO book (title) VALUES ('Dune')")  # the writer goes on
    assert await books.fetch_all("SELECT title FROM book") == [("Dune",)]
    assert not books.closed, "the writer connection belongs to the database"


def test_only_a_writer_connection_runs_statements_quickly(tmp_path: Path) -> None:
    connection = djox.Connection.sqlite(tmp_path / "app.sqlite3")
    assert connection.cursor().execute_quickly("SELECT 1") is False
    connection.close()


async def test_the_write_ahead_log_is_still_checkpointed(books: djox.Database, tmp_path: Path) -> None:
    # A commit on the event loop's thread doesn't copy the log back into the
    # database file (that waits for the disk): the writer thread does it later.
    # 16 MB of covers, one by one: the log must not keep growing.
    cover = b"x" * 4000
    for number in range(4000):
        await books.execute("INSERT INTO book (title, cover) VALUES (%s, %s)", [f"Book {number}", cover])
    log_size = (tmp_path / "app.sqlite3-wal").stat().st_size
    assert log_size < 8_000_000


async def test_works_with_no_readers(tmp_path: Path) -> None:
    with djox.Database.sqlite(tmp_path / "app.sqlite3", readers=0) as database:
        assert await database.fetch_one("SELECT 1") == (1,)


async def test_closing(tmp_path: Path) -> None:
    database = djox.Database.sqlite(tmp_path / "app.sqlite3")
    assert database.backend == "sqlite"
    assert repr(database) == f"Database.sqlite({str(tmp_path / 'app.sqlite3')!r})"

    sent = database.fetch_one("SELECT 1")  # sent before closing, so it still runs
    database.close()

    assert await sent == (1,)
    assert database.closed
    assert repr(database).endswith(", closed)")
    with pytest.raises(djox.DatabaseClosedError):
        await database.fetch_one("SELECT 1")
    database.close()  # closing twice does nothing


def test_the_file_is_in_wal_mode(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite3"
    djox.Database.sqlite(path).close()
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone() == ("wal",)


@pytest.mark.parametrize("path", [":memory:", "file:memorydb_default?mode=memory&cache=shared"])
def test_in_memory_databases_are_refused(path: str) -> None:
    with pytest.raises(ValueError, match="needs a database file"):
        djox.Database.sqlite(path)


def test_settings_are_checked(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout"):
        djox.Database.sqlite(tmp_path / "app.sqlite3", timeout=-1)
    with pytest.raises(ValueError, match="quick_read_limit"):
        djox.Database.sqlite(tmp_path / "app.sqlite3", quick_read_limit=float("nan"))
