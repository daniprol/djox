# Async queries

With `"djox"` in `INSTALLED_APPS`, every model's QuerySet is a
`djox.QuerySet` ([or only the ones you choose](models.md)). Its async methods
run on djox:

```python
book = await Book.objects.aget(title="Dune")
long_books = [book async for book in Book.objects.filter(pages__gt=400)]
count = await Book.objects.filter(author__name="Frank Herbert").acount()
```

- **Django writes the SQL and builds the instances**, so SQL and results are
  exactly Django's.
- **djox runs the SQL** on its own connections, and the event loop never
  waits for a thread.
- **Simple queries go further**: they skip Django's compiler, and Rust builds
  the instances ([the fast lane](fast-lane.md)).

## Where each method runs

| Method | Runs on |
|--------|---------|
| `async for`, `aget()`, `afirst()`, `alast()`, `aearliest()`, `alatest()` | djox |
| `aiterator(chunk_size=2000)` | djox, a chunk at a time ([Streaming](streaming.md)) |
| `acount()`, `aexists()`, `acontains()`, `aaggregate()` | djox |
| `acreate()` | djox, on the [fast lane](fast-lane.md#new-objects) (signals are sent with `asend()`); a djox thread for models with their own `save()`, or sync `pre_save`/`post_save` receivers |
| `aupdate()` | djox |
| `adelete()` | djox when Django can delete with one statement (no `on_delete` work, no delete signal receivers); else a djox thread |
| `aget_or_create()` | djox for the lookup, a djox thread to create |
| `aupdate_or_create()`, `abulk_create()`, `abulk_update()`, `ain_bulk()`, `aexplain()` | a djox thread |
| `prefetch_related()` | a djox thread ([Related objects](related-objects.md)) |
| `acreate()`, `aupdate()` on models with parent models (multi-table inheritance) | a djox thread |
| `asave()` of a new object, on a `djox.Model` | djox, like `acreate()` |
| `asave()`, `adelete()`, `arefresh_from_db()` on an instance | a djox thread ([Choose the models](models.md)) |
| related managers: `author.books.acreate()`, `pizza.toppings.aadd()`, `aremove()`, `aclear()`, `aset()`... | a djox thread (their queries, like `author.books.filter(...)`, run on djox) |

## "A djox thread"

Some methods are long Django code with several queries and a transaction
(`aget_or_create()`, `abulk_create()`...). djox runs that code unchanged, on
one of its own threads:

- **A small pool**, each thread keeping its connection open.
- **No queueing** behind each other, or behind Django's single worker thread.
- **Writes take turns** on SQLite, so twenty tasks running `aget_or_create()`
  for the same row create it once ([Writes take turns](transactions.md#writes-take-turns)).
- **Inside a transaction** they run on the transaction's thread, so they join
  it.
- **`CONN_MAX_AGE` and `CONN_HEALTH_CHECKS` work as usual**: once per request,
  a pooled thread runs Django's `close_old_connections()`, like a request
  thread. A script, with no requests, keeps its connections open.

## Errors and the query log

- Errors are Django's (`django.db.IntegrityError`...).
- With `DEBUG = True`, queries appear in `connection.queries`, like with
  Django's cursors.

## Good to know

- On a database whose `ENGINE` isn't djox's, every method is Django's own.
- Django's sync ORM still blocks the thread it runs on, as always. While
  SQLite works, other Python threads keep running.
- A custom `QuerySet` keeps its methods; djox fills in the async methods it
  didn't write itself ([Choose the models](models.md)).
