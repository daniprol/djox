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
| get one book by id | 204 | 218 | 4 | 588 | 454 | 166 |
| fetch 20 books | 523 | 518 | 24 | 1,040 | 895 | 196 |
| fetch 1,000 books | 8,154 | 8,379 | 572 | 12,138 | 11,711 | 1,031 |
| count | 266 | 246 | 70 | 684 | 593 | 262 |
| insert one book | 229 | 162 | 52 | 450 | 476 | 185 |
| two inserts in a transaction | 326 | 312 | 65 | 712 | 1,362 | n/a |
| 100 gets at once | n/a | n/a | n/a | 555 | 348 | 113 |

"Two inserts in a transaction" is `atomic()` around two `create()`s. Django
has no async `atomic()`, so "django async" runs that sync function with
`sync_to_async()`; "djox.Manager" is `async with djox.atomic():` around two
`acreate()`s.

## What this shows

- **djox's `ENGINE` costs nothing for sync code.** "djox sync" is as fast as
  Django's own SQLite backend, often a little faster.
- **`djox.Manager` is the fastest way to run Django's async methods here,
  and the gap grows with load.** One query at a time it is 4 to 23% faster
  than Django's async methods for reads and counts, and level for inserts.
  With 100 lookups at once it takes 348 µs each against 555 µs (37% less):
  Django's async methods all queue for one shared worker thread, djox's
  queries don't.
- **A short transaction is faster as one sync function.** `djox.atomic()`
  pays a thread round trip to start the transaction, one per query, and one
  to commit: four for two inserts, against one for `sync_to_async()` around
  a sync `atomic()`. What it buys is async code inside the transaction, and
  a thread of its own, so the transaction doesn't hold up Django's worker
  thread (shared by every other Django async call). Moving whole
  transactions to djox's writer thread is part of the fast lane.
- **Each async query costs one thread round trip.** Django's async methods
  hop to a worker thread and back once per call; so does `djox.Manager`, to
  djox's reader or writer thread. On this VM a round trip costs about 150 to
  200 µs (see "djox": raw SQL with nothing else to do), which is why one
  query at a time can't be much faster yet.
- **The rest is Django's Python.** `sqlite3` runs a lookup in 7 µs; Django
  spends about 180 µs building the query, writing the SQL and creating the
  instance. `djox.Manager` keeps Django's code for all of that on purpose (the
  "safe lane": results are exactly Django's). Roadmap step 6 (the fast lane)
  moves that work to Rust for common queries.

These numbers come from one small cloud VM, where waking a thread is slow.
Expect lower numbers on a laptop; the proportions are what matter.
