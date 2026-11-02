# The fast lane

Simple queries skip Django's query building and SQL compiler, and Rust builds
their results. They run two to four times faster, sync or async
([benchmarks](benchmarks.md)). Nothing to turn on.

```python
await Book.objects.aget(pk=5)
[book async for book in Book.objects.filter(pages__gt=400).order_by("-pages")[:20]]
Book.objects.filter(Q(author__name="Jane Austen") | Q(in_print=False)).exclude(pages__lt=100)
await Book.objects.filter(pk=5).aupdate(pages=420)
await Book.objects.acreate(title="Dune", pages=412, author=author)
```

## What takes the fast lane

A query that is:

- **filtered** with `filter()` and `exclude()`, `Q` objects included (`|`,
  `&`, `~`), on the model's columns or through foreign keys and one-to-one
  fields (`author__name`, `book__author__name`), with `exact`, `gt`, `gte`,
  `lt`, `lte`, `in` or `isnull`, and plain values (numbers, text, booleans,
  dates, decimals, UUIDs, model instances for a foreign key);
- **ordered** by columns, here or through foreign keys
  (`order_by("-pages", "author__name")`, or `Meta.ordering`), reversed, and
  **sliced** (`[10:20]`);
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

Everything else (`F()`, `XOR`, relations to many rows like `books__title`,
transforms like `published__year`, `annotate()`, `only()`, models with
parent models) runs the usual way, with the same results.

## New objects

`create()`, `acreate()` and, on a [`djox.Model`](models.md#djoxmodel),
`asave()` of a new object insert it on the fast lane:

- **The `INSERT` is Django's.** The first new object of a model is saved
  Django's way, and djox keeps the SQL Django wrote. The next ones only
  compute their values, like Django's compiler (`auto_now` dates, a `UUID`
  primary key's default...).
- **The rest is `save()`'s**: the check of related objects, `pre_save` and
  `post_save` (sent with `asend()` from async code), the new id on the
  object.
- **Left to Django**, on a djox thread from async code: models with their own
  `save()`, parent models or `order_with_respect_to`, fields with their own
  `pre_save()` (like `FileField`), a value from the database (`db_default`),
  a new object given a primary key without `force_insert` (Django tries an
  `UPDATE` first), and, from async code, models with sync `pre_save` or
  `post_save` receivers.

## How it stays correct

- **The SQL is Django's.** The first time djox meets a query *shape* (the same
  filters, `AND`s, `OR`s and `NOT`s, ordering, slice and columns, with any
  values), Django writes the SQL. djox keeps it and reuses it for that shape.
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
