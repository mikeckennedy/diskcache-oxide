//! What `Cache(durable=True)` keeps about the failures it answers as a miss (plan 09, step 2).
//!
//! An outage can fail thousands of calls a second, so each bucket logs its first failure at once and
//! then at most once per `LOG_EVERY` seconds, with per-operation counts of what it left out. A timeout
//! storm can't hide a damaged database, or either of them a lost invalidation, because each has its
//! own limit. Only the failure path touches any of this, and time comes from the failing operation's
//! own clock reading. An explicit `close()` logs what's left. Dropping a handle doesn't: deallocation
//! can happen anywhere, and running logging handlers from it isn't safe.

use std::sync::atomic::{AtomicU64, Ordering};

use diskcache_oxide_core::Error;
use pyo3::exceptions::{PyException, PyMemoryError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::cache::logger;

/// The fewest seconds between two log lines from one bucket of one handle.
const LOG_EVERY: f64 = 60.0;
/// `last_logged` before a bucket's first line. A clock reading has these bits only as one NaN.
const NEVER: u64 = u64::MAX;

/// The single-key calls that answer a failure as a miss.
#[derive(Clone, Copy)]
pub(crate) enum Op {
    Get,
    Contains,
    Pop,
    Set,
    Add,
    Touch,
    Incr,
    Decr,
    Delete,
    Push,
    Pull,
    Peek,
    PeekItem,
}

/// Each `Op`'s name, by its discriminant.
const OP_NAMES: [&str; 13] =
    ["get", "in", "pop", "set", "add", "touch", "incr", "decr", "delete", "push", "pull", "peek", "peekitem"];

/// One limit on logging, with its counts.
struct Bucket {
    /// The clock reading of the last line, as `f64` bits, or `NEVER`.
    last_logged: AtomicU64,
    /// By `Op`: failures since the last line.
    since: [AtomicU64; OP_NAMES.len()],
    /// By `Op`: every failure, for the stats.
    total: [AtomicU64; OP_NAMES.len()],
}

impl Bucket {
    fn new() -> Self {
        Self {
            last_logged: AtomicU64::new(NEVER),
            since: std::array::from_fn(|_| AtomicU64::new(0)),
            total: std::array::from_fn(|_| AtomicU64::new(0)),
        }
    }

    fn add(&self, op: Op) {
        self.since[op as usize].fetch_add(1, Ordering::Relaxed);
        self.total[op as usize].fetch_add(1, Ordering::Relaxed);
    }

    /// Counts a failure. Returns the counts to log, if a line is due and this caller won it.
    ///
    /// `now` was read before the call waited, so it can be up to `timeout` old: that much of a step
    /// back is an ordinary call, not a clock change. Past it, a clock that jumped back counts as due,
    /// so it can't silence the log for as long as it jumped. Written so a NaN from `clock=`, in either
    /// reading, counts as due and can't silence it for good either.
    fn count(&self, op: Op, now: f64, timeout: f64) -> Option<String> {
        self.add(op);
        let last = self.last_logged.load(Ordering::Acquire);
        let elapsed = now - f64::from_bits(last);
        let due = last == NEVER || !(elapsed < LOG_EVERY && elapsed > -(LOG_EVERY + timeout));
        if !due || self.last_logged.compare_exchange(last, now.to_bits(), Ordering::AcqRel, Ordering::Acquire).is_err()
        {
            return None;
        }
        Some(self.take_counts())
    }

    /// What's been counted since the last line, emptied. A count racing this lands in this line or the
    /// next, and is never lost.
    fn take_counts(&self) -> String {
        let parts: Vec<String> = OP_NAMES
            .iter()
            .zip(&self.since)
            .filter_map(|(name, since)| match since.swap(0, Ordering::Relaxed) {
                0 => None,
                n => Some(format!("{name}: {n}")),
            })
            .collect();
        parts.join(", ")
    }

    fn totals<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let counts = PyDict::new(py);
        for (name, total) in OP_NAMES.iter().zip(&self.total) {
            counts.set_item(*name, total.load(Ordering::Relaxed))?;
        }
        Ok(counts)
    }
}

