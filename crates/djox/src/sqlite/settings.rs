//! [`SqliteSettings`]: where the database is and how to open it.

use std::path::PathBuf;
use std::time::Duration;

use djox_core::{Error, Result};

/// Where the database is and how to open it. Mirrors Django's
/// `DATABASES["default"]` for SQLite: `NAME` is `path`, `OPTIONS["timeout"]`
/// is `busy_timeout`.
#[derive(Debug, Clone)]
pub struct SqliteSettings {
    /// The database file. A `file:` URI works too. Created if it doesn't exist.
    pub path: PathBuf,
    /// How long to wait for a lock held by another process (like Django's
    /// own connections to the same file) before failing.
    pub busy_timeout: Duration,
    /// How many reader connections to open. With 0, the writer runs everything.
    pub reader_count: usize,
    /// The longest a *quick read* may run on the calling thread (see
    /// `quick_reads.rs`). A read that takes longer is stopped there and runs
    /// on a reader instead. Zero turns quick reads off.
    pub quick_read_limit: Duration,
    /// The longest a *quick write* may run on the calling thread (see
    /// `writer.rs`). Zero turns quick writes off: every write goes to the
    /// writer thread, and a transaction block's statements to another thread.
    pub quick_write_limit: Duration,
}

impl SqliteSettings {
    /// Settings for the file at `path`: a 5 second timeout (Django's default),
    /// one reader per CPU core, at most 4 (more rarely helps SQLite), and
    /// quick reads and writes of up to 1 ms.
    ///
    /// `impl Into<PathBuf>` accepts anything that can become a path: a
    /// `&str`, a `String`, a `PathBuf`...
    pub fn new(path: impl Into<PathBuf>) -> SqliteSettings {
        let cores = std::thread::available_parallelism().map_or(1, |count| count.get());
        SqliteSettings {
            path: path.into(),
            busy_timeout: Duration::from_secs(5),
            reader_count: cores.clamp(1, 4),
            quick_read_limit: Duration::from_millis(1),
            quick_write_limit: Duration::from_millis(1),
        }
    }

    /// Fails for settings that can't work.
    ///
    /// In-memory databases are refused: every connection to `:memory:` gets
    /// its own empty database, so the readers would never see the writer's data.
    pub fn check(&self) -> Result<()> {
        if is_in_memory(&self.path.to_string_lossy()) {
            return Err(Error::Settings(
                "djox needs a database file: an in-memory SQLite database can't be shared \
                 between connections. In tests, give Django's TEST setting a NAME, like \
                 DATABASES['default']['TEST'] = {'NAME': 'test.sqlite3'}"
                    .to_string(),
            ));
        }
        Ok(())
    }
}

/// Whether `path` names an in-memory database: `:memory:`, an empty name
/// (SQLite's private temporary database), or a `file:` URI with `mode=memory`
/// (what Django's test runner uses by default).
pub(crate) fn is_in_memory(path: &str) -> bool {
    path.is_empty() || path == ":memory:" || (path.starts_with("file:") && path.contains("mode=memory"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refuses_in_memory_databases() {
        for path in [":memory:", "", "file:memorydb_default?mode=memory&cache=shared"] {
            let result = SqliteSettings::new(path).check();
            assert!(matches!(result, Err(Error::Settings(_))), "{path:?} should be refused");
        }
        assert!(SqliteSettings::new("db.sqlite3").check().is_ok());
    }
}
