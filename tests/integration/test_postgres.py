"""`djox.Database.postgres()` on a real Postgres server: statements, values,
errors, concurrency, streams, closing.

These tests run when `DJOX_POSTGRES_URL` names a server
(`postgresql://postgres:postgres@localhost/postgres`), and are skipped otherwise.
"""

import asyncio
import datetime
import os
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest

import djox

POSTGRES_URL = os.environ.get("DJOX_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="set DJOX_POSTGRES_URL to run the Postgres tests")


@pytest.fixture
async def books() -> AsyncIterator[djox.Database]:
    """The Postgres database, with an empty `book` table, closed after the test."""
    with djox.Database.postgres(POSTGRES_URL, pool_size=4) as database:
        await database.execute("DROP TABLE IF EXISTS book")
        await database.execute(
            "CREATE TABLE book (id serial PRIMARY KEY, title text UNIQUE NOT NULL, pages integer, cover bytea)"
        )
        yield database


async def test_execute_and_fetch(books: djox.Database) -> None:
    executed = await books.execute("INSERT INTO book (title, pages) VALUES (%s, %s)", ["Dune", 412])
    # Postgres has no "last insert id": Django asks for it with `RETURNING id`.
    assert (executed.rows_affected, executed.last_insert_id) == (1, 0)

    assert await books.fetch_all("SELECT id, title, pages FROM book") == [(1, "Dune", 412)]
    assert await books.fetch_one("SELECT title FROM book WHERE pages > %s", [400]) == ("Dune",)
    assert await books.fetch_one("SELECT title FROM book WHERE pages > %s", [1000]) is None
    assert await books.fetch_all("INSERT INTO book (title) VALUES (%s) RETURNING id", ["Emma"]) == [(2,)]


async def test_values_come_back_as_python_objects(books: djox.Database) -> None:
    moment = datetime.datetime(2026, 11, 1, 12, 30, 15, 250000, tzinfo=datetime.UTC)
    book_id = uuid.uuid4()
    sql = """
        SELECT %s::boolean, %s::bigint, %s::double precision, %s::numeric(6, 2), %s::text, %s::bytea,
               %s::date, %s::time, %s::timestamp, %s::timestamptz, %s::interval, %s::uuid, %s::jsonb
    """
    row = await books.fetch_one(
        sql,
        [
            True,
            2**40,
            1.5,
            Decimal("12.30"),
            "Dune",
            b"\x00\x01",
            datetime.date(2026, 11, 1),
            datetime.time(12, 30),
            datetime.datetime(2026, 11, 1, 12, 30),
            moment,
            datetime.timedelta(days=2, microseconds=5),
            book_id,
            '{"a": [1, 2]}',
        ],
    )
    assert row == (
        True,
        2**40,
        1.5,
        Decimal("12.30"),
        "Dune",
        b"\x00\x01",
        datetime.date(2026, 11, 1),
        datetime.time(12, 30),
        datetime.datetime(2026, 11, 1, 12, 30),
        moment,
        datetime.timedelta(days=2, microseconds=5),
        book_id,
        '{"a": [1, 2]}',  # JSON comes back as text, as Django asks psycopg for it
    )


async def test_arrays_come_back_as_lists(books: djox.Database) -> None:
    row = await books.fetch_one(
        "SELECT %s::integer[], %s::text[], %s::integer[], '{}'::integer[], %s::integer[][], %s::uuid[]",
        [
            "{1,2,3}",
            '{"a","b c","say \\"hi\\""}',
            "{1,NULL}",
            "{{1,2},{3,4},{5,6}}",
            "{a8098c1a-f86e-11da-bd1a-00112444be1e}",
        ],
    )
    assert row == (
        [1, 2, 3],
        ["a", "b c", 'say "hi"'],
        [1, None],
        [],
        [[1, 2], [3, 4], [5, 6]],
        [uuid.UUID("a8098c1a-f86e-11da-bd1a-00112444be1e")],
    ), "the items have the types psycopg gives them, `NULL` is `None`"
    assert await books.fetch_one("SELECT NULL::integer[]") == (None,)


async def test_a_python_list_is_sent_as_an_array(books: djox.Database) -> None:
    assert await books.fetch_one("SELECT %s::integer[] @> %s::integer[]", [[1, 2, 3], [2]]) == (True,)
    assert await books.fetch_one("SELECT %s::text[]", [["a", None, 'q"uote']]) == (["a", None, 'q"uote'],)


async def test_ranges_come_back_as_psycopg_ranges(books: djox.Database) -> None:
    from psycopg.types.range import Range

    row = await books.fetch_one(
        "SELECT int4range(1, 5), int8range(NULL, 5), numrange(1.5, 2.5, '[]'), 'empty'::int4range, "
        "daterange('2026-01-01', '2026-02-01'), tstzrange('2026-01-01 00:00+00', NULL), '(1,10]'::int4range"
    )
    assert row == (
        Range(1, 5, "[)"),
        Range(None, 5, "()"),
        Range(Decimal("1.5"), Decimal("2.5"), "[]"),
        Range(empty=True),
        Range(datetime.date(2026, 1, 1), datetime.date(2026, 2, 1), "[)"),
        Range(datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC), None, "[)"),
        Range(2, 11, "[)"),  # Postgres turns `(1,10]` into `[2,11)`, as it does for any discrete range
    )
    assert await books.fetch_all("SELECT ARRAY[int4range(1, 2), int4range(3, 4)]") == [([Range(1, 2), Range(3, 4)],)]


