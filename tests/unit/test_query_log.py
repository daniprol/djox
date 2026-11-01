"""The SQL written to Django's query log: parameters filled in, for reading only."""

from djox._running import sql_with_parameters


def test_parameters_are_written_in_like_sql_literals() -> None:
    sql = "SELECT * FROM book WHERE title = %s AND pages > %s AND cover IS %s"
    assert sql_with_parameters(sql, ["Don't panic", 300, None]) == (
        "SELECT * FROM book WHERE title = 'Don''t panic' AND pages > 300 AND cover IS NULL"
    )


def test_sql_without_parameters_is_kept_as_written() -> None:
    assert sql_with_parameters("SELECT '100%%'", []) == "SELECT '100%%'"


def test_sql_that_doesnt_match_its_parameters_still_logs() -> None:
    assert sql_with_parameters("SELECT %s", [1, 2]) == "SELECT %s -- parameters: [1, 2]"
