//! The shared types of djox, and the [`Database`] trait every backend implements.
//!
//! # How a query travels through djox
//!
//! An ORM turns Python method calls into SQL, runs the SQL, and turns the rows
//! back into Python objects. djox splits that journey in three:
//!
//! 1. **Python** (Django's QuerySet, later djox's own) builds a SQL string and
//!    a list of parameters, like `("SELECT ... WHERE pages > %s", [100])`.
//! 2. **A backend** (this trait, implemented by `djox::sqlite` and
//!    `djox::postgres`) runs the SQL on a real connection without blocking
//!    Python's event loop, and returns plain rows.
//! 3. **Python** turns the rows into model instances.
//!
//! This crate is the contract between 1-3 and 2: the values that go in and
//! out, the errors, and the [`Database`] trait itself.
//!
//! | File          | What it holds                                                    |
//! |---------------|------------------------------------------------------------------|
//! | `database.rs` | [`Database`]: what every backend must be able to do               |
//! | `value.rs`    | [`Value`]: one value sent to or read from the database            |
//! | `rows.rs`     | [`Rows`] (what a query returns) and [`Executed`] (what a write returns) |
//! | `reply.rs`    | [`ReplySender`]: how a backend hands back an answer; [`Reply`] to await it |
//! | `stream.rs`   | [`RowStream`]: the rows of a big query, a chunk at a time           |
//! | `error.rs`    | [`Error`] and [`Result`]                                          |

mod database;
mod error;
mod reply;
mod rows;
mod stream;
mod value;

pub use database::Database;
pub use error::{Error, Result};
pub use reply::{Reply, ReplySender};
pub use rows::{Executed, Rows};
pub use stream::{ChunkRequests, RowStream};
pub use value::{RangeValue, Value};
