# Testing

Django's test tools work unchanged: `manage.py test`, `TestCase`,
`TransactionTestCase`, pytest-django, `assertNumQueries()`.

## Async code inside `TestCase`

`TestCase` wraps each test in a sync `atomic()` block, and runs async tests
with `async_to_sync()`. Async code there must see the block's writes, and its
own writes must belong to the block. So:

- **djox's queries find the open transaction** in the current context and run
  on its connection, on a worker thread.
- **Unrelated async code** (another request) doesn't see it, and keeps using
  djox's own connections.
- **`assertNumQueries()` counts them**: they are logged on the transaction's
  connection.
- **`djox.atomic()` inside a test** joins the test's transaction as a
  savepoint.

```python
from django.test import TestCase

from shop.models import Book


class BookTests(TestCase):
    async def test_long_books(self):
        await Book.objects.acreate(title="Dune", pages=412)
        self.assertEqual(await Book.objects.filter(pages__gt=400).acount(), 1)
```

## Test databases on SQLite are files

Django tests SQLite on an in-memory database, which only one connection can
see. djox has several connections, so:

- The test database is `test_db.sqlite3` next to `NAME` (or
  `djox_test_<alias>.sqlite3` in the temp folder when `NAME` is
  `":memory:"`). `TEST["NAME"]` still wins.
- It costs little: with WAL mode, a commit doesn't wait for the disk. A RAM
  disk (`TEST = {"NAME": "/dev/shm/test_db.sqlite3"}` on Linux) is only about
  10% faster.

## Good to know

- **Raw SQL in a `TestCase`**: `djox.connections[alias]` used directly doesn't
  see the test's transaction (djox's QuerySets do). Use
  `TransactionTestCase`, or `@pytest.mark.django_db(transaction=True)`.
- **pytest-asyncio tests**: inside a sync `atomic()` block run by the event
  loop's own thread (a pytest-asyncio test without `transaction=True`, or an
  `asyncio.run()` inside `atomic()`), nothing else can use the transaction's
  connection, so `djox.atomic()` raises `TransactionManagementError`. Use
  `transaction=True`, or Django's `TestCase`.
