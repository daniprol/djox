"""Times the same queries through Django's ORM, plain `sqlite3` and djox.

    cd benchmarks && uv run python run.py

Every contender runs on its own copy of the same SQLite file (10,000 books):
djox and Python's `sqlite3` bundle separate copies of SQLite, which must not
open the same file in one process. Every contender uses the same SQLite
settings as djox (WAL mode, `synchronous=NORMAL`), so writes compare fairly.

Contenders:

- django sync:   Django's ORM, the normal sync methods (`get`, `list(...)`, `count`).
- django async:  Django's async methods (`aget`, `async for`, `acount`), which run
                 the sync code on a worker thread.
- sqlite3:       Python's `sqlite3` with SQL written by hand. The floor: no ORM at all.
- djox:          `djox.Database` with the same hand-written SQL.
- django + djox: Django builds the SQL and the model instances, djox runs the SQL.
                 A preview of `djox.Manager` (roadmap step 3), wired by hand here.
"""

import asyncio
import datetime
import decimal
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
        }
    },
    INSTALLED_APPS=[],
    USE_TZ=True,
)
django.setup()

from django.db import connection, models  # noqa: E402  (needs the settings above)

import djox  # noqa: E402


class Book(models.Model):
    title = models.CharField(max_length=200)
    pages = models.IntegerField(db_index=True)
    price = models.DecimalField(max_digits=6, decimal_places=2)
    published = models.DateTimeField()

    objects = models.Manager()

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
    for name in ["sqlite3", "djox", "django_djox"]:
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


def make_django_sync() -> dict[str, Callable[[], object]]:
    return {
        "get one book by id": lambda: Book.objects.get(id=7),
        "fetch 20 books": lambda: list(Book.objects.filter(pages__gt=500).order_by("-pages", "id")[:20]),
        "fetch 1,000 books": lambda: list(Book.objects.order_by("id")[:1000]),
        "count": lambda: Book.objects.filter(pages__gt=500).count(),
        "insert one book": lambda: Book.objects.create(
            title="New book", pages=100, price=decimal.Decimal("9.99"), published=datetime.datetime.now(datetime.UTC)
        ),
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
    }


def make_djox() -> dict[str, Callable[[], Awaitable[object]]]:
    database = djox.Database.sqlite(FOLDER / "djox.sqlite3")
    return {
        "get one book by id": lambda: database.fetch_all(GET_ONE, [7]),
        "fetch 20 books": lambda: database.fetch_all(FETCH_20, [500]),
        "fetch 1,000 books": lambda: database.fetch_all(FETCH_1000),
        "count": lambda: database.fetch_all(COUNT, [500]),
        "insert one book": lambda: database.execute(INSERT, INSERT_VALUES),
    }


def make_django_and_djox() -> dict[str, Callable[[], Awaitable[object]]]:
    database = djox.Database.sqlite(FOLDER / "django_djox.sqlite3")

    async def fetch(queryset: models.QuerySet) -> list:
        """Django writes the SQL and builds the instances; djox runs the SQL."""
        compiler = queryset.query.get_compiler(using=queryset.db)
        sql, parameters = compiler.as_sql()
        rows = await database.fetch_all(sql, parameters)
        select, klass_info = compiler.select, compiler.klass_info
        first, last = klass_info["select_fields"][0], klass_info["select_fields"][-1] + 1
        field_names = [column[0].target.attname for column in select[first:last]]
        fetch_mode = {"fetch_mode": queryset._fetch_mode} if hasattr(queryset, "_fetch_mode") else {}
        return [
            Book.from_db(queryset.db, field_names, row[first:last], **fetch_mode)
            for row in compiler.results_iter(results=[rows])
        ]

    async def count(queryset: models.QuerySet) -> int:
        """`SELECT COUNT(*)` with the QuerySet's filters, like Django's `count()` writes it."""
        query = queryset.query.chain()
        query.clear_ordering(force=True)
        query.default_cols = False
        query.add_annotation(models.Count("*"), alias="__count")
        query.set_annotation_mask(["__count"])
        sql, parameters = query.get_compiler(using=queryset.db).as_sql()
        rows = await database.fetch_all(sql, parameters)
        return rows[0][0]

    async def get(**filters: object) -> Book:
        books = await fetch(Book.objects.filter(**filters)[:21])
        return books[0]

    async def create() -> Book:
        book = Book(
            title="New book", pages=100, price=decimal.Decimal("9.99"), published=datetime.datetime.now(datetime.UTC)
        )
        fields = [field for field in Book._meta.concrete_fields if not field.primary_key]
        values = [field.get_db_prep_save(field.pre_save(book, add=True), connection) for field in fields]
        columns = ", ".join(connection.ops.quote_name(field.column) for field in fields)
        placeholders = ", ".join(["%s"] * len(fields))
        executed = await database.execute(f'INSERT INTO "shelf_book" ({columns}) VALUES ({placeholders})', values)
        book.id = executed.last_insert_id
        return book

    return {
        "get one book by id": lambda: get(id=7),
        "fetch 20 books": lambda: fetch(Book.objects.filter(pages__gt=500).order_by("-pages", "id")[:20]),
        "fetch 1,000 books": lambda: fetch(Book.objects.order_by("id")[:1000]),
        "count": lambda: count(Book.objects.filter(pages__gt=500)),
        "insert one book": create,
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
}
CONCURRENT_TASKS = 100


def main() -> None:
    create_database()
    results: dict[str, dict[str, float]] = {}

    # Sync code must run outside the event loop (Django refuses sync queries inside it).
    for name, operations in [("django sync", make_django_sync()), ("sqlite3", make_sqlite3())]:
        results[name] = {
            scenario: time_sync(operation, REPEATS[scenario]) for scenario, operation in operations.items()
        }
        report(name, results[name])

    async def run_async_contenders() -> None:
        for name, operations in [
            ("django async", make_django_async()),
            ("djox", make_djox()),
            ("django + djox", make_django_and_djox()),
        ]:
            results[name] = {}
            for scenario, operation in operations.items():
                results[name][scenario] = await time_async(operation, REPEATS[scenario])
            concurrent = f"{CONCURRENT_TASKS} gets at once"
            results[name][concurrent] = await time_concurrent(operations["get one book by id"], CONCURRENT_TASKS)
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
        print(f"{name:>14} | {scenario:<20} | {microseconds:9.1f} us", file=sys.stderr)


def markdown_table(results: dict[str, dict[str, float]], machine: str) -> str:
    """The results as a Markdown table, for the README."""
    scenarios = [*REPEATS, f"{CONCURRENT_TASKS} gets at once"]
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
