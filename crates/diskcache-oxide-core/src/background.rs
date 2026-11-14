//! Background maintenance (plan 28): one thread per process that does each open store's housekeeping
//! on a schedule, so a cache that's mostly read still frees its expired rows' space, gets back under
//! its size limit, keeps its write-ahead log small and loses its stray temporary files.
//!
//! [`schedule`] is the timing, as pure functions over plain values: when each store's turn comes, and
//! which tasks a turn runs.

pub mod schedule;
