"""A django-bolt version of the web app, for `bolt_load_test.py`.

Run by hand (after `python ../app.py setup` with the same `STACK`):

    STACK=djox python manage.py runbolt --no-admin --port 8765

Its models and tables are the `shop` app of the web benchmark.
"""
