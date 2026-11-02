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

A small shop app ([`web/app.py`](web/app.py)): 100 authors, 10,000 books.

- **Endpoints**, each written three ways (plain sync views, async views, a DRF `ModelViewSet`):
  - get one book (random id)
  - list a page of 20 books with their authors: a `count()` and a `select_related()` join (random page)
  - update a book (`PATCH`), create a book (`POST`)
- **Stacks**: the same code. djox only changes settings: its `ENGINE`, and `"djox"` in `INSTALLED_APPS`.
- **Server**: [granian](https://github.com/emmett-framework/granian), one worker process, one thread. WSGI for sync
  views and DRF, ASGI for async views. No middleware.
- **Connections**: kept between requests under WSGI. Under ASGI, `CONN_MAX_AGE = 0`, as Django's docs advise. On
  Postgres, Django's own connection pool (`"pool": True`).
- **Load**: [oha](https://github.com/hatoo/oha), 32 connections, 2 s warm-up, then 5 s per endpoint. Stacks take
  turns, round after round (3 on SQLite, 5 on Postgres: `--rounds 5`); each number is the median. No request
  failed.

Requests per second, higher is better. In brackets: djox against Django.

SQLite:

| | Sync views, Django | Sync views, djox | Async views, Django | Async views, djox | DRF, Django | DRF, djox |
|---|---:|---:|---:|---:|---:|---:|
| get one book | 1,629 | 2,451 (1.5x) | 168 | 416 (2.5x) | 637 | 653 (1.0x) |
| list a page, with authors | 688 | 698 (1.0x) | 76 | 235 (3.1x) | 302 | 306 (1.0x) |
| update a book | 1,924 | 2,515 (1.3x) | 173 | 401 (2.3x) | 432 | 400 (0.9x) |
| create a book | 1,654 | 1,545 (0.9x) | 171 | 332 (1.9x) | 522 | 523 (1.0x) |

Postgres:

| | Sync views, Django | Sync views, djox | Async views, Django | Async views, djox | DRF, Django | DRF, djox |
|---|---:|---:|---:|---:|---:|---:|
| get one book | 719 | 900 (1.3x) | 223 | 395 (1.8x) | 290 | 299 (1.0x) |
| list a page, with authors | 131 | 134 (1.0x) | 135 | 201 (1.5x) | 94 | 101 (1.1x) |
| update a book | 457 | 493 (1.1x) | 225 | 360 (1.6x) | 154 | 150 (1.0x) |
| create a book | 400 | 376 (0.9x) | 228 | 309 (1.4x) | 184 | 184 (1.0x) |

- **Async views: 1.4 to 3.1 times the requests.** Django runs every async query on its one worker thread;
  djox runs them on its own connections.
- **Sync views: up to 1.5 times**, on simple reads and updates (the fast lane). A page with `select_related()`
  and `create()` run Django's own code, so they match Django.
- **DRF: the same as Django.** Serializers take most of the time, and the viewset's `select_related()` queryset
  runs Django's code. `select_related()` on the fast lane is in progress
  ([#12](https://github.com/daniprol/djox/pull/12)).
- **Async views are slower than sync views on both stacks.** Django's ASGI handler hops to a thread for its
  request signals: an empty async view serves about 540 requests per second on this VM.
- **Postgres moves more between rounds** (every query is a network round trip on the same 4 cores), so it gets
  5 rounds. Differences under about 10% are noise.

## ORM calls on SQLite

The same queries through each contender, on a SQLite file with 10,000 books, each with 3 chapters
([`run.py`](run.py)). Microseconds per call, median of 5 rounds, lower is better.

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

| Contender | What runs |
|---|---|
| django sync / django async | Django's ORM on Django's SQLite `ENGINE`. Async methods run the sync ORM on Django's worker thread |
| djox sync | Django's own manager on djox's `ENGINE` |
| djox.Manager sync / djox.Manager | `djox.Manager()`: simple queries take the fast lane |
| djox safe lane | `djox.Manager()` with `DJOX_FAST_LANE = False`: Django builds everything, djox runs the SQL |
| sqlite3 / djox | Hand-written SQL on Python's `sqlite3` / on `djox.Database`. The floors |

- **The fast lane makes sync code 3 times faster**: `get()` 61 µs against 216, `count()` 95 against 283.
- **Async `aget()` is 4 times faster** than Django's (132 µs against 507), and 1,000 books 3 times.
- **`select_related()` takes the fast lane too**: 20 chapters with their book take 176 µs sync against 694, and
  293 µs async against 1,195. Django writes the join once; Rust builds both objects of each row.
- **`prefetch_related()` gains less** (1.7 ms against 2.4 ms async): the prefetching query still runs Django's code.
- **djox's `ENGINE` alone costs about nothing**: "djox sync" is within noise of Django.
- **Inserts and `atomic()` blocks run Django's own code**, so the fast lane doesn't change them.
- **A short `djox.atomic()` block is slower** than one `sync_to_async()` around a sync `atomic()`: it pays a
  thread round trip per statement. What it buys is async code inside the transaction.
- **A round trip to djox's threads costs 60 to 90 µs** on this VM ("djox" column), against 4 µs for SQLite itself.

<details>
<summary>What made the round trip faster (120 µs to 66 µs)</summary>

- **Worker threads stay registered with Python.** Registering a Rust thread with Python for each answer cost about
  45 µs. Each worker now registers once (`src/threads.rs`).
- **The reader that slept last gets the next query.** Readers wait on a stack, so one query at a time stays on one
  warm thread (`crates/djox/src/sqlite/readers.rs`). About 15 µs saved.

</details>

## ORM calls on Postgres

The same queries on Postgres ([`postgres.py`](postgres.py)), 10,000 books. Microseconds per call, lower is better.

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
