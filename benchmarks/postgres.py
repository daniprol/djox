"""Times the same queries through Django's ORM, psycopg and djox, on Postgres.

    cd benchmarks && DJOX_POSTGRES_URL=postgresql://postgres:postgres@localhost/postgres \\
        uv run --reinstall-package djox python postgres.py

Creates a database `djox_benchmarks` on that server (dropping it first if
it exists) with 10,000 books. Every contender reads and writes the same
database: Postgres is a server, so two drivers are simply two clients.

Contenders:

- django sync:    Django's ORM, the normal sync methods, on Django's own
                  Postgres `ENGINE` (psycopg).
- djox.Manager sync: the same sync methods on `djox.Manager()` and djox's
                  `ENGINE` (`djox.backends.postgresql`): still psycopg, but
                  simple queries take the fast lane.
- psycopg:        psycopg's sync connection with hand-written SQL. The floor.
- django async:   Django's async methods, which run the sync code on a worker thread.
- djox safe lane: the same async methods on `djox.Manager()` with the fast lane off.
- djox.Manager:   the same, with the fast lane (the default).
- psycopg async:  psycopg's `AsyncConnection` with hand-written SQL.
- djox:           `djox.Database.postgres()` with the same hand-written SQL.
"""

import asyncio
import datetime
import decimal
import itertools
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import django
import psycopg
from django.conf import settings

POSTGRES_URL = os.environ.get("DJOX_POSTGRES_URL", "postgresql://postgres:postgres@localhost/postgres")
DATABASE_NAME = "djox_benchmarks"
BOOK_COUNT = 10_000
ROUNDS = 5  # each number is the median of this many rounds

server = urlsplit(POSTGRES_URL)
SERVER_SETTINGS = {
    "NAME": DATABASE_NAME,
    "USER": server.username or "",
    "PASSWORD": server.password or "",
    "HOST": server.hostname or "",
    "PORT": server.port or "",
}
BENCHMARK_URL = server._replace(path=f"/{DATABASE_NAME}").geturl()

settings.configure(
    DATABASES={
        "default": {"ENGINE": "django.db.backends.postgresql", **SERVER_SETTINGS},
        "djox": {"ENGINE": "djox.backends.postgresql", **SERVER_SETTINGS},
    },
    INSTALLED_APPS=[],
    USE_TZ=True,
    DJOX_FAST_LANE=True,  # switched off for the "djox safe lane" contender
)
django.setup()

from asgiref.sync import sync_to_async  # noqa: E402
from django.db import connection, models, transaction  # noqa: E402  (needs the settings above)

import djox  # noqa: E402


class Book(models.Model):
    title = models.CharField(max_length=200)
    pages = models.IntegerField(db_index=True)
    price = models.DecimalField(max_digits=6, decimal_places=2)
    published = models.DateTimeField()

    objects = models.Manager()
    djox_objects = djox.Manager()

    class Meta:
        app_label = "shelf"


# ---------------------------------------------------------------- the database


def create_database() -> None:
    """A fresh `djox_benchmarks` database, filled with books."""
    with psycopg.connect(POSTGRES_URL, autocommit=True) as server_connection:
        server_connection.execute(f"DROP DATABASE IF EXISTS {DATABASE_NAME}")
        server_connection.execute(f"CREATE DATABASE {DATABASE_NAME}")
    with connection.schema_editor() as editor:
        editor.create_model(Book)
    published = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    Book.objects.bulk_create(
        Book(title=f"Book {number}", pages=number % 1000, price=decimal.Decimal("9.99"), published=published)
        for number in range(BOOK_COUNT)
    )
    with connection.cursor() as cursor:
        cursor.execute('VACUUM ANALYZE "shelf_book"')
    connection.close()


# ---------------------------------------------------------------- the queries