/// A durable handle's record of the failures it swallowed.
pub(crate) struct Failures {
    /// `Timeout`: a lock or a fork outlasted `timeout`. Logged as a warning.
    busy: Bucket,
    /// Everything else: a full disk, an I/O error, a damaged database. Logged as an error.
    broken: Bucket,
    /// Any failed `delete`, whose entry stays and can be served stale. Logged as an error.
    lost_deletes: Bucket,
    /// The handle's `timeout` in seconds: how old a failing call's clock reading can be.
    timeout: f64,
}

impl Failures {
    pub(crate) fn new(timeout: f64) -> Self {
        Self { busy: Bucket::new(), broken: Bucket::new(), lost_deletes: Bucket::new(), timeout }
    }

    /// Counts an outage that reached a read through a pickle hook. It's reported through
    /// `on_decode_error`, once per stored version, so this adds no line of its own: the bucket's next
    /// one includes it.
    pub(crate) fn count_hook_outage(&self, op: Op, busy: bool) {
        if busy { &self.busy } else { &self.broken }.add(op);
    }

    /// Counts a swallowed `error` and logs it if a line is due. A logging handler's failure is
    /// unraisable, as for a decode report, except what the caller must always see: `MemoryError` and
    /// anything that isn't an `Exception`.
    pub(crate) fn record(&self, py: Python<'_>, op: Op, now: f64, error: &Error, directory: &str) -> PyResult<()> {
        let (bucket, level, message) = if matches!(op, Op::Delete) {
            (
                &self.lost_deletes,
                "error",
                "cache at %s lost invalidations (%s): each entry stays and can be served stale until it expires, \
                 or forever if it has no expiry: %s. More may follow, counted once a minute",
            )
        } else if matches!(error, Error::Timeout { .. }) {
            (
                &self.busy,
                "warning",
                "cache at %s is busy, so calls were answered as a miss or False (%s): %s. More may follow, counted \
                 once a minute",
            )
        } else {
            (
                &self.broken,
                "error",
                "cache at %s is failing, so calls were answered as a miss or False (%s): %s. More may follow, counted \
                 once a minute",
            )
        };
        let Some(counts) = bucket.count(op, now, self.timeout) else { return Ok(()) };
        let logged = logger(py)
            .and_then(|log| log.call_method1(level, (message, directory, counts, error.to_string())).map(drop));
        match logged {
            Ok(()) => Ok(()),
            Err(lost) if lost.is_instance_of::<PyMemoryError>(py) || !lost.is_instance_of::<PyException>(py) => {
                Err(lost)
            }
            Err(lost) => {
                lost.write_unraisable(py, None);
                Ok(())
            }
        }
    }

    /// For `close()`: logs what each bucket has counted since its last line, so an outage that ended
    /// inside the limit isn't left at its first line's count. It never raises, as `close()` doesn't.
    pub(crate) fn flush(&self, py: Python<'_>, directory: &str) {
        let buckets = [
            (&self.busy, "warning", "cache at %s was busy, so calls were answered as a miss or False (%s)"),
            (&self.broken, "error", "cache at %s was failing, so calls were answered as a miss or False (%s)"),
            (
                &self.lost_deletes,
                "error",
                "cache at %s lost invalidations (%s): each entry stays and can be served stale until it expires, \
                 or forever if it has no expiry",
            ),
        ];
        for (bucket, level, message) in buckets {
            let counts = bucket.take_counts();
            if counts.is_empty() {
                continue;
            }
            let logged = logger(py).and_then(|log| log.call_method1(level, (message, directory, counts)).map(drop));
            if let Err(lost) = logged {
                lost.write_unraisable(py, None);
            }
        }
    }

    /// Every failure swallowed so far, by bucket and then by operation.
    pub(crate) fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let stats = PyDict::new(py);
        stats.set_item("busy", self.busy.totals(py)?)?;
        stats.set_item("broken", self.broken.totals(py)?)?;
        stats.set_item("lost_deletes", self.lost_deletes.totals(py)?)?;
        Ok(stats)
    }
}
