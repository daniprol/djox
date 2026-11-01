"""Shared test setup: Django settings and a fresh database per test.

| Folder          | What is tested                                                        |
|-----------------|-----------------------------------------------------------------------|
| `unit/`         | One rule at a time: value conversions, exception classes               |
| `integration/`  | `djox.Database` on real SQLite files: statements, concurrency, closing |
| `e2e/`          | Django models end to end: Django writes SQL, djox runs it, Django builds instances |
| `django_suite/` | Django's own ORM tests, ported, with markers (see its README)          |

The Rust unit tests live next to the code (`scripts/cargo-test.sh --workspace`).
Tests use real SQLite files in temporary folders. No mocks.
"""

import tempfile
from collections.abc import Iterator
from pathlib import Path

import django
import pytest
from django.conf import settings

import djox

# Django can be configured once per process, so its database lives in a folder made for this test session.
DJANGO_DATABASE = Path(tempfile.mkdtemp(prefix="djox-tests-")) / "django.sqlite3"

settings.configure(
    # Django only compiles SQL in these tests; its own connection never opens this file.
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": DJANGO_DATABASE}},
    INSTALLED_APPS=["tests.e2e.library"],
    USE_TZ=True,
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
)
django.setup()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[djox.Database]:
    """A new SQLite database with a `book` table, closed after the test."""
    database = djox.Database.sqlite(tmp_path / "app.sqlite3", readers=2)
    with database:
        yield database


@pytest.fixture
async def books(database: djox.Database) -> djox.Database:
    """The `database` with an empty `book` table."""
    await database.execute(
        "CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT UNIQUE NOT NULL, pages INTEGER, cover BLOB)"
    )
    return database


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """After the run, count the tests ported from Django by status."""
    counts = {"unchanged": 0, "adapted": 0, "unsupported": 0}
    for reports in terminalreporter.stats.values():
        for report in reports:
            keywords = getattr(report, "keywords", {})
            if getattr(report, "when", None) != "call" or "django_ported" not in keywords:
                continue
            if "django_unsupported" in keywords:
                counts["unsupported"] += 1
            elif "django_adapted" in keywords:
                counts["adapted"] += 1
            else:
                counts["unchanged"] += 1
    if any(counts.values()):
        summary = ", ".join(f"{count} {status}" for status, count in counts.items())
        terminalreporter.write_line(f"Django tests ported to djox: {summary}")