GET_ONE = 'SELECT "id", "title", "pages", "price", "published" FROM "shelf_book" WHERE "id" = %s LIMIT 21'
FETCH_20 = (
    'SELECT "id", "title", "pages", "price", "published" FROM "shelf_book" '
    'WHERE "pages" > %s ORDER BY "pages" DESC, "id" LIMIT 20'
)
FETCH_1000 = 'SELECT "id", "title", "pages", "price", "published" FROM "shelf_book" ORDER BY "id" LIMIT 1000'
COUNT = 'SELECT COUNT(*) FROM "shelf_book" WHERE "pages" > %s'
UPDATE = 'UPDATE "shelf_book" SET "pages" = %s WHERE "id" = %s'
INSERT = 'INSERT INTO "shelf_book" ("title", "pages", "price", "published") VALUES (%s, %s, %s, %s)'
INSERT_VALUES = ["New book", 100, decimal.Decimal("9.99"), datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)]


def new_book() -> dict[str, object]:
    """The fields of a book to insert."""
    return {
        "title": "New book",
        "pages": 100,
        "price": decimal.Decimal("9.99"),
        "published": datetime.datetime.now(datetime.UTC),
    }


def alternating(first: Callable[[], Any], second: Callable[[], Any]) -> Callable[[], Any]:
    """An operation that runs `first`, then `second`, then `first`... one per call."""
    calls = itertools.count()
    return lambda: first() if next(calls) % 2 == 0 else second()


def two_inserts_in_a_transaction(alias: str) -> None:
    """The sync way: Django's `atomic()` around two `create()`s."""
    with transaction.atomic(using=alias):
        Book.objects.using(alias).create(**new_book())
        Book.objects.using(alias).create(**new_book())


def make_django_sync(manager: models.Manager, alias: str) -> dict[str, Callable[[], object]]:
    books = manager.db_manager(alias)
    return {
        "get one book by id": lambda: books.get(id=7),
        "fetch 20 books": lambda: list(books.filter(pages__gt=500).order_by("-pages", "id")[:20]),
        "fetch 1,000 books": lambda: list(books.order_by("id")[:1000]),
        "count": lambda: books.filter(pages__gt=500).count(),
        "update one book": lambda: books.filter(id=7).update(pages=100),
        "insert one book": lambda: books.create(**new_book()),
        "two inserts in a transaction": lambda: two_inserts_in_a_transaction(alias),
        "get or create (exists)": lambda: books.get_or_create(id=7, defaults=new_book()),
    }


def make_psycopg() -> dict[str, Callable[[], object]]:
    database = psycopg.connect(BENCHMARK_URL, autocommit=True)  # autocommit, like Django

    def two_inserts() -> None:
        with database.transaction():
            database.execute(INSERT, INSERT_VALUES)
            database.execute(INSERT, INSERT_VALUES)

    return {
        "get one book by id": lambda: database.execute(GET_ONE, [7]).fetchall(),
        "fetch 20 books": lambda: database.execute(FETCH_20, [500]).fetchall(),
        "fetch 1,000 books": lambda: database.execute(FETCH_1000).fetchall(),
        "count": lambda: database.execute(COUNT, [500]).fetchall(),
        "update one book": lambda: database.execute(UPDATE, [100, 7]),
        "insert one book": lambda: database.execute(INSERT, INSERT_VALUES),
        "two inserts in a transaction": two_inserts,
    }


def make_django_async() -> dict[str, Callable[[], Awaitable[object]]]:
    async def fetch(queryset: models.QuerySet) -> list:
        return [book async for book in queryset]

    return {
        "get one book by id": lambda: Book.objects.aget(id=7),
        "fetch 20 books": lambda: fetch(Book.objects.filter(pages__gt=500).order_by("-pages", "id")[:20]),
        "fetch 1,000 books": lambda: fetch(Book.objects.order_by("id")[:1000]),
        "count": lambda: Book.objects.filter(pages__gt=500).acount(),
        "update one book": lambda: Book.objects.filter(id=7).aupdate(pages=100),
        "insert one book": lambda: Book.objects.acreate(**new_book()),
        # Django has no async `atomic()`: the usual way is a sync function on its worker thread.
        "two inserts in a transaction": sync_to_async(lambda: two_inserts_in_a_transaction("default")),
        "one insert, or two in a transaction": alternating(
            lambda: Book.objects.acreate(**new_book()),
            sync_to_async(lambda: two_inserts_in_a_transaction("default")),
        ),
        "get or create (exists)": lambda: Book.objects.aget_or_create(id=7, defaults=new_book()),
    }


