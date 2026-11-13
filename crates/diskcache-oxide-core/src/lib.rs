//! Storage engine for diskcache-oxide.
//!
//! Deals only in keys, modes and bytes (or value files). Converting Python objects to and from
//! bytes lives in the `diskcache-oxide-py` crate, so this crate never depends on `PyO3`.

// deny, not forbid, so that `stack` and `sqlite_mutex` alone can allow it.
#![deny(unsafe_code)]

pub mod check;
pub mod cleanup;
mod error;
pub mod fanout;
pub mod gate;
pub mod genlock;
pub mod key;
pub mod layout;
pub mod lockwait;
pub mod maintenance;
pub mod open;
pub mod ops;
mod os_error;
pub mod poison;
pub mod pool;
pub mod queue;
pub mod rows;
pub mod schema;
pub mod spill;
pub mod sqlite;
pub mod sqlite_mutex;
mod stack;
pub mod stats;
pub mod store;
mod sync;
#[cfg(test)]
mod testutil;
// The shuttle model checker (scripts/model-check.sh); `crate::sync` swaps in its primitives.
#[cfg(all(test, shuttle))]
mod model;
pub mod wal;

pub use error::{Error, Result};
/// A borrowed view of a stored value, as `Store::get_with` hands it to its decoder.
pub use rusqlite::types::ValueRef;
