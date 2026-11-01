# Benchmarks

The same queries through Django's ORM, plain `sqlite3` and djox, on a SQLite
file with 10,000 books, each with 3 chapters (for `select_related()` and
`prefetch_related()`).

```bash
cd benchmarks && uv run --reinstall-package djox python run.py   # prints the table, writes results/latest.json
```

`--reinstall-package djox` rebuilds djox from this checkout: the benchmarks
install it as a normal package, and uv doesn't notice changes to its Python files.

## Contenders

| Name | What runs |
|------|-----------|
| django sync | Django's ORM, normal sync methods (`get`, `list(...)`, `count`, `create`), on Django's own SQLite `ENGINE` |
| djox sync | The same sync methods on djox's `ENGINE` (`djox.backends.sqlite3`), with Django's own manager |
| djox.Manager sync | The same sync methods on `djox.Manager()`: simple queries take the fast lane |
| sqlite3 | Python's `sqlite3` with hand-written SQL. The floor: no ORM, no async |
| django async | Django's async methods (`aget`, `async for`, `acount`, `acreate`), which run the sync ORM on a worker thread |
| djox safe lane | The same async methods on `djox.Manager()` with the fast lane off (`DJOX_FAST_LANE = False`): Django builds the query, the SQL and the model instances, djox runs the SQL without a worker thread. Transactions use `async with djox.atomic()` |
| djox.Manager | The same, with the fast lane (the default): simple queries reuse the SQL Django wrote for their shape, and Rust builds the model instances, converting dates and decimals itself |
| djox | `djox.Database` with the same hand-written SQL as sqlite3 |

Every contender has its own copy of the file, and every one uses WAL mode and
`synchronous = NORMAL`, like djox. Each number is the median of 5 rounds.

## Results

Microseconds per call, lower is better. Linux x86_64 (4-core cloud VM),
Python 3.13, Django 6.1.

| | django sync | djox sync | djox.Manager sync | sqlite3 | django async | djox safe lane | djox.Manager | djox |
|---|---|---|---|---|---|---|---|---|
| get one book by id | 216 | 186 | 61 | 4 | 507 | 371 | 132 | 63 |
| fetch 20 books | 543 | 460 | 120 | 26 | 957 | 753 | 216 | 99 |
| fetch 1,000 books | 7,515 | 7,662 | 2,469 | 622 | 9,864 | 8,920 | 3,055 | 693 |
| count | 283 | 278 | 95 | 67 | 584 | 420 | 177 | 127 |
| update one book | 138 | 110 | 54 | 9 | 304 | 261 | 157 | 73 |
| insert one book | 159 | 149 | 167 | 45 | 331 | 346 | 365 | 104 |
| two inserts in a transaction | 325 | 318 | 305 | 51 | 535 | 1,091 | 1,247 | n/a |
| get or create (exists) | 194 | 195 | 61 | n/a | 482 | 341 | 147 | n/a |
| one insert, or two in a transaction | n/a | n/a | n/a | n/a | 481 | 871 | 926 | n/a |
| 20 chapters with their book | 694 | 802 | 176 | n/a | 1,195 | 1,006 | 293 | n/a |
| 1,000 chapters with their book | 13,441 | 13,830 | 4,901 | n/a | 15,413 | 14,336 | 5,661 | n/a |
| 20 books with their chapters | 1,884 | 1,812 | 1,303 | n/a | 2,368 | 2,340 | 1,723 | n/a |
| 100 gets at once | n/a | n/a | n/a | n/a | 426 | 331 | 142 | 58 |
| 100 get or create at once | n/a | n/a | n/a | n/a | 407 | 268 | 118 | n/a |
| 100 mixed writes at once | n/a | n/a | n/a | n/a | 460 | 779 | 773 | n/a |

This VM's speed changes by about 20% from one run to the next (every column
moves together), so compare numbers within one table, not across runs.

"20 chapters with their book" is `select_related("book")` on a filtered,
sliced query; "1,000 chapters with their book" the same without a filter.
"20 books with their chapters" is `prefetch_related("chapters")` on 20 books:
two queries.

"Two inserts in a transaction" is `atomic()` around two `create()`s. Django
has no async `atomic()`, so "django async" runs that sync function with
`sync_to_async()`; the djox contenders use `async with djox.atomic():` around
two `acreate()`s.

## What this shows

- **djox's `ENGINE` alone costs about nothing for sync code.** "djox sync"
  (Django's own manager on djox's `ENGINE`) is within this VM's noise of
  Django's own SQLite backend.
- **The fast lane makes sync code three times faster.** With `djox.Manager`,
  a sync `get()` takes 61 µs against 216 µs on Django, `count()` 95 against
  283, `update()` 54 against 138, and 1,000 books are built in a third of
  the time. The fast lane skips Django's query building and SQL compiler (the
  SQL of each query shape is written once, by Django), and Rust builds the
  instances and converts their dates and decimals.
- **Async code gains as much, plus no worker thread.** An async `aget()`
  takes 132 µs against 507 µs with Django's `aget()`, and 1,000 books 3.1 ms
  against 9.9 ms. What is left over the sync numbers is the round trip to
  djox's threads (below).
- **`select_related()` takes the fast lane too.** 20 chapters with their
  book take 176 µs in sync code against 694 µs on Django, and 293 µs async
  against 1,195 µs: Django writes the join once, and Rust builds both
  objects of each row.
- **`prefetch_related()` gains less.** Its main query takes the fast lane,
  but the prefetching query is built and run by Django's own prefetching
  (on a djox thread, for async code), so 20 books with their chapters take
  1.7 ms against 2.4 ms with Django's async ORM. Putting the prefetching
  queries on the fast lane too is a possible next step.
- **Inserts and transactions don't take the fast lane.** `acreate()` and
  `atomic()` blocks run Django's own code, so they are the same with it or
  without it.
- **A short transaction is faster as one sync function.** `djox.atomic()`
  pays a thread round trip to start the transaction, one per query, and one
  to commit: four for two inserts, against one for `sync_to_async()` around
  a sync `atomic()`. What it buys is async code inside the transaction, and
  a thread of its own, so the transaction doesn't hold up Django's worker
  thread (shared by every other Django async call).
- **A round trip to djox's threads costs about 65 to 90 µs.** That is "djox":
  raw SQL with nothing else to do, against 5 µs for SQLite itself. It is close
  to the floor of waking a thread and waking the event loop again on this VM.
  A fast-lane get spends about 80 µs on top of it, in Python.

### What made the round trip faster

A get on `djox.Database` took 120 µs before, and 66 µs now (same VM):

- **Worker threads stay registered with Python.** A Rust thread must be
  registered with Python (given a "thread state") before it can take the GIL.
  `Python::attach` registered djox's worker thread for every answer and
  unregistered it right after, which cost about 45 µs a query. Now each worker
  registers once and keeps it (`src/threads.rs`).
- **The reader that slept last gets the next query.** With a plain shared
  queue, each new query woke the reader that had been idle the longest, so the
  four readers took turns, each one waking up cold. Now readers wait on a
  stack, so queries that come one at a time stay on one warm thread
  (`crates/djox/src/sqlite/readers.rs`). That saved about 15 µs a query.

These numbers come from one small cloud VM, where waking a thread is slow.
Expect lower numbers on a laptop; the proportions are what matter.
