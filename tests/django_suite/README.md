# Django's own ORM tests, ported to djox

Django's test suite is the best description of how its ORM must behave. As
djox takes over parts of the ORM, the matching Django tests are copied here
and run against djox.

| Folder          | From Django's `tests/`                              | What it checks on djox                       |
|-----------------|-----------------------------------------------------|----------------------------------------------|
| `backends/`     | `backends/tests.py`, `backends/sqlite/tests.py`     | Cursors, placeholders, constraints, threads, the SQLite backend |
| `transactions/` | `transactions/tests.py`                             | `atomic()`, savepoints, autocommit           |
| `db_functions/` | `db_functions/{datetime,math,text,comparison}/`     | Django's SQL functions (`django_date_extract`, `MD5`...) on djox's SQLite |

## How a test is ported

1. Copy the test module from Django's `tests/` folder (same Django version as
   the one in `uv.lock`, see `PORTED_FROM` in `conftest.py`), with the
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
  Django's own test runner. So are tests for other databases.
- Django's tests run with `USE_TZ = False`; djox's test settings use `True`. A
  test that depends on it is adapted with `@override_settings(USE_TZ=False)`.
- On a Django version other than `PORTED_FROM`, these tests are not collected:
  Django's tests change with each release. Re-copy them when `uv.lock` moves
  to a new Django.
