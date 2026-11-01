"""`djox.Connection` and `djox.Cursor` behave like Python's `sqlite3`, with
Django's placeholders, on real SQLite files."""

import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import djox


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[djox.Connection]:
    """A connection to a new database with a `book` table, closed after the test."""
    connection = djox.Connection.sqlite(tmp_path / "app.sqlite3")
    connection.execute("CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT UNIQUE NOT NULL, pages INTEGER)")
    yield connection
    connection.close()


def test_execute_returns_the_cursor_with_its_rows(connection: djox.Connection) -> None:
    connection.execute("INSERT INTO book (title, pages) VALUES (%s, %s)", ["Dune", 412])
    cursor = connection.execute("SELECT id, title, pages FROM book")

    assert cursor.description == (
        ("id", None, None, None, None, None, None),
        ("title", None, None, None, None, None, None),
        ("pages", None, None, None, None, None, None),
    )
    assert cursor.fetchone() == (1, "Dune", 412)
    assert cursor.fetchone() is None


def test_fetch_methods_hand_out_each_row_once(connection: djox.Connection) -> None:
    connection.executemany("INSERT INTO book (title) VALUES (%s)", [[f"Book {number}"] for number in range(5)])
    cursor = connection.execute("SELECT title FROM book ORDER BY id")

    assert cursor.fetchone() == ("Book 0",)
    assert cursor.fetchmany(2) == [("Book 1",), ("Book 2",)]
    assert list(cursor) == [("Book 3",), ("Book 4",)]
    assert cursor.fetchall() == []


def test_rowcount_and_lastrowid(connection: djox.Connection) -> None:
    cursor = connection.cursor()
    cursor.execute("INSERT INTO book (title) VALUES (%s), (%s)", ["A", "B"])
    assert (cursor.rowcount, cursor.lastrowid) == (2, 2)

    cursor.execute("UPDATE book SET pages = 1")
    assert cursor.rowcount == 2
    cursor.execute("SELECT * FROM book")
    assert cursor.rowcount == -1
    assert cursor.description is not None

    cursor.executemany("DELETE FROM book WHERE title = %s", [["A"], ["B"], ["C"]])
    assert cursor.rowcount == 2
    assert cursor.description is None


def test_named_placeholders_take_a_mapping(connection: djox.Connection) -> None:
    connection.execute("INSERT INTO book (title, pages) VALUES (%(title)s, %(pages)s)", {"title": "Dune", "pages": 412})
    cursor = connection.execute("SELECT title FROM book WHERE pages > %(least)s OR pages = %(least)s", {"least": 400})
    assert cursor.fetchall() == [("Dune",)]

    with pytest.raises(djox.ProgrammingError, match="pages"):
        connection.execute("SELECT %(pages)s", {"title": "Dune"})


def test_sql_without_parameters_runs_as_written(connection: djox.Connection) -> None:
    # Like Django's SQLite backend: no parameters, no placeholder conversion.
    assert connection.execute("SELECT '100%%', '%s'").fetchone() == ("100%%", "%s")
    assert connection.execute("SELECT '100%%', %s", ["x"]).fetchone() == ("100%", "x")


def test_executemany_reads_a_generator_once(connection: djox.Connection) -> None:
    titles = ({"title": title} for title in ["A", "B", "C"])
    cursor = connection.executemany("INSERT INTO book (title) VALUES (%(title)s)", titles)
    assert cursor.rowcount == 3
    assert connection.executemany("INSERT INTO book (title) VALUES (%s)", []).rowcount == 0


def test_executemany_refuses_queries(connection: djox.Connection) -> None:
    with pytest.raises(djox.ProgrammingError, match="executemany"):
        connection.executemany("SELECT %s", [[1]])


def test_errors_are_sorted_like_sqlite3(connection: djox.Connection) -> None:
    connection.execute("INSERT INTO book (title) VALUES ('Dune')")
    with pytest.raises(djox.IntegrityError, match="UNIQUE"):
        connection.execute("INSERT INTO book (title) VALUES ('Dune')")
    with pytest.raises(djox.OperationalError, match="no such table"):
        connection.execute("SELECT * FROM author")
    with pytest.raises(djox.ProgrammingError):
        connection.execute("SELECT %s, %s", [1])


def test_autocommit_off_opens_a_transaction_on_the_first_write(connection: djox.Connection) -> None:
    connection.autocommit = False
    connection.execute("SELECT count(*) FROM book")
    assert not connection.in_transaction, "reads don't open a transaction"

    connection.execute("INSERT INTO book (title) VALUES ('Dune')")
    assert connection.in_transaction
    connection.rollback()
    assert connection.execute("SELECT count(*) FROM book").fetchone() == (0,)

    connection.execute("INSERT INTO book (title) VALUES ('Emma')")
    connection.commit()
    assert not connection.in_transaction
    assert connection.execute("SELECT count(*) FROM book").fetchone() == (1,)


def test_autocommit_on_commits_each_statement(connection: djox.Connection, tmp_path: Path) -> None:
    connection.execute("INSERT INTO book (title) VALUES ('Dune')")
    assert not connection.in_transaction
    other = djox.Connection.sqlite(tmp_path / "app.sqlite3")
    assert other.execute("SELECT title FROM book").fetchall() == [("Dune",)]
    other.close()


def test_closing(connection: djox.Connection, tmp_path: Path) -> None:
    cursor = connection.cursor()
    connection.close()
    assert connection.closed
    assert repr(connection) == f"Connection.sqlite({str(tmp_path / 'app.sqlite3')!r}, closed)"
    with pytest.raises(djox.DatabaseClosedError):
        cursor.execute("SELECT 1")
    connection.close()  # closing twice does nothing

    cursor.close()
    with pytest.raises(djox.ProgrammingError, match="closed cursor"):
        cursor.fetchall()


def test_in_memory_databases_work(tmp_path: Path) -> None:
    connection = djox.Connection.sqlite(":memory:")
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("memory",)


def test_the_file_is_in_wal_mode(connection: djox.Connection) -> None:
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_limits(connection: djox.Connection) -> None:
    assert connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) >= 999


def test_other_threads_run_while_sqlite_works(connection: djox.Connection) -> None:
    slow_query = """
        WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM numbers WHERE n < 3000000)
        SELECT sum(n) FROM numbers
    """
    ticks = 0
    done = threading.Event()

    def tick_until_done() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            time.sleep(0.001)

    ticker = threading.Thread(target=tick_until_done)
    ticker.start()
    assert connection.execute(slow_query).fetchone() == (4500001500000,)
    done.set()
    ticker.join()

    assert ticks > 5  # the ticker kept running: the query didn't hold the GIL


def test_checking_closed_while_a_python_function_runs_never_deadlocks(connection: djox.Connection) -> None:
    # Thread A runs a statement that calls a Python function (it holds the
    # connection and needs the GIL); thread B asks `closed` over and over
    # (it holds the GIL and needs the connection). `closed` must let go of
    # the GIL first, or both wait for each other forever.
    connection.create_function("double", 1, lambda value: value * 2)
    finished = threading.Event()

    def run_statement() -> None:
        sql = (
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 20000) "
            "SELECT sum(double(i)) FROM n"
        )
        connection.execute(sql).fetchall()
        finished.set()

    worker = threading.Thread(target=run_statement, daemon=True)
    worker.start()
    while not finished.is_set() and worker.is_alive():
        assert connection.closed is False
    worker.join(timeout=10)
    assert finished.is_set(), "the statement never finished: deadlock"