async def test_a_psycopg_range_is_sent_as_a_range(books: djox.Database) -> None:
    from psycopg.types.range import Range

    sent: list[Any] = [
        Range(1, 5),
        Range(None, 5, "()"),
        Range(empty=True),
        Range(datetime.date(2026, 1, 1), datetime.date(2026, 2, 1)),
    ]
    row = await books.fetch_one("SELECT %s::int4range, %s::int4range, %s::int4range, %s::daterange", sent)
    assert row == tuple(sent)
    assert await books.fetch_one("SELECT %s::int4range @> %s::integer", [Range(1, 5), 3]) == (True,)


async def test_hstore_comes_back_as_a_dict(books: djox.Database) -> None:
    await books.execute("CREATE EXTENSION IF NOT EXISTS hstore")
    row = await books.fetch_one("SELECT %s::hstore, ''::hstore, 'a=>NULL'::hstore", ['"a"=>"1", "b c"=>"d \\"e\\""'])
    assert row == ({"a": "1", "b c": 'd "e"'}, {}, {"a": None})
    # A dict is sent as an `hstore` too, like Django's `HStoreField` does.
    assert await books.fetch_one("SELECT %s::hstore -> 'k'", [{"k": "v", "n": None}]) == ("v",)


async def test_numbers_in_an_xor_filter_are_integers(books: djox.Database) -> None:
    # Django writes `a XOR b XOR c` as the sum of `CASE WHEN a THEN 1 ELSE 0 END`s.
    # Postgres can't tell the type of those `1`s and `0`s, and guesses `text`.
    one_condition = "CASE WHEN %s THEN %s ELSE %s END"
    sql = f"SELECT ({one_condition} + {one_condition} + {one_condition}) %% %s = %s"
    parameters = [True, 1, 0, True, 1, 0, True, 1, 0, 2, 1]
    assert await books.fetch_one(sql, parameters) == (True,)
    # Statements that did not need the integers declared keep working beside it.
    assert await books.fetch_one("SELECT %s::text", [5]) == ("5",)


async def test_a_null_parameter_and_column(books: djox.Database) -> None:
    await books.execute("INSERT INTO book (title, pages, cover) VALUES (%s, %s, %s)", ["Dune", None, None])
    assert await books.fetch_one("SELECT pages, cover FROM book") == (None, None)


async def test_a_percent_sign_is_written_twice(books: djox.Database) -> None:
    await books.execute("INSERT INTO book (title) VALUES ('100%% Dune')")
    assert await books.fetch_one("SELECT title FROM book WHERE title LIKE %s", ["100%"]) == ("100% Dune",)


async def test_execute_many_keeps_all_or_nothing(books: djox.Database) -> None:
    sql = "INSERT INTO book (title) VALUES (%s)"
    executed = await books.execute_many(sql, [["A"], ["B"], ("C",)])
    assert executed.rows_affected == 3

    with pytest.raises(djox.IntegrityError):
        await books.execute_many(sql, [["D"], ["A"]])  # "A" is a duplicate, so "D" isn't kept either
    assert await books.fetch_one("SELECT count(*) FROM book") == (3,)


