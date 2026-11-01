//! Turns Django's `%s` placeholders into Postgres's `$1`, `$2`...
//!
//! Django writes SQL for every database with `%s` placeholders. Postgres
//! numbers its placeholders instead. The rules are psycopg's, so SQL from
//! Django's compiler runs unchanged:
//!
//! - the first `%s` becomes `$1`, the second `$2`, and so on
//! - `%%` becomes `%` (a literal percent sign)
//! - everything else stays as it is

use std::borrow::Cow;

/// The SQL with each `%s` turned into `$1`, `$2`... and `%%` into `%`.
///
/// `Cow` ("copy on write") is either a borrowed `&str` or a new `String`: SQL
/// without `%` is returned as is, without copying it.
pub fn to_numbered_placeholders(sql: &str) -> Cow<'_, str> {
    if !sql.contains('%') {
        return Cow::Borrowed(sql);
    }
    let mut converted = String::with_capacity(sql.len() + 8);
    let mut placeholder_count = 0;
    // `.peekable()` lets us look at the next character without taking it.
    let mut characters = sql.chars().peekable();
    while let Some(character) = characters.next() {
        if character == '%' {
            match characters.peek() {
                Some('s') => {
                    characters.next();
                    placeholder_count += 1;
                    converted.push('$');
                    converted.push_str(&placeholder_count.to_string());
                    continue;
                }
                Some('%') => {
                    characters.next();
                    converted.push('%');
                    continue;
                }
                _ => {}
            }
        }
        converted.push(character);
    }
    Cow::Owned(converted)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbers_each_placeholder() {
        assert_eq!(
            to_numbered_placeholders("SELECT * FROM book WHERE pages > %s AND title = %s"),
            "SELECT * FROM book WHERE pages > $1 AND title = $2"
        );
    }

    #[test]
    fn a_double_percent_is_a_literal_percent() {
        assert_eq!(
            to_numbered_placeholders("SELECT 'a%%' LIKE %s, 10 %% 3"),
            "SELECT 'a%' LIKE $1, 10 % 3"
        );
    }

    #[test]
    fn sql_without_percent_is_not_copied() {
        assert!(matches!(to_numbered_placeholders("SELECT 1"), Cow::Borrowed(_)));
    }
}