def make_djox_manager() -> dict[str, Callable[[], Awaitable[object]]]:
    books = Book.djox_objects.db_manager("djox")

    async def two_inserts() -> None:
        async with djox.atomic(using="djox"):
            await books.acreate(**new_book())
            await books.acreate(**new_book())

    async def fetch(queryset: models.QuerySet) -> list:
        return [book async for book in queryset]

    return {
        "get one book by id": lambda: books.aget(id=7),
        "fetch 20 books": lambda: fetch(books.filter(pages__gt=500).order_by("-pages", "id")[:20]),
        "fetch 1,000 books": lambda: fetch(books.order_by("id")[:1000]),
        "count": lambda: books.filter(pages__gt=500).acount(),
        "update one book": lambda: books.filter(id=7).aupdate(pages=100),
        "insert one book": lambda: books.acreate(**new_book()),
        "two inserts in a transaction": two_inserts,
        "one insert, or two in a transaction": alternating(lambda: books.acreate(**new_book()), two_inserts),
        "get or create (exists)": lambda: books.aget_or_create(id=7, defaults=new_book()),
    }


async def make_psycopg_async() -> dict[str, Callable[[], Awaitable[object]]]:
    """One `AsyncConnection`: it runs one statement at a time, so the
    concurrent scenarios (which need a pool) are left out."""
    database = await psycopg.AsyncConnection.connect(BENCHMARK_URL, autocommit=True)

    async def fetch(sql: str, parameters: list[object] | None = None) -> list[Any]:
        cursor = await database.execute(sql, parameters)
        return await cursor.fetchall()

    async def two_inserts() -> None:
        async with database.transaction():
            await database.execute(INSERT, INSERT_VALUES)
            await database.execute(INSERT, INSERT_VALUES)

    return {
        "get one book by id": lambda: fetch(GET_ONE, [7]),
        "fetch 20 books": lambda: fetch(FETCH_20, [500]),
        "fetch 1,000 books": lambda: fetch(FETCH_1000),
        "count": lambda: fetch(COUNT, [500]),
        "update one book": lambda: database.execute(UPDATE, [100, 7]),
        "insert one book": lambda: database.execute(INSERT, INSERT_VALUES),
        "two inserts in a transaction": two_inserts,
    }


def make_djox() -> dict[str, Callable[[], Awaitable[object]]]:
    database = djox.Database.postgres(BENCHMARK_URL)
    return {
        "get one book by id": lambda: database.fetch_all(GET_ONE, [7]),
        "fetch 20 books": lambda: database.fetch_all(FETCH_20, [500]),
        "fetch 1,000 books": lambda: database.fetch_all(FETCH_1000),
        "count": lambda: database.fetch_all(COUNT, [500]),
        "update one book": lambda: database.execute(UPDATE, [100, 7]),
        "insert one book": lambda: database.execute(INSERT, INSERT_VALUES),
    }


# ---------------------------------------------------------------- timing


def time_sync(operation: Callable[[], object], repeats: int) -> float:
    """Microseconds per call: the median of `ROUNDS` rounds of `repeats` calls."""
    operation()  # warm up (caches, prepared statements)
    rounds = []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        for _ in range(repeats):
            operation()
        rounds.append((time.perf_counter() - start) / repeats * 1e6)
    return statistics.median(rounds)


async def time_async(operation: Callable[[], Awaitable[object]], repeats: int) -> float:
    """Like `time_sync`, awaiting each call."""
    await operation()
    rounds = []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        for _ in range(repeats):
            await operation()
        rounds.append((time.perf_counter() - start) / repeats * 1e6)
    return statistics.median(rounds)


