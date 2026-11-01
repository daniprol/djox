"""Shared test setup: Django settings and a fresh database per test.

| Folder          | What is tested                                                        |
|-----------------|-----------------------------------------------------------------------|
| `unit/`         | One rule at a time: value conversions, exception classes               |
| `integration/`  | `djox.Database` on real SQLite files and a Postgres server: statements, concurrency, closing |
| `e2e/`          | Django end to end on djox's ENGINE: migrations, the ORM, transactions, sync and async together |
| `django_suite/` | Django's own ORM tests, ported, with markers (see its README)          |

The Rust unit tests live next to the code (`scripts/cargo-test.sh --workspace`).
Tests use real SQLite files in temporary folders, and a real Postgres
server when one is set (`DJOX_POSTGRES_URL`). No mocks.

`DJOX_TEST_DATABASE=postgres uv run pytest` runs the Django tests on
Postgres; tests of what only the SQLite backend does are marked
`sqlite_only` and skipped there.

pytest-django sets Django up with `tests/settings.py` (see `pyproject.toml`)
and creates the test database for tests marked `django_db` and for Django
`TestCase` classes.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

import djox


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


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip the tests marked `sqlite_only` when the tests run on another database."""
    from django.conf import settings

    if settings.DATABASES["default"]["ENGINE"] == "djox.backends.sqlite3":
        return
    for item in items:
        marker = item.get_closest_marker("sqlite_only")
        if marker is not None:
            reason = marker.args[0] if marker.args else "SQLite only"
            item.add_marker(pytest.mark.skip(reason=f"SQLite only: {reason}"))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """After the run, count the tests ported from Django by status (tests that
    ran; Django's own list of SQLite skips is left out)."""
    counts = {"unchanged": 0, "adapted": 0, "unsupported": 0}
    counted: set[str] = set()  # a test with subtests reports more than once
    for reports in terminalreporter.stats.values():
        for report in reports:
            keywords = getattr(report, "keywords", {})
            if getattr(report, "when", None) != "call" or "django_ported" not in keywords:
                continue
            if report.nodeid in counted:
                continue
            counted.add(report.nodeid)
            if "django_unsupported" in keywords:
                counts["unsupported"] += 1
            elif "django_adapted" in keywords:
                counts["adapted"] += 1
            else:
                counts["unchanged"] += 1
    if any(counts.values()):
        summary = ", ".join(f"{count} {status}" for status, count in counts.items())
        terminalreporter.write_line(f"Django tests ported to djox: {summary}")
