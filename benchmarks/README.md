# Benchmarks

The same queries through Django's ORM, plain `sqlite3` and djox, on a SQLite
file with 10,000 books.

```bash
cd benchmarks && uv run python run.py   # prints the table, writes results/latest.json
```

## Contenders

| Name | What runs |
|------|-----------|
| django sync | Django's ORM, normal sync methods (`get`, `list(...)`, `count`, `create`) |
| sqlite3 | Python's `sqlite3` with hand-written SQL. The floor: no ORM, no async |
| django async | Django's async methods (`aget`, `async for`, `acount`, `acreate`), which run the sync ORM on a worker thread |
| djox | `djox.Database` with the same hand-written SQL as sqlite3 |
| django + djox | Django builds the SQL and the model instances, djox runs the SQL. A hand-wired preview of `djox.Manager` (roadmap step 3) |

Every contender has its own copy of the file, and every one uses WAL mode and
`synchronous = NORMAL`, like djox. Each number is the median of 5 rounds.

## Results

Microseconds per call, lower is better. Linux x86_64 (4-core cloud VM),
Python 3.13, Django 6.1.

| | django sync | sqlite3 | django async | djox | django + djox |
|---|---|---|---|---|---|
| get one book by id | 378 | 11 | 854 | 207 | 650 |
| fetch 20 books | 838 | 77 | 1,437 | 299 | 1,260 |
| fetch 1,000 books | 15,267 | 1,435 | 16,681 | 1,352 | 16,368 |
| count | 418 | 160 | 967 | 341 | 707 |
| insert one book | 426 | 76 | 650 | 230 | 319 |
| 100 gets at once | n/a | n/a | 686 | 144 | 453 |

## What this shows

- **djox is the fastest async option tested here.** A lookup by id takes 207 µs against
  854 µs for Django's `aget()`, and 100 lookups at once take 144 µs each
  against 686 µs.
- **The time goes into crossing threads, not into SQLite.** `sqlite3` runs
  the same lookup in 11 µs. Any async SQLite access has to hand the work to
  another thread and back. On this machine a bare Python round trip between
  two threads costs about 107 µs before doing any work, so djox's floor here
  is near 110 µs; the rest is ours to cut.
- **Django's own Python work dominates once djox runs the SQL.** "django +
  djox" is only somewhat faster than "django async" for single rows and no
  faster for 1,000 rows: building the query, compiling the SQL and creating
  model instances in Python costs more than everything djox does. That is
  what roadmap steps 3 and 6 (the fast lane) attack.
- **Writes are cheap.** djox's single writer queue keeps inserts at about a
  quarter of a millisecond, with no lock errors under load.

These numbers come from one small cloud VM, where waking a thread is slow.
Expect lower numbers on a laptop; the proportions are what matter.
