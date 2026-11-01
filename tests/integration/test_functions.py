"""Python functions and aggregates that SQL can call, on a `djox.Connection`
and on every connection of a `djox.Database`."""

from collections.abc import Iterator
from pathlib import Path

import pytest

import djox


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[djox.Connection]:
    connection = djox.Connection.sqlite(tmp_path / "app.sqlite3")
    yield connection
    connection.close()


class Product:
    """Multiplies its arguments together, like `SUM` but with `*`."""

    def __init__(self) -> None:
        self.result = 1

    def step(self, value: int) -> None:
        self.result *= value

    def finalize(self) -> int:
        return self.result


def test_create_function(connection: djox.Connection) -> None:
    connection.create_function("shout", 1, str.upper, deterministic=True)
    connection.create_function("add_up", -1, lambda *numbers: sum(numbers))

    assert connection.execute("SELECT shout('dune'), add_up(1, 2, 3), add_up()").fetchone() == ("DUNE", 6, 0)


def test_functions_receive_and_return_sqlite_values(connection: djox.Connection) -> None:
    connection.create_function("kind", 1, lambda value: type(value).__name__)
    row = connection.execute("SELECT kind(NULL), kind(1), kind(1.5), kind('a'), kind(x'00')").fetchone()
    assert row == ("NoneType", "int", "float", "str", "bytes")


def test_a_function_that_raises_fails_the_statement(connection: djox.Connection) -> None:
    def broken() -> None:
        raise ValueError("it broke")

    connection.create_function("broken", 0, broken)
    with pytest.raises(djox.OperationalError, match=r"user-defined function raised exception.*it broke"):
        connection.execute("SELECT broken()")


def test_create_aggregate(connection: djox.Connection) -> None:
    connection.create_aggregate("product", 1, Product)
    connection.execute("CREATE TABLE number (value INTEGER, odd BOOLEAN)")
    connection.executemany("INSERT INTO number VALUES (%s, %s)", [[2, 0], [3, 1], [4, 0], [5, 1]])

    rows = connection.execute("SELECT odd, product(value) FROM number GROUP BY odd ORDER BY odd").fetchall()
    assert rows == [(0, 8), (1, 15)]
    assert connection.execute("SELECT product(value) FROM number WHERE value > 10").fetchone() == (None,)


async def test_prepare_runs_on_every_connection_of_a_database(tmp_path: Path) -> None:
    prepared: list[djox.Connection] = []

    def prepare(connection: djox.Connection) -> None:
        connection.create_function("answer", 0, lambda: 42)
        prepared.append(connection)

    with djox.Database.sqlite(tmp_path / "app.sqlite3", readers=2, prepare=prepare) as database:
        assert len(prepared) == 3  # the writer and two readers
        assert await database.fetch_one("SELECT answer()") == (42,)
        assert await database.fetch_all("SELECT answer() AS answer") == [(42,)]

    # The connections were only lent to `prepare`.
    assert all(connection.closed for connection in prepared)
    with pytest.raises(djox.DatabaseClosedError):
        prepared[0].execute("SELECT 1")


def test_an_exception_in_prepare_is_raised_as_it_is(tmp_path: Path) -> None:
    def prepare(connection: djox.Connection) -> None:
        raise LookupError("no such setting")

    with pytest.raises(LookupError, match="no such setting"):
        djox.Database.sqlite(tmp_path / "app.sqlite3", prepare=prepare)
