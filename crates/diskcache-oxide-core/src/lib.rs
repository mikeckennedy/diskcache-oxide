//! Storage engine for diskcache-oxide.
//!
//! Deals only in keys, modes and bytes (or value files). Converting Python objects to and from
//! bytes lives in the `diskcache-oxide-py` crate, so this crate never depends on `PyO3`.

#![forbid(unsafe_code)]

mod error;
pub mod gate;
pub mod pool;
pub mod spike;
pub mod sqlite;

pub use error::{Error, Result};
