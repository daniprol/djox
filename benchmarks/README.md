# Benchmarks

Stock Django against djox, on the same machine and the same data.

| Benchmark | Measures | Script |
|---|---|---|
| [Web app](#web-app-requests-per-second) | Requests per second of a Django + DRF app | `web/load_test.py` |
| [ORM calls on SQLite](#orm-calls-on-sqlite) | Microseconds per ORM call | `run.py` |
| [ORM calls on Postgres](#orm-calls-on-postgres) | Microseconds per ORM call | `postgres.py` |

## Run

```bash
cd benchmarks
uv sync --reinstall-package djox     # builds djox from this checkout
uv run python run.py                 # ORM calls, SQLite (~2 min)
uv run python postgres.py            # ORM calls, Postgres (needs DJOX_POSTGRES_URL)
cargo install oha                    # the HTTP load tool, once
uv run python web/load_test.py       # web app, SQLite and Postgres (~30 min)
```

- Postgres scripts use `DJOX_POSTGRES_URL` (default `postgresql://postgres:postgres@localhost/postgres`) and
  create their own database on that server.
- Results go to `results/*.json`. Each script prints its Markdown table.
- All numbers below: Linux x86_64, 4-core cloud VM, Python 3.13, Django 6.1, Postgres 16 on the same VM.
- This VM's speed moves by about 20% between runs. Compare within one table, not across tables.

## Web app: requests per second

A small shop app ([`web/app.py`](https://github.com/daniprol/djox/blob/main/benchmarks/web/app.py)): 100 authors, 10,000 books.

- **Endpoints**, each written three ways (plain sync views, async views, a DRF `ModelViewSet`):
  - get one book (random id)
  - list a page of 20 books with their authors: a `count()` and a `select_related()` join (random page)
  - update a book (`PATCH`), create a book (`POST`)
- **Stacks**: the same code. djox only changes settings: its `ENGINE`, and `"djox"` in `INSTALLED_APPS`.
- **Server**: [granian](https://github.com/emmett-framework/granian), one worker process, one thread. WSGI for sync
  views and DRF, ASGI for async views: Django's handler on Django, djox's (`djox.asgi`) on djox. No middleware.
- **Connections**: kept between requests under WSGI. Under ASGI, `CONN_MAX_AGE = 0`, as Django's docs advise. On
  Postgres, Django's own connection pool (`"pool": True`).
- **Load**: [oha](https://github.com/hatoo/oha), 32 connections, 2 s warm-up, then 5 s per endpoint. Stacks take
  turns, round after round (3 by default, `--rounds 5` for more); each number is the median. No request
  failed.

Requests per second, higher is better. In brackets: djox against Django.

SQLite:

| | Sync views, Django | Sync views, djox | Async views, Django | Async views, djox | DRF, Django | DRF, djox |
|---|---:|---:|---:|---:|---:|---:|
| get one book | 1,875 | 3,438 (1.8x) | 200 | 1,733 (8.7x) | 809 | 1,026 (1.3x) |
| list a page, with authors | 769 | 1,665 (2.2x) | 88 | 1,161 (13.3x) | 415 | 400 (1.0x) |
| update a book | 2,402 | 3,262 (1.4x) | 217 | 997 (4.6x) | 502 | 565 (1.1x) |
| create a book | 2,049 | 2,241 (1.1x) | 216 | 861 (4.0x) | 556 | 646 (1.2x) |

Postgres:

| | Sync views, Django | Sync views, djox | Async views, Django | Async views, djox | DRF, Django | DRF, djox |
|---|---:|---:|---:|---:|---:|---:|
| get one book | 883 | 1,147 (1.3x) | 305 | 1,157 (3.8x) | 438 | 521 (1.2x) |
| list a page, with authors | 157 | 341 (2.2x) | 185 | 596 (3.2x) | 166 | 208 (1.3x) |
| update a book | 659 | 700 (1.1x) | 341 | 1,018 (3.0x) | 227 | 248 (1.1x) |
| create a book | 574 | 558 (1.0x) | 315 | 929 (2.9x) | 252 | 263 (1.0x) |

- **Async views: 2.9 to 13.3 times the requests.** Django runs every async query on its one worker thread, and
  its ASGI handler hops to threads for each request's signals. djox runs the queries on its own connections
  (small SQLite reads right on the event loop's thread), and its handler sends the signals on the loop.
- **The gap between async and sync views mostly closes.** On Postgres, djox's async views serve more requests
  than its sync views. On SQLite they serve a third to two thirds as many; Django's serve about a tenth.
- **Sync views: 1.1 to 2.2 times**, from the fast lane: reads, updates, `select_related()` pages, and `create()`.
- **DRF: up to 1.3 times.** DRF's router passes the id from the URL as text (`pk="5"`), which the fast lane
  takes since [#15](https://github.com/daniprol/djox/pull/15). DRF spends most of each request in its own code
  (serializers, permissions, rendering), so it gains less than plain views. The DRF columns come from an earlier
  run (5 rounds, on a faster moment of this VM than the other columns): compare them with each other.
- **Postgres moves more between rounds** (every query is a network round trip on the same 4 cores). Differences
  under about 10% are noise.

## ORM calls on SQLite

The same queries through each contender, on a SQLite file with 10,000 books, each with 3 chapters
([`run.py`](https://github.com/daniprol/djox/blob/main/benchmarks/run.py)). Microseconds per call, median of 5 rounds, lower is better.

| | django sync | djox sync | djox.Manager sync | sqlite3 | django async | djox safe lane | djox.Manager | djox |
|---|---|---|---|---|---|---|---|---|
| get one book by id | 218 | 214 | 64 | 4 | 517 | 245 | 51 | 6 |
| fetch 20 books | 474 | 522 | 124 | 24 | 963 | 592 | 141 | 27 |
| fetch 1,000 books | 8,355 | 7,760 | 2,753 | 588 | 9,834 | 10,881 | 2,989 | 810 |
| count | 290 | 263 | 93 | 61 | 568 | 306 | 89 | 58 |
| update one book | 119 | 130 | 58 | 10 | 378 | 323 | 193 | 115 |
| insert one book | 183 | 195 | 145 | 47 | 441 | 401 | 268 | 136 |
| two inserts in a transaction | 319 | 346 | 332 | 60 | 645 | 1,485 | 1,280 | n/a |
| get or create (exists) | 202 | 218 | 61 | n/a | 578 | 253 | 58 | n/a |
| one insert, or two in a transaction | n/a | n/a | n/a | n/a | 624 | 1,060 | 863 | n/a |
| 20 chapters with their book | 744 | 744 | 219 | n/a | 1,245 | 924 | 206 | n/a |
| 1,000 chapters with their book | 15,373 | 15,678 | 5,423 | n/a | 17,241 | 13,811 | 7,469 | n/a |
| 20 books with their chapters | 2,004 | 1,883 | 1,546 | n/a | 2,414 | 2,390 | 1,872 | n/a |
| 100 gets at once | n/a | n/a | n/a | n/a | 445 | 254 | 60 | 6 |
| 100 get or create at once | n/a | n/a | n/a | n/a | 503 | 232 | 63 | n/a |
| 100 mixed writes at once | n/a | n/a | n/a | n/a | 595 | 898 | 916 | n/a |

| Contender | What runs |
|---|---|
| django sync / django async | Django's ORM on Django's SQLite `ENGINE`. Async methods run the sync ORM on Django's worker thread |
| djox sync | Django's own manager on djox's `ENGINE` |
| djox.Manager sync / djox.Manager | `djox.Manager()`: simple queries take the fast lane |
| djox safe lane | `djox.Manager()` with `DJOX_FAST_LANE = False`: Django builds everything, djox runs the SQL |
| sqlite3 / djox | Hand-written SQL on Python's `sqlite3` / on `djox.Database`. The floors |

- **The fast lane makes sync code 3 times faster**: `get()` 64 µs against 218, `count()` 93 against 290.
- **Async `aget()` is 10 times faster** than Django's (51 µs against 517), and 1,000 books 3 times. A small read
  runs right on the calling thread ([quick reads](https://daniprol.github.io/djox/sqlite/)), within 50 µs of
  hand-written SQL.
- **`select_related()` takes the fast lane too**: 20 chapters with their book take 219 µs sync against 744, and
  206 µs async against 1,245. Django writes the join once; Rust builds both objects of each row.
- **Annotations, aggregates and `F()` take the fast lane too** (the general fast lane: any chain of calls reuses
  the SQL Django wrote once for its shape). Measured before and after it, in one run of the same VM:

  | djox.Manager, µs | sync before | sync after | async before | async after | Django async |
  |---|---|---|---|---|---|
  | 10 books with a chapter count (`annotate(Count())`) | 625 | 183 | 670 | 148 | 1,028 |
  | 10 books by chapter title (a reverse relation) | 507 | 108 | 539 | 117 | 898 |
  | sum of pages (`aggregate(Sum())`) | 356 | 171 | 465 | 173 | 764 |
  | update with `F()` | 226 | 102 | 495 | 169 | 605 |

- **`create()` takes the fast lane**: `acreate()` 268 µs against 441, `create()` 145 against 183. Django writes the
  model's `INSERT` once.
- **`prefetch_related()` gains less** (1.9 ms against 2.4 ms async): the prefetching query still runs Django's code.
- **djox's `ENGINE` alone costs about nothing**: "djox sync" is within noise of Django.
- **A short `djox.atomic()` block is slower** than one `sync_to_async()` around a sync `atomic()`: it pays a
  thread round trip per statement. What it buys is async code inside the transaction.
- **Writes, and reads too slow to run inline, take a round trip to djox's threads**: 60 to 90 µs on this VM
  (update one book: 115 µs in the "djox" column, against 10 µs for SQLite itself).

<details>
<summary>What made the round trip faster (120 µs to 66 µs)</summary>

- **Worker threads stay registered with Python.** Registering a Rust thread with Python for each answer cost about
  45 µs. Each worker now registers once (`src/threads.rs`).
- **The reader that slept last gets the next query.** Readers wait on a stack, so one query at a time stays on one
  warm thread (`crates/djox/src/sqlite/readers.rs`). About 15 µs saved.

</details>

## ORM calls on Postgres

The same queries on Postgres ([`postgres.py`](https://github.com/daniprol/djox/blob/main/benchmarks/postgres.py)), 10,000 books. Microseconds per call, lower is better.

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

- Contenders as above. psycopg is the floor; "djox" is `djox.Database.postgres()` with hand-written SQL.
- Sync code stays on psycopg (Django's Postgres backend); async code runs on djox's Rust pool.
- **Async `aget()` is 3.5 times faster** than Django's (258 µs against 917), and 100 at once 4 times faster.
- **Raw djox is as fast as psycopg's `AsyncConnection`**, and has a pool: 100 gets at once take 53 µs each.
- **The fast lane halves a sync `get()`** on Postgres too, though it still runs on psycopg.
- **Short `djox.atomic()` blocks are slower**, as on SQLite.
- Each query is a network round trip here, so numbers move by 20 to 30% between runs.
