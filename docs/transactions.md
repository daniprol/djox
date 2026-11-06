# Transactions

`djox.atomic()` is Django's `transaction.atomic()` for async code: everything
in the block is committed when it ends, or rolled back if it raises.

```python
import djox

async with djox.atomic():
    author = await Author.objects.acreate(name="Frank Herbert")
    await Book.objects.acreate(title="Dune", author=author)
```

- **Django's arguments**: `using`, `savepoint`, `durable`.
- **A decorator too**: `@djox.atomic` or `@djox.atomic(using="other")` on an
  `async def`.
- **Nested blocks are savepoints**: if one raises, only its own work is
  undone.

## `djox.on_commit()`

```python
await djox.on_commit(send_welcome_email)
```

- Runs a function once the transaction commits, like Django's
  `transaction.on_commit()`. An `async def` is awaited once the block has
  ended, so it can use the database freely.
- Never if it rolls back; right away outside a transaction.

## What joins the transaction

Everything inside the block:

- **djox's queries** run on the transaction's connection.
- **Django's own async methods** (`aget_or_create()`...) and your own
  `sync_to_async()` functions run on the transaction's thread, so sync code
  there (`transaction.set_rollback(True)`, `Model.save()`...) is in it too.
- **Row locks**: `select_for_update()` works in the block, locking the rows
  until it ends (`await Book.objects.select_for_update().aget(pk=1)`).
  On SQLite it does nothing, as in Django: the block already has the
  file's one write turn.
- **Tasks started inside the block** (`asyncio.gather()`) share it. They must
  finish before the block ends.

??? info "How it works"
    **On SQLite**, the outermost block runs its transaction on djox's own
    writer connection. `BEGIN`, the block's statements and `COMMIT` run
    right on the event loop's thread when they need no waiting (a small
    `INSERT` takes about 20 µs, a trip to another thread 80 to 100 µs).
    Anything that might take longer (a long read, a write not yet seen to be
    quick) runs on another thread instead, so the event loop never waits.
    Until the block ends, nothing else runs on the writer.

    Django joins only when the block needs it: the first time Django's sync
    code runs in the block (`aget_or_create()`, a nested block,
    `djox.on_commit()`, your `sync_to_async()` functions), the block's
    thread points its Django connection at the writer connection and starts
    Django's own `atomic()` there. Django then ends the block, as in sync code.

    **On Postgres** (and on SQLite with `transaction_mode` `DEFERRED`), the
    outermost block takes a thread from a small pool (each keeps its Django
    connection open) and runs Django's own `atomic()` there. asgiref's
    `ThreadSensitiveContext` sends the block's thread-sensitive work to that
    thread, so blocks don't queue for Django's single worker thread.

    Inside a sync transaction (Django's `TestCase`), the block joins it as a
    savepoint.

## Writes take turns

SQLite lets one connection write at a time, and `BEGIN IMMEDIATE` (djox's
default) takes the write lock when a block starts. djox writes in two ways:

- **Plain writes** (`acreate()`, `aupdate()`, a one-statement `adelete()`) run
  on djox's writer connection: right away when they are quick, or on its
  writer thread, one after the other.
- **Transactions**: `djox.atomic()` blocks run on the writer connection too;
  methods on djox's threads like `aget_or_create()` outside a block each run
  on a Django connection of their own.

Left alone, SQLite would make one of them sleep and retry (up to 100 ms per
sleep). Instead, djox has them take turns in asyncio:

- Plain writes share the turn; a transaction holds it alone.
- A waiting writer starts the moment the turn is free.
- It gives up with "database is locked" after the `timeout` option, like
  SQLite would.
- A stream of plain writes can't hold a transaction back: after a couple of
  milliseconds, new ones wait behind it.

This covers the writes of one event loop. Writes from sync Django code (other
threads) or other processes still meet SQLite's own waiting. On Postgres,
writers don't take turns: Postgres locks rows, so many transactions write at
once.

## Tips

- **Keep blocks short.** On SQLite, a block holds the write lock: don't wait
  for slow things (an HTTP call) inside one.
- **Cost on SQLite**: a block of quick statements makes no thread round trip
  at all, so it is faster than a sync function in `sync_to_async()`
  ([benchmarks](benchmarks.md)). Code that makes Django join the block (see
  above) costs two round trips more, plus one per sync call.
- **Cost on Postgres**: a block costs two thread round trips (start and end),
  plus one per query inside. For a short transaction, a sync function in
  `sync_to_async()` is still faster; `djox.atomic()` keeps the code async,
  and Django's worker thread free.
