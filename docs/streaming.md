# Streaming

`aiterator()` reads big results a chunk at a time:

```python
async for book in Book.objects.aiterator(chunk_size=2000):
    await send(book)
```

- **Memory stays flat.** The next chunk is read when the loop has used the
  previous one, so a million rows are never all in memory.
- **Nothing waits for the stream.** It reads on a connection of its own, so
  other queries and writes go on, even while the loop is halfway through.
- **A consistent view.** It sees the database as it was when it started.
- **Prefetching works**: with `prefetch_related()`, each chunk is prefetched,
  like in Django.
- **Inside a sync transaction** (like Django's `TestCase`), the rows are read
  all at once on the transaction's connection, the only one that sees its
  writes.
- **Hooks see it once**, when its first chunk arrives ([Hooks](hooks.md)).
