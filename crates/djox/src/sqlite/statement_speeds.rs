//! [`StatementSpeeds`]: which statements ran quickly last time, and which ran
//! out of time, so the calling thread only tries the ones likely to be quick.
//!
//! Quick reads (`quick_reads.rs`) and quick writes (`writer.rs`) run a
//! statement right on the calling thread, and stop it once it has run for
//! their time limit. Stopping costs that moment, so a statement that ran out
//! of time is remembered as slow: its next 100 runs skip the calling thread.
//! Then it gets another chance (a query is not always slow: a cold cache, a
//! busy machine). Each time it runs out of time again, it waits twice as long
//! for its next chance, so a statement that is really slow soon costs almost
//! nothing.
//!
//! The writer also needs the opposite: inside a transaction, SQLite can't
//! stop a write without rolling back the whole transaction, so a write runs
//! on the calling thread there only if it was quick the last time it ran.

use std::collections::{HashMap, HashSet};

/// The most statements remembered of each kind. A program has a few dozen
/// kinds of statement; past this many, the list starts over rather than
/// growing forever.
const MAX_REMEMBERED: usize = 1000;

/// How many runs of a statement skip the calling thread after it first ran out of time.
const RUNS_TO_SKIP_AT_FIRST: u32 = 100;

/// How many times that number doubles at most (to 100 × 2⁷ = 12,800 runs).
const MAX_DOUBLINGS: u32 = 7;

/// What is remembered of a statement that ran out of time.
struct SlowStatement {
    /// How many times it ran out of time.
    times_too_slow: u32,
    /// How many of its next runs still skip the calling thread.
    runs_to_skip: u32,
}

/// The statements that ran out of time, and the ones that finished in time,
/// by SQL (see the module docs).
#[derive(Default)] // `Default`: `StatementSpeeds::default()` makes an empty one, like a dataclass with defaults.
pub(crate) struct StatementSpeeds {
    /// A `HashMap` is like a `dict`.
    slow: HashMap<String, SlowStatement>,
    /// A `HashSet` is like a `set`.
    quick: HashSet<String>,
}

impl StatementSpeeds {
    /// Whether this run of `sql` should skip the calling thread, because it ran
    /// out of time lately (one run fewer to skip, then).
    pub(crate) fn skips(&mut self, sql: &str) -> bool {
        // `get_mut()` is like `dict.get()`, with a value we may change in place.
        match self.slow.get_mut(sql) {
            Some(statement) if statement.runs_to_skip > 0 => {
                statement.runs_to_skip -= 1;
                true
            }
            _ => false,
        }
    }

    /// Whether `sql` finished within the time limit the last time it ran.
    pub(crate) fn is_known_quick(&self, sql: &str) -> bool {
        self.quick.contains(sql)
    }

    /// Note that `sql` finished within the time limit.
    pub(crate) fn remember_quick(&mut self, sql: &str) {
        if self.quick.contains(sql) {
            return;
        }
        if self.quick.len() >= MAX_REMEMBERED {
            self.quick.clear();
        }
        self.quick.insert(sql.to_string());
    }

    /// Note that `sql` ran out of time: skip its next runs, twice as many as last time.
    pub(crate) fn remember_slow(&mut self, sql: &str) {
        self.quick.remove(sql);
        if self.slow.len() >= MAX_REMEMBERED && !self.slow.contains_key(sql) {
            self.slow.clear();
        }
        // `entry().or_insert()` is like `dict.setdefault()`.
        let statement = self.slow.entry(sql.to_string()).or_insert(SlowStatement {
            times_too_slow: 0,
            runs_to_skip: 0,
        });
        let doublings = statement.times_too_slow.min(MAX_DOUBLINGS);
        // `<<` doubles a number that many times: `100 << 2` is 400.
        statement.runs_to_skip = RUNS_TO_SKIP_AT_FIRST << doublings;
        statement.times_too_slow += 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn skipped_runs(speeds: &mut StatementSpeeds, sql: &str) -> u32 {
        let mut count = 0;
        while speeds.skips(sql) {
            count += 1;
        }
        count
    }

    #[test]
    fn a_new_statement_is_tried_but_not_known_quick() {
        let mut speeds = StatementSpeeds::default();
        assert!(!speeds.skips("SELECT 1"));
        assert!(!speeds.is_known_quick("SELECT 1"));
    }

    #[test]
    fn a_slow_statement_skips_more_runs_each_time() {
        let mut speeds = StatementSpeeds::default();
        speeds.remember_slow("SELECT 1");
        assert_eq!(skipped_runs(&mut speeds, "SELECT 1"), RUNS_TO_SKIP_AT_FIRST);
        speeds.remember_slow("SELECT 1");
        assert_eq!(skipped_runs(&mut speeds, "SELECT 1"), 2 * RUNS_TO_SKIP_AT_FIRST);
    }

    #[test]
    fn running_out_of_time_forgets_that_a_statement_was_quick() {
        let mut speeds = StatementSpeeds::default();
        speeds.remember_quick("UPDATE book SET title = ?");
        assert!(speeds.is_known_quick("UPDATE book SET title = ?"));
        speeds.remember_slow("UPDATE book SET title = ?");
        assert!(!speeds.is_known_quick("UPDATE book SET title = ?"));
    }
}
