"""Times the same queries through Django's ORM, plain `sqlite3` and djox.

    cd benchmarks && uv run python run.py

Every contender runs on its own copy of the same SQLite file (10,000 books):
djox and Python's `sqlite3` bundle separate copies of SQLite, which must not
open the same file in one process. Every contender uses the same SQLite
settings as djox (WAL mode, `synchronous=NORMAL`), so writes compare fairly.

Contenders:

- django sync:   Django's ORM, the normal sync methods (`get`, `list(...)`, `count`).
- djox sync:     the same sync methods, with `"ENGINE": "djox.backends.sqlite3"`.
- djox.Manager sync: the same sync methods on `djox.Manager()`: simple queries take
                 the fast lane, like the async ones below.
- sqlite3:       Python's `sqlite3` with SQL written by hand. The floor: no ORM at all.
- django async:  Django's async methods (`aget`, `async for`, `acount`), which run
                 the sync code on a worker thread.
- djox safe lane: the same async methods on `djox.Manager()` with the fast lane
                 off (`DJOX_FAST_LANE = False`): Django builds the query, the SQL and the
                 model instances, djox runs the SQL without a worker thread.
                 Transactions use `djox.atomic()`.
- djox.Manager:  the same, with the fast lane (the default): simple queries reuse the
                 SQL Django wrote for their shape, and Rust builds the instances.
- djox:          `djox.Database` with the same hand-written SQL as sqlite3.
"""

import asyncio
import datetime
import decimal
import itertools
import json
import platform
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import django
from django.conf import settings

FOLDER = Path(tempfile.mkdtemp(prefix="djox-benchmarks-"))
BOOK_COUNT = 10_000
ROUNDS = 5  # each number is the median of this many rounds

settings.configure(
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": FOLDER / "django.sqlite3",
            "OPTIONS": {"init_command": "PRAGMA synchronous = NORMAL"},
        },
        # djox's ENGINE on its own copy of the file: Django's ENGINE uses
        # Python's `sqlite3`, and two copies of SQLite must not share a file.
        "djox": {
            "ENGINE": "djox.backends.sqlite3",
            "NAME": FOLDER / "djox_engine.sqlite3",
            "OPTIONS": {"init_command": "PRAGMA synchronous = NORMAL"},
        },
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
    """Fill Django's file with books, then copy it for the other contenders."""
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode = WAL")  # stored in the file, so every copy has it
    with connection.schema_editor() as editor:
        editor.create_model(Book)
    published = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    Book.objects.bulk_create(
        Book(title=f"Book {number}", pages=number % 1000, price=decimal.Decimal("9.99"), published=published)
        for number in range(BOOK_COUNT)
    )
    connection.close()
    for name in ["sqlite3", "djox", "djox_engine"]:
        shutil.copy(FOLDER / "django.sqlite3", FOLDER / f"{name}.sqlite3")


# ---------------------------------------------------------------- the queries

GET_ONE = 'SELECT "id", "title", "pages", "price", "published" FROM "shelf_book" WHERE "id" = %s LIMIT 21'
FETCH_20 = (
    'SELECT "id", "title", "pages", "price", "published" FROM "shelf_book" '
    'WHERE "pages" > %s ORDER BY "pages" DESC, "id" LIMIT 20'
)
FETCH_1000 = 'SELECT "id", "title", "pages", "price", "published" FROM "shelf_book" ORDER BY "id" LIMIT 1000'
COUNT = 'SELECT COUNT(*) FROM "shelf_book" WHERE "pages" > %s'
INSERT = 'INSERT INTO "shelf_book" ("title", "pages", "price", "published") VALUES (%s, %s, %s, %s)'
INSERT_VALUES = ["New book", 100, "9.99", "2020-01-01 00:00:00"]


def question_marks(sql: str) -> str:
    return sql.replace("%s", "?")


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


def make_django_sync() -> dict[str, Callable[[], object]]:
    return {
        "get one book by id": lambda: Book.objects.get(id=7),
        "fetch 20 books": lambda: list(Book.objects.filter(pages__gt=500).order_by("-pages", "id")[:20]),
        "fetch 1,000 books": lambda: list(Book.objects.order_by("id")[:1000]),
        "count": lambda: Book.objects.filter(pages__gt=500).count(),
        "insert one book": lambda: Book.objects.create(
            title="New book", pages=100, price=decimal.Decimal("9.99"), published=datetime.datetime.now(datetime.UTC)
        ),
        "two inserts in a transaction": lambda: two_inserts_in_a_transaction("default"),
        "get or create (exists)": lambda: Book.objects.get_or_create(id=7, defaults=new_book()),
    }


def make_django_async() -> dict[str, Callable[[], Awaitable[object]]]:
    async def fetch(queryset: models.QuerySet) -> list:
        return [book async for book in queryset]

    return {
        "get one book by id": lambda: Book.objects.aget(id=7),
        "fetch 20 books": lambda: fetch(Book.objects.filter(pages__gt=500).order_by("-pages", "id")[:20]),
        "fetch 1,000 books": lambda: fetch(Book.objects.order_by("id")[:1000]),
        "count": lambda: Book.objects.filter(pages__gt=500).acount(),
        "insert one book": lambda: Book.objects.acreate(
            title="New book", pages=100, price=decimal.Decimal("9.99"), published=datetime.datetime.now(datetime.UTC)
        ),
        # Django has no async `atomic()`: the usual way is a sync function on its worker thread.
        "two inserts in a transaction": sync_to_async(lambda: two_inserts_in_a_transaction("default")),
        "one insert, or two in a transaction": alternating(
            lambda: Book.objects.acreate(**new_book()),
            sync_to_async(lambda: two_inserts_in_a_transaction("default")),
        ),
        "get or create (exists)": lambda: Book.objects.aget_or_create(id=7, defaults=new_book()),
    }


