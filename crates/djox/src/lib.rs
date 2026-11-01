//! The djox backends: each one runs SQL for async Python code without
//! blocking it, and implements the [`Database`] trait from `djox-core`.
//!
//! Each backend lives in its own folder and owns everything about its
//! database: settings, connections, value conversions, placeholders. Two
//! backends may look alike in places. That is on purpose: each one stays easy
//! to read on its own, and a change to one can't break the other.
//!
//! | Folder      | Backend                                                         |
//! |-------------|-----------------------------------------------------------------|
//! | `sqlite/`   | [`sqlite::SqliteDatabase`]: one writer thread, a few reader threads |
//! | `postgres/` | (later) a pool of async connections                              |
//!
//! The shared types and the trait come from the `djox-core` crate and are
//! re-exported here, so `use djox::Value` works.

pub mod sqlite;

pub use djox_core::{ChunkRequests, Database, Error, Executed, Reply, ReplySender, Result, RowStream, Rows, Value};
