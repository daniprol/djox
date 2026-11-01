//! Turns Django's `%s` placeholders into SQLite's `?`.
//!
//! Django writes SQL for every database with `%s` placeholders, and its SQLite
//! backend rewrites them before running the query. We do the same, with the
//! same rules, so SQL from Django's compiler runs unchanged:
//!
//! - `%s` becomes `?`
//! - `%%` becomes `%` (a literal percent sign)
//! - everything else, including `?`, stays as it is

use std::borrow::Cow;

/// The SQL with `%s` turned into `?` and `%%` into `%`.
///
/// `Cow` ("copy on write") is either a borrowed `&str` or a new `String`: SQL
/// without `%` is returned as is, without copying it.
pub fn to_question_marks(sql: &str) -> Cow<'_, str> {
    if !sql.contains('%') {
        return Cow::Borrowed(sql);
    }
    let mut converted = String::with_capacity(sql.len());
    let mut characters = sql.chars().peekable();
    while let Some(character) = characters.next() {
        if character == '%' {
            match characters.peek() {
                Some('s') => {
                    characters.next();
                    converted.push('?');
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
    use super::to_question_marks;

    #[test]
    fn converts_like_djangos_sqlite_backend() {
        assert_eq!(to_question_marks("SELECT 1"), "SELECT 1");
        assert_eq!(to_question_marks("WHERE a = %s AND b = %s"), "WHERE a = ? AND b = ?");
        assert_eq!(
            to_question_marks("WHERE a LIKE '50%%' AND b = %s"),
            "WHERE a LIKE '50%' AND b = ?"
        );
        assert_eq!(to_question_marks("WHERE a = ?"), "WHERE a = ?");
        // `%%s` is an escaped percent followed by the letter s, not a placeholder.
        assert_eq!(to_question_marks("SELECT '%%s'"), "SELECT '%s'");
        assert_eq!(to_question_marks("SELECT 'é%s'"), "SELECT 'é?'");
    }
}
