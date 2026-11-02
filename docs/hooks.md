# Hooks

Hooks are your functions, called around every SQL statement on djox's
`ENGINE`: Django's sync ORM and every `djox.QuerySet` method, the fast lane
included, from sync or async code.

```python
import logging

import djox

logger = logging.getLogger(__name__)


def tag_with_service(query: djox.Query) -> djox.Query:
    return query.with_sql(f"/* service: shop */ {query.sql}")


def warn_if_slow(query: djox.Query, finished: djox.QueryFinished) -> None:
    if finished.seconds > 0.1:
        logger.warning("slow query (%.3fs): %s", finished.seconds, query.sql)


djox.add_hooks(djox.Hooks(before_query=tag_with_service, after_query=warn_if_slow))
```

Use them to **watch** statements (time, log, count them per request) or
**change** them (add a tracing comment, block writes to a read-only replica).

| Hook | When | Returns |
|------|------|---------|
| `before_query(query)` | before each statement | `None` to change nothing, or a new `djox.Query` to run instead |
| `after_query(query, finished)` | after each statement, also when it failed (`finished.error`) | `None` |

## `djox.Query`

- `alias`, `sql` (with `%s` placeholders), `parameters`, and `many` (for
  `executemany()`).
- `query.with_sql(...)` and `query.with_parameters(...)` make a changed copy.

## Rules

- **In order**: hooks run in the order they were added; each `before_query`
  sees what the previous one returned.
- **Raising in `before_query`** stops the statement; the caller gets the
  error.
- **Any object works**: `djox.add_hooks()` takes `djox.Hooks(...)` or any
  object with `before_query` and/or `after_query` methods.
  `djox.remove_hooks()` takes it away again.
- **Plain functions, not `async def`**: hooks also run inside sync queries,
  where nothing can be awaited.
- **A streamed `aiterator()` is one statement**: the hooks see it once, when
  its first chunk arrives.

## Not hooked

- Django's signals (`pre_save`, `post_delete`...) keep working as always: use
  them for model events.
- `djox.connections[alias]` ([Raw SQL](raw-sql.md)), the layer below the ORM,
  has no hooks.
