# Django's own ORM tests, ported to djox

Django's test suite is the best description of how its ORM must behave. As
djox takes over parts of the ORM, the matching Django tests are copied here
and run against djox.

| Folder          | From Django's `tests/`                              | What it checks on djox                       |
|-----------------|-----------------------------------------------------|----------------------------------------------|
| `backends/`     | `backends/tests.py`, `backends/sqlite/tests.py`     | Cursors, placeholders, constraints, threads, the SQLite backend |
| `transactions/` | `transactions/tests.py`                             | `atomic()`, savepoints, autocommit           |
| `db_functions/` | `db_functions/{datetime,math,text,comparison}/`     | Django's SQL functions (`django_date_extract`, `MD5`...) on djox's SQLite |
| `async/`        | `async/test_async_{queryset,model_methods,related_managers}.py` | The `a`-methods on `djox.Manager` and `djox.Model`, inside `TestCase`'s transaction |
| `basic/`        | `basic/tests.py`                                    | Creating, saving, getting and refreshing objects through `djox.QuerySet` |
| `lookup/`       | `lookup/tests.py`                                   | `filter()` lookups, `in_bulk()`, `values()`: the fast lane's notes, and Django's `Query` built from them |
| `ordering/`     | `ordering/tests.py`                                 | `order_by()`, `reverse()`, `Meta.ordering`, `first()`/`last()` and slices on `djox.QuerySet` |
| `queries/`      | `queries/` (all its test modules)                   | The QuerySet language: `Q`, joins, `exclude()`, subqueries, `union()`, `iterator()`, `bulk_update()`, `contains()`, `explain()` |
| `aggregation/`, `aggregation_regress/`, `annotations/` | the same folders | `aggregate()`, `annotate()`, `alias()` and their edge cases |
| `expressions/`, `expressions_case/` | the same folders                  | `F()`, `Value()`, `Case()`/`When()`, `Subquery()`, date and time arithmetic |
| `update/`, `update_only_fields/`, `delete/` | the same folders           | `update()`, `save(update_fields=...)`, `delete()` and cascades |
| `get_or_create/`, `bulk_create/` | the same folders                     | `get_or_create()`, `update_or_create()`, `bulk_create()` |
| `many_to_one/`, `many_to_many/`, `reverse_lookup/` | the same folders   | Related managers and lookups across relations |
| `custom_managers/`, `defer/`, `dates/`, `datetimes/`, `get_earliest_or_latest/`, `null_queries/`, `or_lookups/`, `from_db_value/` | the same folders | Custom managers and `QuerySet`s, `defer()`/`only()`, `dates()`, `datetimes()`, `earliest()`/`latest()`, `None` in filters, OR-ing querysets, field converters |
| `select_related/`, `select_related_onetoone/`, `select_related_regress/` | `select_related*/tests.py` | `select_related()` on `djox.QuerySet`: the fast lane's joins, and the related objects Rust builds |
| `prefetch_related/` | `prefetch_related/{tests,test_prefetch_related_objects,test_uuid}.py` | `prefetch_related()` on `djox.QuerySet`, after a main query on the fast lane |

The models of `basic/`, `lookup/`, `ordering/` and of the query apps after
them in the table are left as Django wrote them: `DJOX_MODELS` (in
`tests/settings.py`) switches them to djox, so their sync tests run on
`djox.QuerySet`, with the fast lane's notes. (Most of the query tests use more
than the fast lane can note, so they check that djox steps aside correctly.
The async paths of `select_related()` and `prefetch_related()` are tested in
`tests/e2e/test_related_objects.py`.)

Like Django's own test settings, `tests/settings.py` has a second database,
`"other"` (for tests with `databases = {"default", "other"}`), and installs
`django.contrib.sites` (some query tests join `django_site`).

In `async/models.py` each model has `objects = djox.Manager()`, and
`SimpleModel` (the one `test_async_model_methods.py` saves and deletes) is a
`djox.Model`, each marked with a `# djox:` comment, so Django's async tests
run on djox. That is the only change to that app.

## How a test is ported

1. Copy the test module from Django's `tests/` folder (same Django version as
   the one in `uv.lock`, see `DJANGO_TESTS_PORTED_FROM` in `tests/settings.py`), with the
   `models.py` (and migrations) of its app, keeping Django's folder name: the
   app is installed under that name (`"backends"`, `"transactions"`...), like
   in Django's own test runner. Rename `tests.py` to `test_<something>.py` so
   pytest finds it, and fix relative imports for the new place. Neither counts
   as a change.
2. Mark each test class with where it came from:
   `@pytest.mark.django_ported("transactions.tests.AtomicTests")`.
3. Then, for each test, exactly one of:
   - nothing else: the test runs **unchanged** and must pass;
   - `@pytest.mark.django_adapted("why")`: the test was **changed** to fit
     djox. Say what changed and why, and put a `# djox: ...` comment on
     each changed line, so the difference from Django's version is easy to find;
   - `@pytest.mark.django_unsupported("why")`: djox doesn't support this
     **yet**. The test is expected to fail (strict `xfail`), so the day it
     starts passing pytest says so, and the marker must be removed.

`uv run pytest tests/django_suite` ends with a count of unchanged, adapted and
unsupported Django tests.

## Good to know

- The copied files keep Django's code style and have no type hints, so ruff,
  mypy and pyrefly skip them (see `pyproject.toml`).
- Django's SQLite backend lists tests SQLite can't pass
  (`DatabaseFeatures.django_test_skips`); they are skipped here too, as in
  Django's own test runner. So are tests for other databases. The lists use
  Django's test ids (`expressions.tests.FTimeDeltaTests...`), which
  `conftest.py` takes from the `django_ported` marker.
- Under Django's test runner, a `unittest.TestCase` may use the database;
  pytest-django blocks it. Such a test is adapted with `@pytest.mark.django_db`.
- Django's tests run with `USE_TZ = False`; djox's test settings use `True`. A
  test that depends on it is adapted with `@override_settings(USE_TZ=False)`.
- On a Django version other than `DJANGO_TESTS_PORTED_FROM`, these tests are not collected:
  Django's tests change with each release. Re-copy them when `uv.lock` moves
  to a new Django.
