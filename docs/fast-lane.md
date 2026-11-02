# The fast lane

Simple queries skip Django's query building and SQL compiler, and Rust builds
their results. They run two to four times faster, sync or async
([benchmarks](benchmarks.md)). Nothing to turn on.

```python
await Book.objects.aget(pk=5)
[book async for book in Book.objects.filter(pages__gt=400).order_by("-pages")[:20]]
Book.objects.filter(author=author).values_list("title", flat=True)
await Book.objects.filter(pk=5).aupdate(pages=420)
```

## What takes the fast lane

A query that is:

- **filtered** with `filter(name=value)` on the model's own columns (foreign
  keys included, by instance or id), with `exact`, `gt`, `gte`, `lt`, `lte`,
  `in` or `isnull`, and plain values (numbers, text, booleans, dates,
  decimals, UUIDs);
- **ordered** by columns (`order_by("-pages", "id")`, or `Meta.ordering`),
  reversed, and **sliced** (`[10:20]`);
- optionally **narrowed** with `values(...)` or `values_list(...)`
  (`flat=True` included);
- optionally **joined** with `select_related("author", "author__publisher")`,
  and followed by any `prefetch_related()`;
- then read or written with one of these methods:

| Kind | Async | Sync |
|------|-------|------|
| Rows | `async for`, `aiterator()`, `aget()`, `afirst()`, `alast()` | `for`, `list()`, `iterator()`, `get()`, `first()`, `last()`, `queryset[3]` |
| Answers | `acount()`, `aexists()`, `acontains()` | `count()`, `exists()`, `contains()` |
| Writes | `aupdate(name=value)`, `adelete()` | `update(name=value)`, `delete()` |

- `update()` takes plain values (no `F()` expressions).
- `delete()` takes the fast lane when Django would delete with one statement
  too: no `on_delete` work and no delete signal receivers.
- Integer and UUID ids given as text (`pk="5"`, as URLs and DRF give them)
  take it too.

Everything else (`exclude()`, `Q()`, `F()`, joins like `author__name`,
`annotate()`, `only()`, models with parent models) runs the usual way, with
the same results.

## How it stays correct

- **The SQL is Django's.** The first time djox meets a query *shape* (the same
  filters, ordering, slice and columns, with any values), Django writes the
  SQL. djox keeps it and reuses it for that shape.
- **Values are prepared by their field** when `filter()` runs, so errors
  (a wrong field name or value) are raised where Django raises them.
- **Conversions match Django's**: Rust converts dates, times, decimals and
  booleans like Django's converters, and leaves any value it doesn't handle to
  Django.
- **Sync methods join the thread's Django connection**, like Django's own:
  its transaction, its query log, and `SynchronousOnlyOperation` in async
  code.

## Turn it off

```python
# settings.py
DJOX_FAST_LANE = False
```
