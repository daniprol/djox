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
| djox sync | The same sync methods on djox's `ENGINE` (`djox.backends.sqlite3`) |
| sqlite3 | Python's `sqlite3` with hand-written SQL. The floor: no ORM, no async |
| django async | Django's async methods (`aget`, `async for`, `acount`, `acreate`), which run the sync ORM on a worker thread |
| djox safe lane | The same async methods on `djox.Manager()` with the fast lane off (`DJOX_FAST_LANE = False`): Django builds the query, the SQL and the model instances, djox runs the SQL without a worker thread. Transactions use `async with djox.atomic()` |
| djox.Manager | The same, with the fast lane (the default): simple queries reuse the SQL Django wrote for their shape, and Rust builds the model instances |
| djox | `djox.Database` with the same hand-written SQL as sqlite3 |

Every contender has its own copy of the file, and every one uses WAL mode and
`synchronous = NORMAL`, like djox. Each number is the median of 5 rounds.

## Results

Microseconds per call, lower is better. Linux x86_64 (4-core cloud VM),
Python 3.13, Django 6.1.

| | django sync | djox sync | sqlite3 | django async | djox safe lane | djox.Manager | djox |
|---|---|---|---|---|---|---|---|
| get one book by id | 191 | 200 | 5 | 552 | 431 | 171 | 93 |
| fetch 20 books | 491 | 491 | 26 | 1,000 | 781 | 333 | 126 |
| fetch 1,000 books | 8,213 | 9,725 | 662 | 10,613 | 10,368 | 5,776 | 697 |
| count | 298 | 277 | 64 | 671 | 514 | 201 | 149 |
| insert one book | 169 | 173 | 43 | 495 | 401 | 451 | 143 |
| two inserts in a transaction | 358 | 318 | 62 | 618 | 1,447 | 1,460 | n/a |
| get or create (exists) | 259 | 215 | n/a | 526 | 425 | 176 | n/a |
| one insert, or two in a transaction | n/a | n/a | n/a | 574 | 1,043 | 1,021 | n/a |
| 100 gets at once | n/a | n/a | n/a | 495 | 408 | 154 | 104 |
| 100 get or create at once | n/a | n/a | n/a | 497 | 365 | 188 | n/a |
| 100 mixed writes at once | n/a | n/a | n/a | 763 | 1,009 | 1,027 | n/a |

This VM's speed changes by about 20% from one run to the next (every column
moves together), so compare numbers within one table, not across runs.

"Two inserts in a transaction" is `atomic()` around two `create()`s. Django
has no async `atomic()`, so "django async" runs that sync function with
`sync_to_async()`; the djox contenders use `async with djox.atomic():` around
two `acreate()`s.

## What this shows

- **djox's `ENGINE` costs nothing for sync code.** "djox sync" is as fast as
  Django's own SQLite backend.
- **The fast lane makes simple reads two to three times faster.** A get by id
  takes 171 µs against 431 µs with Django writing every query, and 552 µs
  with Django's async `aget()`: an async get on djox is now faster than
  Django's sync `get()`. Counts, 20 books and `aget_or_create()` that finds
  its row gain as much. The fast lane skips Django's query building and SQL
  compiler (the SQL of each query shape is written once, by Django), and Rust
  builds the instances.
- **Big results gain less, for now.** For 1,000 books, most of what is left is
  Django's converters, which turn each stored date and decimal into a
  `datetime` and a `Decimal` in Python (about three quarters of the time spent
  building the instances). Doing those conversions in Rust is the next step.
- **Writes don't take the fast lane yet.** `acreate()` and transactions are
  the same with it or without it.
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