async def time_concurrent(operation: Callable[[], Awaitable[object]], tasks: int) -> float:
    """Microseconds per call when `tasks` calls run at once with `asyncio.gather`."""
    await operation()
    rounds = []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        await asyncio.gather(*(operation() for _ in range(tasks)))
        rounds.append((time.perf_counter() - start) / tasks * 1e6)
    return statistics.median(rounds)


REPEATS = {
    "get one book by id": 1000,
    "fetch 20 books": 500,
    "fetch 1,000 books": 30,
    "count": 300,
    "update one book": 300,
    "insert one book": 300,
    "two inserts in a transaction": 200,
    "get or create (exists)": 300,
    "one insert, or two in a transaction": 200,
}
CONCURRENT_TASKS = 100
# Each runs `CONCURRENT_TASKS` calls of a scenario at once, with `asyncio.gather()`.
CONCURRENT_SCENARIOS = {
    f"{CONCURRENT_TASKS} gets at once": "get one book by id",
    f"{CONCURRENT_TASKS} get or create at once": "get or create (exists)",
    f"{CONCURRENT_TASKS} mixed writes at once": "one insert, or two in a transaction",
}


def main() -> None:
    create_database()
    results: dict[str, dict[str, float]] = {}

    # Sync code must run outside the event loop (Django refuses sync queries inside it).
    for name, operations in [
        ("django sync", make_django_sync(Book.objects, "default")),
        ("djox.Manager sync", make_django_sync(Book.djox_objects, "djox")),
        ("psycopg", make_psycopg()),
    ]:
        results[name] = {
            scenario: time_sync(operation, REPEATS[scenario]) for scenario, operation in operations.items()
        }
        report(name, results[name])

    async def run_async_contenders() -> None:
        for name, fast_lane, operations, concurrent_too in [
            ("django async", True, make_django_async(), True),
            ("djox safe lane", False, make_djox_manager(), True),
            ("djox.Manager", True, make_djox_manager(), True),
            ("psycopg async", True, await make_psycopg_async(), False),
            ("djox", True, make_djox(), True),
        ]:
            # Read whenever a QuerySet is made, so it can change between contenders.
            settings.DJOX_FAST_LANE = fast_lane
            results[name] = {}
            for scenario, operation in operations.items():
                results[name][scenario] = await time_async(operation, REPEATS[scenario])
            for concurrent, scenario in CONCURRENT_SCENARIOS.items():
                if concurrent_too and scenario in operations:
                    results[name][concurrent] = await time_concurrent(operations[scenario], CONCURRENT_TASKS)
            report(name, results[name])

    asyncio.run(run_async_contenders())

    machine = (
        f"{platform.system()} {platform.machine()}, Python {platform.python_version()}, Django {django.get_version()}"
    )
    output = {"machine": machine, "book_count": BOOK_COUNT, "microseconds_per_call": results}
    (Path(__file__).parent / "results" / "postgres.json").write_text(json.dumps(output, indent=2) + "\n")
    print(markdown_table(results, machine))


def report(name: str, timings: dict[str, float]) -> None:
    for scenario, microseconds in timings.items():
        print(f"{name:>17} | {scenario:<20} | {microseconds:9.1f} us", file=sys.stderr)


def markdown_table(results: dict[str, dict[str, float]], machine: str) -> str:
    """The results as a Markdown table, for the README."""
    scenarios = [*REPEATS, *CONCURRENT_SCENARIOS]
    names = list(results)
    lines = [f"Microseconds per call, lower is better. {machine}.", ""]
    lines.append("| | " + " | ".join(names) + " |")
    lines.append("|---" * (len(names) + 1) + "|")
    for scenario in scenarios:
        cells = [f"{results[name][scenario]:,.0f}" if scenario in results[name] else "n/a" for name in names]
        lines.append(f"| {scenario} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
