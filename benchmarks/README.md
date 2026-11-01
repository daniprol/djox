# Benchmarks

The same queries through Django's ORM, plain `sqlite3` and djox, on a SQLite
file with 10,000 books.

```bash
cd benchmarks && uv run python run.py   # prints the table, writes results/latest.json
```

## Contenders

| Name | What runs |
|------|-----------|
| django sync | Django's ORM, normal sync methods (`get`, `list(...)`, `count`, `create`), on Django's own SQLite `ENGINE` |
| djox sync | The same sync methods on djox's `ENGINE` (`djox.backends.sqlite3`) |
| sqlite3 | Python's `sqlite3` with hand-written SQL. The floor: no ORM, no async |
| django async | Django's async methods (`aget`, `async for`, `acount`, `acreate`), which run the sync ORM on a worker thread |
| djox.Manager | The same async methods on `djox.Manager()`: Django builds the SQL and the model instances, djox runs the SQL without a worker thread. Transactions use `async with djox.atomic()` |
| djox | `djox.Database` with the same hand-written SQL as sqlite3 |

Every contender has its own copy of the file, and every one uses WAL mode and
`synchronous = NORMAL`, like djox. Each number is the median of 5 rounds.

## Results

Microseconds per call, lower is better. Linux x86_64 (4-core cloud VM),
Python 3.13, Django 6.1.

| | django sync | djox sync | sqlite3 | django async | djox.Manager | djox |
|---|---|---|---|---|---|---|
| get one book by id | 164 | 157 | 3 | 537 | 325 | 70 |
| fetch 20 books | 488 | 380 | 20 | 1,062 | 725 | 99 |
| fetch 1,000 books | 6,163 | 6,009 | 481 | 8,462 | 8,163 | 892 |
| count | 247 | 164 | 56 | 774 | 398 | 118 |
| insert one book | 139 | 154 | 41 | 335 | 346 | 122 |
| two inserts in a transaction | 268 | 341 | 42 | 619 | 1,262 | n/a |
| 100 gets at once | n/a | n/a | n/a | 466 | 262 | 75 |

"Two inserts in a transaction" is `atomic()` around two `create()`s. Django
has no async `atomic()`, so "django async" runs that sync function with
`sync_to_async()`; "djox.Manager" is `async with djox.atomic():` around two
`acreate()`s.

## What this shows

- **djox's `ENGINE` costs nothing for sync code.** "djox sync" is as fast as
  Django's own SQLite backend, often a little faster.
- **`djox.Manager` is the fastest way to run Django's async methods here.**
  One query at a time it takes 30 to 50% less time than Django's async
  methods for gets, small fetches and counts (325 µs against 537 µs for a
  get), and the same for inserts. With 100 lookups at once it takes 262 µs
  each against 466 µs: Django's async methods all queue for one shared worker
  thread, djox's queries don't.
- **A short transaction is faster as one sync function.** `djox.atomic()`
  pays a thread round trip to start the transaction, one per query, and one
  to commit: four for two inserts, against one for `sync_to_async()` around
  a sync `atomic()`. What it buys is async code inside the transaction, and
  a thread of its own, so the transaction doesn't hold up Django's worker
  thread (shared by every other Django async call). Moving whole
  transactions to djox's writer thread is part of the fast lane.
- **A round trip to djox's threads costs about 65 µs.** That is "djox": raw
  SQL with nothing else to do, against 3 µs for SQLite itself. It is close to
  the floor of waking a thread and waking the event loop again on this VM (a
  bare Python thread and `call_soon_threadsafe` take about 50 µs).
- **The rest is Django's Python.** `sqlite3` runs a lookup in 3 µs; Django
  spends about 160 µs building the query, writing the SQL and creating the
  instance. `djox.Manager` keeps Django's code for all of that on purpose (the
  "safe lane": results are exactly Django's). Roadmap step 6 (the fast lane)
  moves that work to Rust for common queries.

### What made the round trip faster

A get on `djox.Database` took 120 µs before, and 70 µs now (same VM):

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
