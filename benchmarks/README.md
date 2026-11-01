# Benchmarks

The same queries through Django's ORM, plain `sqlite3` and djox, on a SQLite
file with 10,000 books.

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
| get one book by id | 187 | 247 | 58 | 7 | 605 | 436 | 214 | 89 |
| fetch 20 books | 518 | 474 | 204 | 47 | 1,088 | 831 | 371 | 139 |
| fetch 1,000 books | 8,478 | 10,469 | 4,693 | 1,314 | 12,824 | 9,844 | 4,667 | 1,187 |
| count | 293 | 248 | 87 | 105 | 775 | 668 | 237 | 142 |
| update one book | 121 | 158 | 54 | 16 | 495 | 443 | 215 | 108 |
| insert one book | 176 | 228 | 169 | 56 | 533 | 594 | 522 | 128 |
| two inserts in a transaction | 334 | 452 | 352 | 70 | 696 | 1,907 | 1,694 | n/a |
| get or create (exists) | 186 | 243 | 65 | n/a | 588 | 512 | 159 | n/a |
| one insert, or two in a transaction | n/a | n/a | n/a | n/a | 632 | 1,175 | 1,097 | n/a |
| 100 gets at once | n/a | n/a | n/a | n/a | 559 | 444 | 186 | 77 |
| 100 get or create at once | n/a | n/a | n/a | n/a | 478 | 500 | 215 | n/a |
| 100 mixed writes at once | n/a | n/a | n/a | n/a | 661 | 1,349 | 1,247 | n/a |

This VM's speed changes by about 20% from one run to the next (every column
moves together), so compare numbers within one table, not across runs.

"Two inserts in a transaction" is `atomic()` around two `create()`s. Django
has no async `atomic()`, so "django async" runs that sync function with
`sync_to_async()`; the djox contenders use `async with djox.atomic():` around
two `acreate()`s.

## What this shows

- **djox's `ENGINE` alone costs about nothing for sync code.** "djox sync"
  (Django's own manager on djox's `ENGINE`) is within this VM's noise of
  Django's own SQLite backend.
- **The fast lane makes sync code three times faster.** With `djox.Manager`,
  a sync `get()` takes 58 µs against 187 µs on Django, `count()` 87 against
  293, `update()` 54 against 121, and 1,000 books are built in half the
  time. The fast lane skips Django's query building and SQL compiler (the
  SQL of each query shape is written once, by Django), and Rust builds the
  instances and converts their dates and decimals.
- **Async code gains as much, plus no worker thread.** An async `aget()`
  takes 214 µs against 605 µs with Django's `aget()`, and 1,000 books 4.7 ms
  against 12.8 ms. What is left over the sync numbers is the round trip to
  djox's threads (below).
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

## Postgres

The same queries on Postgres, through Django's own Postgres `ENGINE`
(psycopg), djox's (`djox.backends.postgresql`), psycopg by hand, and
`djox.Database.postgres()`:

```bash
cd benchmarks && DJOX_POSTGRES_URL=postgresql://postgres:postgres@localhost/postgres \
    uv run --reinstall-package djox python postgres.py   # writes results/postgres.json
```

It creates a database `djox_benchmarks` on that server (dropping it first)
with 10,000 books. Every contender uses the same database: Postgres is a
server, so the drivers are simply different clients.

| Name | What runs |
|------|-----------|
| django sync | Django's ORM, normal sync methods, on Django's Postgres `ENGINE` |
| djox.Manager sync | The same sync methods on `djox.Manager()` and djox's `ENGINE`: still psycopg, with the fast lane |
| psycopg | psycopg's sync connection with hand-written SQL. The floor |
| django async | Django's async methods, which run the sync ORM on a worker thread |
| djox safe lane | The async methods on `djox.Manager()`, fast lane off, on djox's Rust pool |
| djox.Manager | The same, with the fast lane |
| psycopg async | One psycopg `AsyncConnection` with hand-written SQL (one statement at a time, so no "at once" rows) |
| djox | `djox.Database.postgres()` with the same hand-written SQL |

Microseconds per call, lower is better. Linux x86_64 (4-core cloud VM),
Python 3.13, Django 6.1, Postgres 16 on the same VM.

| | django sync | djox.Manager sync | psycopg | django async | djox safe lane | djox.Manager | psycopg async | djox |
|---|---|---|---|---|---|---|---|---|
| get one book by id | 511 | 275 | 103 | 917 | 497 | 258 | 127 | 142 |
| fetch 20 books | 913 | 410 | 218 | 1,478 | 840 | 375 | 162 | 209 |
| fetch 1,000 books | 5,357 | 2,685 | 2,150 | 6,350 | 6,409 | 3,467 | 1,622 | 2,064 |
| count | 1,011 | 670 | 675 | 2,338 | 996 | 761 | 587 | 840 |
| update one book | 893 | 965 | 427 | 1,318 | 764 | 763 | 475 | 379 |
| insert one book | 944 | 1,130 | 456 | 1,288 | 744 | 906 | 670 | 484 |
| two inserts in a transaction | 1,680 | 1,998 | 742 | 1,893 | 2,772 | 3,249 | 1,034 | n/a |
| get or create (exists) | 558 | 270 | n/a | 924 | 497 | 264 | n/a | n/a |
| one insert, or two in a transaction | n/a | n/a | n/a | 1,485 | 1,649 | 2,073 | n/a | n/a |
| 100 gets at once | n/a | n/a | n/a | 612 | 318 | 158 | n/a | 53 |
| 100 get or create at once | n/a | n/a | n/a | 647 | 414 | 132 | n/a | n/a |
| 100 mixed writes at once | n/a | n/a | n/a | 1,430 | 1,400 | 1,301 | n/a | n/a |

Each query is a network round trip here, so numbers move by 20 to 30% from
one run to the next; compare within one table.

- **Raw djox is as fast as psycopg's async connection, and it has a pool.**
  One query at a time, `djox.Database.postgres()` is within noise of
  psycopg's `AsyncConnection`. A hundred gets at once take 53 µs each,
  because they run on ten connections at the same time.
- **An async `aget()` is 3.5 times faster than Django's** (258 against
  917 µs), and faster than Django's sync `get()`. A hundred at once are four
  times faster than Django's, which queues every call for one worker thread.
- **The fast lane helps Django's sync code on Postgres too**: a sync `get()`
  on `djox.Manager` takes half the time, though it still runs on psycopg.
- **Transactions pay djox's thread round trips**, as on SQLite: a short
  `djox.atomic()` block is slower than one `sync_to_async()` around a sync
  `atomic()`. With many at once, they are as fast as Django's.
