# Related objects

`select_related()` and `prefetch_related()` work as in Django, sync and
async:

```python
books = [book async for book in Book.objects.select_related("author")]
authors = [author async for author in Author.objects.prefetch_related("books")]
await djox.aprefetch_related_objects(books, "editions")  # on a list you already have
```

## `select_related()`

- Joins the related tables into the query, so it is one query like any other.
- Runs on djox, and on [the fast lane](fast-lane.md) when the rest of the
  query is simple.
- Rust builds each object and its related objects, and caches them on each
  other with Django's own setters.

## `prefetch_related()`

- Runs the main query (on the fast lane when it is simple), then Django's own
  `prefetch_related_objects()`, unchanged, together on one of djox's threads.
- **The prefetching query takes the fast lane** too: its `WHERE ... IN (...)` is noted in the
  recipe, so Django's SQL for it is reused and Rust builds the objects. For:
    - **a reverse foreign key** (the books of an author) when the books' queryset is a `djox.QuerySet`;
    - **a foreign key** (the author of a book) when the author's model uses djox, or the
      `Prefetch()` queryset is a `djox.QuerySet`;
    - **a many-to-many field** (the labels of a shelf), when the labels' model has a djox default
      manager and the labels' queryset has no calls (`Label.objects.all()`). With a `filter()` or
      `order_by()`, Django joins the table in its own way, so it stays Django's.

  Sliced `Prefetch()` querysets, one-to-one fields and querysets that aren't djox's stay Django's.
- One more query per relation, and one round trip from the event loop for all
  of them.
- Every kind of relation, and `Prefetch()` objects, work as in Django.
- `aiterator(chunk_size=...)` prefetches for each chunk, like Django's.
- Inside a transaction, the prefetching runs on the transaction's thread, so
  it sees its writes.

??? question "Why not await each prefetching query?"
    Django runs those queries from deep inside its own code, level after
    level, and each level needs the one before it. On a thread, all of them
    cost one round trip from the event loop; awaited one by one, each would
    cost one.
