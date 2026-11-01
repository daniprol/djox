"""Creating and destroying test databases: Django's, closing djox's pool first.

Postgres refuses to drop a database while other connections use it, and
djox's async database keeps a pool of connections open. So before Django
drops a test database, djox closes its pool (waiting for the statements
already sent).
"""

from django.db.backends.postgresql import creation

from djox._connections import connections


class DatabaseCreation(creation.DatabaseCreation):
    """Django's Postgres test database handling, closing djox's pool before a drop."""

    def _destroy_test_db(self, test_database_name: str, verbosity: int) -> None:
        connections.close(self.connection.alias)
        # django-stubs leave out Django's private creation methods.
        super()._destroy_test_db(test_database_name, verbosity)  # type: ignore[misc]
