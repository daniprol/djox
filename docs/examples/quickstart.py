"""Quickstart: run SQL from async code with djox."""

import asyncio
import sys
from pathlib import Path

import djox


async def main(path: Path) -> None:
    # One writer connection and a few reader connections, each on its own thread.
    with djox.Database.sqlite(path) as database:
        await database.execute("CREATE TABLE IF NOT EXISTS book (id INTEGER PRIMARY KEY, title TEXT, pages INTEGER)")

        # Writes wait their turn on the writer: no "database is locked" errors.
        await database.execute_many(
            "INSERT INTO book (title, pages) VALUES (%s, %s)",
            [["Dune", 412], ["Emma", 474], ["Beloved", 324]],
        )

        # Reads run in parallel on the readers. `%s` placeholders, like Django.
        long_books, count = await asyncio.gather(
            database.fetch_all("SELECT title FROM book WHERE pages > %s ORDER BY title", [400]),
            database.fetch_one("SELECT count(*) FROM book"),
        )
        print(long_books)  # [('Dune',), ('Emma',)]
        print(count)  # (3,)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else "quickstart.sqlite3")))