def make_sqlite3() -> dict[str, Callable[[], object]]:
    database = sqlite3.connect(FOLDER / "sqlite3.sqlite3", isolation_level=None)  # autocommit, like Django
    database.execute("PRAGMA synchronous = NORMAL")
    return {
        "get one book by id": lambda: database.execute(question_marks(GET_ONE), [7]).fetchall(),
        "fetch 20 books": lambda: database.execute(question_marks(FETCH_20), [500]).fetchall(),
        "fetch 1,000 books": lambda: database.execute(question_marks(FETCH_1000)).fetchall(),
        "count": lambda: database.execute(question_marks(COUNT), [500]).fetchall(),
        "insert one book": lambda: database.execute(question_marks(INSERT), INSERT_VALUES),
        "two inserts in a transaction": lambda: two_inserts_with_sqlite3(database),
    }


def two_inserts_with_sqlite3(database: sqlite3.Connection) -> None:
    database.execute("BEGIN IMMEDIATE")
    database.execute(question_marks(INSERT), INSERT_VALUES)
    database.execute(question_marks(INSERT), INSERT_VALUES)
    database.execute("COMMIT")


def make_djox() -> dict[str, Callable[[], Awaitable[object]]]:
    database = djox.Database.sqlite(FOLDER / "djox.sqlite3")
    return {
        "get one book by id": lambda: database.fetch_all(GET_ONE, [7]),
        "fetch 20 books": lambda: database.fetch_all(FETCH_20, [500]),
        "fetch 1,000 books": lambda: database.fetch_all(FETCH_1000),
        "count": lambda: database.fetch_all(COUNT, [500]),
        "insert one book": lambda: database.execute(INSERT, INSERT_VALUES),
    }


def make_djox_sync(manager: models.Manager) -> dict[str, Callable[[], object]]:
    books = manager.db_manager("djox")
    return {
        "get one book by id": lambda: books.get(id=7),
        "fetch 20 books": lambda: list(books.filter(pages__gt=500).order_by("-pages", "id")[:20]),
        "fetch 1,000 books": lambda: list(books.order_by("id")[:1000]),
        "count": lambda: books.filter(pages__gt=500).count(),
        "insert one book": lambda: books.create(
            title="New book", pages=100, price=decimal.Decimal("9.99"), published=datetime.datetime.now(datetime.UTC)
        ),
        "two inserts in a transaction": lambda: two_inserts_in_a_transaction("djox"),
        "get or create (exists)": lambda: books.get_or_create(id=7, defaults=new_book()),
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
        "insert one book": lambda: books.acreate(
            title="New book", pages=100, price=decimal.Decimal("9.99"), published=datetime.datetime.now(datetime.UTC)
        ),
        "two inserts in a transaction": two_inserts,
        "one insert, or two in a transaction": alternating(lambda: books.acreate(**new_book()), two_inserts),
        "get or create (exists)": lambda: books.aget_or_create(id=7, defaults=new_book()),
    }


# ---------------------------------------------------------------- timing


def time_sync(operation: Callable[[], object], repeats: int) -> float:
    """Microseconds per call: the median of `ROUNDS` rounds of `repeats` calls."""
    operation()  # warm up (caches, compiled statements)
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
    "get one book by id": 2000,
    "fetch 20 books": 1000,
    "fetch 1,000 books": 50,
    "count": 1000,
    "insert one book": 500,
    "two inserts in a transaction": 300,
    "get or create (exists)": 500,
    "one insert, or two in a transaction": 300,
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
        ("django sync", make_django_sync()),
        ("djox sync", make_djox_sync(Book.objects)),
        ("djox.Manager sync", make_djox_sync(Book.djox_objects)),
        ("sqlite3", make_sqlite3()),
    ]:
        results[name] = {
            scenario: time_sync(operation, REPEATS[scenario]) for scenario, operation in operations.items()
        }
        report(name, results[name])

    async def run_async_contenders() -> None:
        for name, fast_lane, operations in [
            ("django async", True, make_django_async()),
            ("djox safe lane", False, make_djox_manager()),
            ("djox.Manager", True, make_djox_manager()),
            ("djox", True, make_djox()),
        ]:
            # Read whenever a QuerySet is made, so it can change between contenders.
            settings.DJOX_FAST_LANE = fast_lane
            results[name] = {}
            for scenario, operation in operations.items():
                results[name][scenario] = await time_async(operation, REPEATS[scenario])
            for concurrent, scenario in CONCURRENT_SCENARIOS.items():
                if scenario in operations:
                    results[name][concurrent] = await time_concurrent(operations[scenario], CONCURRENT_TASKS)
            report(name, results[name])

    asyncio.run(run_async_contenders())

    machine = (
        f"{platform.system()} {platform.machine()}, Python {platform.python_version()}, Django {django.get_version()}"
    )
    output = {"machine": machine, "book_count": BOOK_COUNT, "microseconds_per_call": results}
    (Path(__file__).parent / "results" / "latest.json").write_text(json.dumps(output, indent=2) + "\n")
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
