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
| djox.Manager | The same async methods on `djox.Manager()`: Django builds the SQL and the model instances, djox runs the SQL without a worker thread |
| djox | `djox.Database` with the same hand-written SQL as sqlite3 |

Every contender has its own copy of the file, and every one uses WAL mode and
`synchronous = NORMAL`, like djox. Each number is the median of 5 rounds.

## Results

Microseconds per call, lower is better. Linux x86_64 (4-core cloud VM),
Python 3.13, Django 6.1.

| | django sync | djox sync | sqlite3 | django async | djox.Manager | djox |
|---|---|---|---|---|---|---|
| get one book by id | 224 | 184 | 7 | 627 | 563 | 178 |
| fetch 20 books | 550 | 524 | 30 | 1,138 | 952 | 224 |
| fetch 1,000 books | 9,702 | 8,621 | 582 | 13,645 | 11,876 | 1,299 |
| count | 309 | 309 | 72 | 794 | 813 | 295 |
| insert one book | 176 | 168 | 55 | 598 | 589 | 220 |
| 100 gets at once | n/a | n/a | n/a | 665 | 420 | 119 |

## What this shows

- **djox's `ENGINE` costs nothing for sync code.** "djox sync" is as fast as
  Django's own SQLite backend, often a little faster.
- **`djox.Manager` is the fastest way to run Django's async methods here,
  and the gap grows with load.** One query at a time it is 10 to 16% faster
  than Django's async methods for reads, and level for counts and inserts.
  With 100 lookups at once it takes 420 µs each against 665 µs (37% less):
  Django's async methods all queue for one shared worker thread, djox's
  queries don't.
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
