//! Turns Django's `%s` placeholders into SQLite's `?`.
//!
//! Django writes SQL for every database with `%s` placeholders, and its SQLite
//! backend rewrites them before running the query. We do the same, with the
//! same rules, so SQL from Django's compiler runs unchanged:
//!
//! - `%s` becomes `?`
//! - `%%` becomes `%` (a literal percent sign)
//! - everything else, including `?`, stays as it is
//!
//! Django also accepts named placeholders, `%(name)s`, with a dict of
//! parameters. [`named_to_question_marks`] turns those into `?` too, and
//! lists the names in order, so the caller can line up the values.

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

/// The SQL with each `%(name)s` turned into `?` and `%%` into `%`, and the
/// names in the order they appear (a name used twice is listed twice).
///
/// `"WHERE a = %(x)s OR b = %(x)s"` gives `"WHERE a = ? OR b = ?"` and `["x", "x"]`.
pub fn named_to_question_marks(sql: &str) -> (String, Vec<String>) {
    let mut converted = String::with_capacity(sql.len());
    let mut names = Vec::new();
    let mut rest = sql;
    // `find('%')` gives the position of the next `%`, or `None` when there is none left.
    while let Some(position) = rest.find('%') {
        converted.push_str(&rest[..position]);
        let after_percent = &rest[position + 1..];
        if let Some(after_escape) = after_percent.strip_prefix('%') {
            converted.push('%');
            rest = after_escape;
        } else if let Some((name, after_name)) = placeholder_name(after_percent) {
            converted.push('?');
            names.push(name.to_string());
            rest = after_name;
        } else {
            converted.push('%');
            rest = after_percent;
        }
    }
    converted.push_str(rest);
    (converted, names)
}

/// If `text` starts with `(name)s`, the name and the text after it.
fn placeholder_name(text: &str) -> Option<(&str, &str)> {
    let inside = text.strip_prefix('(')?;
    let closing = inside.find(')')?;
    let name = &inside[..closing];
    let after_name = inside[closing + 1..].strip_prefix('s')?;
    Some((name, after_name))
}

#[cfg(test)]
mod tests {
    use super::{named_to_question_marks, to_question_marks};

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

    #[test]
    fn converts_named_placeholders_and_lists_the_names() {
        let (sql, names) = named_to_question_marks("WHERE a = %(x)s AND b = %(y)s OR c = %(x)s");
        assert_eq!(sql, "WHERE a = ? AND b = ? OR c = ?");
        assert_eq!(names, ["x", "y", "x"]);

        let (sql, names) = named_to_question_marks("WHERE a LIKE '50%%' AND b = %(x)s");
        assert_eq!(sql, "WHERE a LIKE '50%' AND b = ?");
        assert_eq!(names, ["x"]);

        let (sql, names) = named_to_question_marks("SELECT '%%(x)s', '100%'");
        assert_eq!(sql, "SELECT '%(x)s', '100%'");
        assert!(names.is_empty());
    }
}
