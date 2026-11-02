# The fast lane

Most queries skip Django's query building and SQL compiler, and Rust builds
their results. They run two to four times faster, sync or async
([benchmarks](benchmarks.md)). Nothing to turn on.

```python
await Book.objects.aget(pk=5)
[book async for book in Book.objects.filter(pages__gt=400).order_by("-pages")[:20]]
Book.objects.filter(Q(author__name="Jane Austen") | Q(in_print=False)).exclude(pages__lt=100)
Book.objects.annotate(chapter_count=Count("chapters")).filter(chapter_count__gt=10)
await Book.objects.filter(pages__gt=500).aaggregate(total=Sum("pages"))
await Book.objects.filter(pk=5).aupdate(pages=F("pages") + 1)
await Book.objects.acreate(title="Dune", pages=412, author=author)
```

## What takes the fast lane

A query made of these calls, in any order Django accepts:

- **`filter()` and `exclude()`**, `Q` objects included (`|`, `&`, `~`), on
  the model's fields or through any relation (`author__name`,
  `books__editions__number`, a many-to-many field), with `exact`, `gt`,
  `gte`, `lt`, `lte`, `in` or `isnull`, comparing with plain values
  (numbers, text, booleans, dates, decimals, UUIDs, model instances) or with
  Django's expressions (`pages__gt=F("chapters") * 10`), and on annotations
  (`chapter_count__gt=10`);
- **`annotate()` and `alias()`** with Django's expressions: `F()`,
  arithmetic, `Value()`, aggregates (`Count`, `Sum`, `Avg`, `Max`, `Min`,
  with `filter=Q(...)` and `default=`), and Django's functions (`Lower`,
  `Coalesce`, `TruncDay`...);
- **`order_by()`** with names or expressions (`F("pages").desc(nulls_last=True)`),
  `reverse()`, `distinct()`, and slices (`[10:20]`);
- **`values()` and `values_list()`**, with names or expressions
  (`flat=True` and `named=True` included);
- **`select_related()`**, and any `prefetch_related()`;
- then read or written with one of these methods:

| Kind | Async | Sync |
|------|-------|------|
| Rows | `async for`, `aiterator()`, `aget()`, `afirst()`, `alast()` | `for`, `list()`, `iterator()`, `get()`, `first()`, `last()`, `queryset[3]` |
| Answers | `acount()`, `aexists()`, `acontains()`, `aaggregate()` | `count()`, `exists()`, `contains()`, `aggregate()` |
| Writes | `aupdate(...)`, `adelete()` | `update(...)`, `delete()` |

- `update()` takes plain values and Django's expressions (`F("pages") + 1`).
- `delete()` takes the fast lane when Django would delete with one statement
  too: no `on_delete` work and no delete signal receivers.
- Integer and UUID ids given as text (`pk="5"`, as URLs and DRF give them)
  take it too.

Everything else runs the usual way, with the same results: subqueries
(`Exists()`, `Subquery()`, `OuterRef()`), expression classes of your own,
transforms in names (`published__year`), `XOR`, `extra()`, `only()` and
`defer()`, `union()`, and models with parent models.

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

- **The SQL is Django's.** A query is noted as its chain of calls. The first
  time djox meets a chain's *shape* (the same calls, names and expressions,
  with any values), Django builds the query and writes the SQL. djox keeps
  it, with where each parameter comes from, and reuses it for that shape.
  If it can't tell where a parameter comes from, Django keeps that shape.
- **Errors come when Django raises them.** Values are prepared by their
  field when `filter()` runs, and Django checks each new kind of
  `annotate()`, `order_by()`... call once, when it is made: a wrong name
  raises right away, as in Django.
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
