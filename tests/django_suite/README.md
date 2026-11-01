# Django's own ORM tests, ported to djox

Django's test suite is the best description of how its ORM must behave. As
djox takes over parts of the ORM (roadmap steps 2 and 3), the matching Django
tests are copied here and run against djox.

## How a test is ported

1. Copy the test from Django's `tests/` folder (same Django version as the one
   in `uv.lock`), with the models it needs, into a file named after Django's
   test module (`queries/test_qs_combinators.py` becomes
   `test_qs_combinators.py`, its models go in a `models.py` app next to it).
2. Mark where it came from with `@pytest.mark.django_ported("<django test id>")`,
   for example `"queries.tests.Queries1Tests.test_ticket1050"`.
3. Then exactly one of:
   - nothing else: the test runs **unchanged** and must pass;
   - `@pytest.mark.django_adapted("why")`: the test was **changed** to fit
     djox (for example `aget()` instead of `get()`). Say what changed and why;
   - `@pytest.mark.django_unsupported("why")`: djox doesn't support this
     **yet**. The test is expected to fail (strict `xfail`), so the day it
     starts passing pytest says so, and the marker must be removed.

`uv run pytest tests/django_suite` ends with a count of supported, adapted and
unsupported Django tests.
