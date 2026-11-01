//! Sorts Postgres errors into djox errors.
//!
//! Every error Postgres reports carries a five-character code, its SQLSTATE,
//! like `23505` (a duplicate unique value). The first two characters are its
//! *class*: `23` is "integrity constraint violation". psycopg turns classes
//! into PEP 249 exceptions, and Django turns those into its own
//! (`django.db.IntegrityError`...). djox sorts them the same way, so a query
//! raises the same Django exception whether psycopg or djox ran it.
//!
//! | Class                 | Meaning                         | djox error          |
//! |-----------------------|---------------------------------|---------------------|
//! | `23`                  | a constraint failed             | `Integrity`         |
//! | `22`                  | a value doesn't fit             | `Data`              |
//! | `42`, `3D`, `3F`, `26`, `34`, `44` | a mistake in the SQL (syntax, missing table...) | `Programming` |
//! | `0A`                  | not supported                   | `NotSupported`      |
//! | `25`, `2D`, `XX`, `2B` | the transaction's or server's state | `Internal`      |
//! | anything else         | lost connection, deadlock, timeout... | `Operational` |

use djox_core::Error;

/// A `tokio_postgres` error as a djox error.
pub fn to_djox_error(error: tokio_postgres::Error) -> Error {
    // `as_db_error()` is the error Postgres itself reported, if it was one
    // (an error can also come from the driver: a closed connection, a
    // parameter that can't be sent...).
    if let Some(database_error) = error.as_db_error() {
        let message = database_error.message().to_string();
        return error_for_code(database_error.code().code(), message);
    }
    let message = full_message(&error);
    if error.is_closed() {
        Error::Operational(message)
    } else {
        // The driver refused the statement before sending it: the wrong
        // number of parameters, or a value that doesn't fit its column type.
        Error::Programming(message)
    }
}

/// The djox error for a SQLSTATE `code`.
pub fn error_for_code(code: &str, message: String) -> Error {
    // `.get(0..2)` is the first two characters, or `None` if the code is shorter.
    match code.get(0..2).unwrap_or("") {
        "23" => Error::Integrity(message),
        "22" => Error::Data(message),
        "42" | "3D" | "3F" | "26" | "34" | "44" => Error::Programming(message),
        "0A" => Error::NotSupported(message),
        "25" | "2D" | "XX" | "2B" => Error::Internal(message),
        _ => Error::Operational(message),
    }
}

/// The error's message with the reason behind it ("error serializing
/// parameter 0: cannot convert..."): `to_string()` alone gives only the first part.
fn full_message(error: &tokio_postgres::Error) -> String {
    // `std::error::Error::source` is the error that caused this one, like `__cause__`.
    match std::error::Error::source(error) {
        Some(cause) => format!("{error}: {cause}"),
        None => error.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sorts_errors_by_class_like_psycopg() {
        let sorted = |code: &str| error_for_code(code, "message".to_string());
        assert_eq!(sorted("23505"), Error::Integrity("message".into()));
        assert_eq!(sorted("22003"), Error::Data("message".into()));
        assert_eq!(sorted("42P01"), Error::Programming("message".into()));
        assert_eq!(sorted("0A000"), Error::NotSupported("message".into()));
        assert_eq!(sorted("25P02"), Error::Internal("message".into()));
        assert_eq!(sorted("40P01"), Error::Operational("message".into()));
        assert_eq!(sorted("57014"), Error::Operational("message".into()));
    }
}