async def test_errors_are_sorted_like_psycopg_sorts_them(books: djox.Database) -> None:
    await books.execute("INSERT INTO book (title) VALUES ('Dune')")
    with pytest.raises(djox.IntegrityError, match="duplicate key"):
        await books.execute("INSERT INTO book (title) VALUES ('Dune')")
    with pytest.raises(djox.ProgrammingError, match="does not exist"):
        await books.fetch_all("SELECT * FROM author")
    with pytest.raises(djox.ProgrammingError, match="syntax"):
        await books.fetch_all("SELEKT 1")
    with pytest.raises(djox.DataError, match="out of range"):
        await books.execute("INSERT INTO book (title, pages) VALUES (%s, %s)", ["Emma", 2**40])
    with pytest.raises(djox.DataError, match="division by zero"):
        await books.fetch_all("SELECT 1 / 0")


async def test_many_tasks_write_and_read_at_once(books: djox.Database) -> None:
    async def add_and_count(number: int) -> None:
        await books.execute("INSERT INTO book (title) VALUES (%s)", [f"Book {number}"])
        await books.fetch_all("SELECT count(*) FROM book")

    await asyncio.gather(*(add_and_count(number) for number in range(300)))

    assert await books.fetch_one("SELECT count(*) FROM book") == (300,)


async def test_a_slow_query_does_not_block_the_event_loop(books: djox.Database) -> None:
    ticks = 0

    async def tick_until_done(query: asyncio.Future[Any]) -> None:
        nonlocal ticks
        while not query.done():
            ticks += 1
            await asyncio.sleep(0.001)

    query = asyncio.ensure_future(books.fetch_one("SELECT pg_sleep(0.2)"))
    await tick_until_done(query)

    assert await query == (None,)
    assert ticks > 5  # the loop kept running while the server slept


async def test_the_time_zone_is_the_sessions(books: djox.Database) -> None:
    with djox.Database.postgres(POSTGRES_URL, time_zone="America/New_York") as database:
        assert await database.fetch_one("SHOW TimeZone") == ("America/New_York",)
        # A date and time without a zone is read in the session's time zone.
        aware = await database.fetch_one("SELECT '2026-11-01 12:00'::timestamptz")
        assert aware == (datetime.datetime(2026, 11, 1, 17, 0, tzinfo=datetime.UTC),)


async def test_a_stream_hands_out_its_rows_chunk_by_chunk(books: djox.Database) -> None:
    await books.execute_many("INSERT INTO book (title) VALUES (%s)", [[f"Book {number}"] for number in range(5)])
    stream = books.stream_rows("SELECT title FROM book WHERE id > %s ORDER BY id", [1], chunk_size=2)

    assert await stream.next_chunk() == [("Book 1",), ("Book 2",)]
    # Other statements run while the stream waits, half read.
    await asyncio.wait_for(books.execute("INSERT INTO book (title) VALUES ('New')"), timeout=5)
    assert await stream.next_chunk() == [("Book 3",), ("Book 4",)]
    assert await stream.next_chunk() == []  # fewer than 2: the end


async def test_a_stream_refuses_statements_that_write(books: djox.Database) -> None:
    stream = books.stream_rows("INSERT INTO book (title) VALUES ('Dune') RETURNING id")
    with pytest.raises(djox.ProgrammingError, match="only queries that read"):
        await stream.next_chunk()
    assert await books.fetch_one("SELECT count(*) FROM book") == (0,)


async def test_closing() -> None:
    database = djox.Database.postgres(POSTGRES_URL)
    assert database.backend == "postgres"
    assert "postgres" in repr(database)

    sent = database.fetch_one("SELECT 1")  # sent before closing, so it still runs
    database.close()

    assert await sent == (1,)
    assert database.closed
    with pytest.raises(djox.DatabaseClosedError):
        await database.fetch_one("SELECT 1")
    database.close()  # closing twice does nothing


def test_repr_never_shows_the_password() -> None:
    database = djox.Database.postgres(POSTGRES_URL)
    with database:
        assert "localhost" in repr(database)
        assert ":postgres@" not in repr(database)


def test_settings_are_checked() -> None:
    with pytest.raises(ValueError, match="at least one connection"):
        djox.Database.postgres(POSTGRES_URL, pool_size=0)
    with pytest.raises(ValueError, match="connection settings"):
        djox.Database.postgres("postgresql://[not a host")


def test_a_server_that_cant_be_reached_fails_at_once() -> None:
    with pytest.raises(djox.OperationalError):
        djox.Database.postgres("postgresql://postgres@localhost:1/postgres?connect_timeout=2")
