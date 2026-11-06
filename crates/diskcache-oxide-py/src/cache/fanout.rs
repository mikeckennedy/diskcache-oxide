//! `diskcache_oxide.FanoutCache`: one handle over the N shard stores of a fan-out root (plan 13).
//!
//! Each key lives in exactly one shard, chosen by the frozen hash in `core::fanout`, so every
//! process routes it the same way forever. A routed operation is exactly `Cache`'s, run by the same
//! `HandleConfig` method against the key's shard. Calls over every shard (`len`, `volume`, `clear`,
//! `expire`, `cull`, `checkpoint`, `close`) share one deadline, not one each. A maintenance call
//! carries on past a shard that fails and then raises the first failure, saying how far it got
//! (`partial_failure`). `__iter__` walks the shards one after another; `iterkeys` merges the
//! shards' sorted pages (`FanoutKeyIterator`).

use std::cmp::Ordering as KeyOrdering;
use std::collections::{BinaryHeap, VecDeque};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, TryLockError};
use std::time::{Duration, Instant};

use diskcache_oxide_core::fanout::{self, Manifest};
use diskcache_oxide_core::maintenance::{Cursor, Interrupted, Order, PAGE, Snapshot};
use diskcache_oxide_core::open::Role;
use diskcache_oxide_core::ops::Number;
use diskcache_oxide_core::store::Store;
use diskcache_oxide_core::{Error, lockwait};
use pyo3::PyTraverseError;
use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::gc::PyVisit;
use pyo3::prelude::*;
use pyo3::types::PyTuple;

use super::{DecodePolicy, Delta, HandleConfig, IncrDefault, Settings, StoreHandle, Transaction, str_key, whole};
use crate::to_py_err;

/// A disk cache split over several SQLite databases (shards), so reads and writes on different
/// keys don't share one file's locks.
#[pyclass(frozen, module = "diskcache_oxide")]
pub(crate) struct FanoutCache {
    /// One per shard, in shard order: `shards[i].shard == i`.
    shards: Box<[StoreHandle]>,
    config: HandleConfig,
}

#[pymethods]
impl FanoutCache {
    #[new]
    #[pyo3(signature = (
        directory,
        timeout = None,
        *,
        shards = None,
        size_limit = None,
        cull_limit = None,
        serializer = "msgpack",
        pickle = false,
        clock = None,
        max_connections = None,
        sqlite_cache_size = None,
        wal_size_limit = None,
        wal_checkpoint = "manual",
        on_decode_error = None,
        durable = false,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        directory: PathBuf,
        timeout: Option<&Bound<'_, PyAny>>,
        shards: Option<&Bound<'_, PyAny>>,
        size_limit: Option<&Bound<'_, PyAny>>,
        cull_limit: Option<&Bound<'_, PyAny>>,
        serializer: &str,
        pickle: bool,
        clock: Option<Py<PyAny>>,
        max_connections: Option<&Bound<'_, PyAny>>,
        sqlite_cache_size: Option<&Bound<'_, PyAny>>,
        wal_size_limit: Option<&Bound<'_, PyAny>>,
        wal_checkpoint: &str,
        on_decode_error: Option<DecodePolicy>,
        durable: bool,
    ) -> PyResult<Self> {
        let settings = Settings::parse(
            py,
            timeout,
            size_limit,
            cull_limit,
            serializer,
            max_connections,
            sqlite_cache_size,
            wal_size_limit,
            wal_checkpoint,
        )?;
        let shards = shard_count(shards)?;
        let stores = crate::detach(py, || open(&directory, &settings, shards))
            .map_err(|(e, shards)| open_error(py, e, shards))?;
        let shards = stores
            .into_iter()
            .enumerate()
            .map(|(shard, store)| StoreHandle { store, shard, closed: AtomicBool::new(false) })
            .collect();
        Ok(Self { shards, config: HandleConfig::new(&directory, &settings, pickle, clock, on_decode_error, durable) })
    }

    // -- attributes ---------------------------------------------------------------------------

    /// The root directory.
    #[getter]
    fn directory(&self) -> &str {
        &self.config.directory
    }

    #[getter]
    fn timeout(&self) -> f64 {
        self.config.timeout.as_secs_f64()
    }

    /// How many shards the root has, as its manifest records.
    #[getter]
    fn shards(&self) -> usize {
        self.shards.len()
    }

    /// The sum of every shard's stored `size_limit`.
    #[getter]
    fn size_limit(&self) -> i64 {
        self.shards.iter().map(|h| h.store.cached_limits().size_limit).sum()
    }

    /// Shard 0's stored `cull_limit`. Every shard is opened with the same one, so this is every
    /// shard's only while they were all created, and last set, under the same settings.
    #[getter]
    fn cull_limit(&self) -> i64 {
        self.shards[0].store.cached_limits().cull_limit
    }

    /// Always True for a real cache (a `NullCache` is False).
    #[getter]
    #[allow(clippy::unused_self)] // A Python attribute: it needs `self` even though it's constant.
    fn enabled(&self) -> bool {
        true
    }

    /// The shard `key` lives in, `0 <= shard_of(key) < shards`: the same in every process and every
    /// release. Keys with the same shard can share a `transact(key=...)`.
    fn shard_of(&self, key: &Bound<'_, PyAny>) -> PyResult<usize> {
        Ok(self.route(str_key(key)?).shard)
    }

    /// Test hook: every failure `durable=True` has swallowed on this handle, as `Cache`'s.
    fn _durable_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        match &self.config.failures {
            Some(failures) => failures.stats(py),
            None => Ok(pyo3::types::PyDict::new(py)),
        }
    }

    /// Test hook: how many open handles each shard's store counts, in shard order.
    fn _handle_counts(&self) -> Vec<isize> {
        self.shards.iter().map(|h| h.store.handles()).collect()
    }

    fn __repr__(&self) -> String {
        format!("FanoutCache({:?}, shards={})", self.config.directory, self.shards.len())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.config.traverse(&visit)
    }

    // -- routed to one shard -------------------------------------------------------------------

    #[pyo3(signature = (key, default = None, *, expire_time = false))]
    fn get(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        default: Option<Py<PyAny>>,
        expire_time: bool,
    ) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        self.config.get(py, self.routed(name)?, name, default, expire_time)
    }

    fn __getitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        self.config.get_item(py, self.routed(name)?, key, name)
    }

    fn __contains__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
        let name = str_key(key)?;
        self.config.contains(py, self.routed(name)?, name)
    }

    #[pyo3(signature = (key, value, expire = None))]
    fn set(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        expire: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let name = str_key(key)?;
        self.config.set(py, self.routed(name)?, name, value, expire)
    }

    fn __setitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        self.set(py, key, value, None).map(|_| ())
    }

    #[pyo3(signature = (key, value, expire = None))]
    fn add(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        expire: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let name = str_key(key)?;
        self.config.add(py, self.routed(name)?, name, value, expire)
    }

    fn delete(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
        let name = str_key(key)?;
        self.config.delete(py, self.routed(name)?, name)
    }

    fn __delitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<()> {
        if self.delete(py, key)? { Ok(()) } else { Err(PyKeyError::new_err(key.clone().unbind())) }
    }

    #[pyo3(signature = (key, default = None))]
    fn pop(&self, py: Python<'_>, key: &Bound<'_, PyAny>, default: Option<Py<PyAny>>) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        self.config.pop(py, self.routed(name)?, name, default)
    }

    #[pyo3(signature = (key, expire = None))]
    fn touch(&self, py: Python<'_>, key: &Bound<'_, PyAny>, expire: Option<&Bound<'_, PyAny>>) -> PyResult<bool> {
        let name = str_key(key)?;
        self.config.touch(py, self.routed(name)?, name, expire)
    }

    #[pyo3(signature = (key, delta = Delta(Number::Int(1)), default = IncrDefault(Some(Number::Int(0)))))]
    fn incr(&self, py: Python<'_>, key: &Bound<'_, PyAny>, delta: Delta, default: IncrDefault) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        self.config.add_number(py, self.routed(name)?, key, name, delta, default, false)
    }

    #[pyo3(signature = (key, delta = Delta(Number::Int(1)), default = IncrDefault(Some(Number::Int(0)))))]
    fn decr(&self, py: Python<'_>, key: &Bound<'_, PyAny>, delta: Delta, default: IncrDefault) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        self.config.add_number(py, self.routed(name)?, key, name, delta, default, true)
    }

    // -- over every shard, under one deadline ---------------------------------------------------

    fn __len__(&self, py: Python<'_>) -> PyResult<usize> {
        let counts = self.every_shard(py, Store::len)?;
        Ok(counts.into_iter().map(|n| usize::try_from(n).unwrap_or(0)).sum())
    }

    /// Always True: a cache is usable even when empty (D-009).
    #[allow(clippy::unused_self)] // A Python protocol method: it needs `self`.
    fn __bool__(&self) -> bool {
        true
    }

    /// The sum of every shard's volume.
    fn volume(&self, py: Python<'_>) -> PyResult<i64> {
        Ok(self.every_shard(py, Store::volume)?.into_iter().sum())
    }

    /// Removes every expired entry from every shard. Returns how many it removed. `timeout`
    /// overrides the cache's for this call: one budget for the whole call (see `remove_everywhere`).
    #[pyo3(signature = (*, timeout = None))]
    fn expire(&self, py: Python<'_>, timeout: Option<&Bound<'_, PyAny>>) -> PyResult<u64> {
        let timeout = self.config.call_timeout(timeout)?;
        let now = self.config.now(py)?;
        self.remove_everywhere(py, "expire", timeout, |store, snapshot, deadline| {
            store.expire_until(snapshot, now, deadline)
        })
    }

    /// Removes every entry stored before the call. Returns how many it removed. `timeout` as for
    /// `expire`.
    #[pyo3(signature = (*, timeout = None))]
    fn clear(&self, py: Python<'_>, timeout: Option<&Bound<'_, PyAny>>) -> PyResult<u64> {
        let timeout = self.config.call_timeout(timeout)?;
        self.remove_everywhere(py, "clear", timeout, Store::clear_until)
    }

    /// Removes expired entries, then evicts each shard until it's under its share of `size_limit`.
    /// Returns how many it removed. `timeout` as for `expire`.
    #[pyo3(signature = (*, timeout = None))]
    fn cull(&self, py: Python<'_>, timeout: Option<&Bound<'_, PyAny>>) -> PyResult<u64> {
        let timeout = self.config.call_timeout(timeout)?;
        let now = self.config.now(py)?;
        self.remove_everywhere(py, "cull", timeout, |store, snapshot, deadline| {
            store.cull_until(snapshot, now, deadline)
        })
    }

    /// Checkpoints every shard's write-ahead log and truncates it (D-024), retrying busy ones until
    /// one deadline of `timeout` for the whole call; a shard reached after it gets one try. True only
    /// if every shard's WAL was reset. A real error (a full disk) raises once every shard was tried.
    #[pyo3(signature = (*, timeout = None))]
    fn checkpoint(&self, py: Python<'_>, timeout: Option<&Bound<'_, PyAny>>) -> PyResult<bool> {
        let timeout = self.config.call_timeout(timeout)?;
        let deadline = lockwait::deadline_after(Instant::now(), timeout);
        let mut reset = true;
        let mut failures = Vec::new();
        for (i, handle) in self.shards.iter().enumerate() {
            if i > 0 {
                self.check_signals_between(py, "checkpoint", None, &failures)?;
            }
            let outcome = crate::detach(py, || {
                // A closed shard that can't rejoin behind a fork is busy, as on `Cache`.
                match handle.ensure_open(deadline.saturating_duration_since(Instant::now())) {
                    Ok(_) => handle.store.checkpoint_until(deadline),
                    Err(Error::Timeout { .. }) => Ok(false),
                    Err(error) => Err(error),
                }
            });
            match outcome {
                Ok(done) => reset &= done,
                Err(error) => failures.push((handle.shard, error)),
            }
        }
        if failures.is_empty() {
            Ok(reset)
        } else {
            Err(self.partial_failure(py, "checkpoint", timeout, None, failures))
        }
    }

    /// Keys in sorted order (code-point order, like `sorted()`, the same order a `Cache` gives),
    /// optionally only those starting with `prefix`: a merge of every shard's sorted pages. Each
    /// shard keeps a small page buffered (see `merge_page`), so the first `next()` reads every shard
    /// once; no connection is held between `next()` calls.
    #[pyo3(signature = (*, prefix = None, reverse = false))]
    fn iterkeys(slf: Py<Self>, prefix: Option<String>, reverse: bool) -> FanoutKeyIterator {
        let shards = slf.get().shards.len();
        FanoutKeyIterator::new(slf, Walk::merge(shards, reverse), prefix)
    }

    /// Every key once: shard 0's in insertion order, then shard 1's, and so on. That is not the
    /// order the keys were stored in across the whole cache.
    fn __iter__(slf: Py<Self>) -> FanoutKeyIterator {
        FanoutKeyIterator::new(slf, Walk::Shards { shard: 0, cursor: Cursor::Start, buffer: VecDeque::new() }, None)
    }

    // -- lifecycle ----------------------------------------------------------------------------

    /// Releases this handle on every shard, as `Cache.close()` does for its one store, but with
    /// one budget for the whole close: the checkpoints of large WALs share at most 1 s (or
    /// `timeout`, if shorter), and behind a pending fork every release together waits at most 1 s
    /// before it's left for the end of the fork (D-027). Never raises. Using the cache again
    /// reopens the shards it touches. Closing twice is harmless.
    #[pyo3(signature = (*, checkpoint = true))]
    fn close(&self, py: Python<'_>, checkpoint: bool) {
        if let Some(failures) = &self.config.failures {
            failures.flush(py, &self.config.directory);
        }
        let open: Vec<&std::sync::Arc<Store>> =
            self.shards.iter().filter(|h| !h.closed.swap(true, Ordering::AcqRel)).map(|h| &h.store).collect();
        if open.is_empty() {
            return;
        }
        let budget = checkpoint.then(|| self.config.timeout.min(Duration::from_secs(1)));
        // Released only when some shard's last handle closes on a large WAL, as in `Cache.close()`.
        if open.iter().any(|s| s.closing_on_a_large_wal()) {
            crate::detach(py, || Store::close_all(&open, budget));
        } else {
            Store::close_all(&open, budget);
        }
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    #[pyo3(signature = (exc_type, exc, tb, /))]
    fn __exit__(&self, py: Python<'_>, exc_type: &Bound<'_, PyAny>, exc: &Bound<'_, PyAny>, tb: &Bound<'_, PyAny>) {
        let _ = (exc_type, exc, tb);
        self.close(py, true);
    }

    // -- transactions -------------------------------------------------------------------------

    /// A write transaction on the shard `key` lives in: `with cache.transact(key='total') as tx:`.
    /// It is `Cache.transact()` on that shard's store (the same `Transaction`, reentrancy,
    /// `TransactionBusy` and aborted exits), and `tx` is this `FanoutCache`. Inside the block, this
    /// thread's calls on any other shard's key raise `ValueError`, reads included, through any
    /// handle on the root, and so does a `transact` on another shard; `shard_of` says which keys
    /// share one. `key` is keyword-only, so a switched-off cache's `NullCache.transact(key=...)`
    /// takes the same spelling.
    #[pyo3(signature = (*, key, retry = false))]
    fn transact(slf: Py<Self>, key: &Bound<'_, PyAny>, retry: bool) -> PyResult<Transaction> {
        let shard = slf.get().route(str_key(key)?).shard;
        Ok(Transaction::on_shard(slf, shard, retry))
    }
}

impl FanoutCache {
    /// The handle of the shard `key` hashes to.
    #[inline]
    fn route(&self, key: &str) -> &StoreHandle {
        // The shard count came from a validated manifest, so it fits a `u32`.
        let count = u32::try_from(self.shards.len()).unwrap_or(u32::MAX);
        &self.shards[fanout::shard_of_str(key, count)]
    }

    /// The handle of the shard `key` hashes to, for a routed call: refused with `ValueError` if this
    /// thread holds a `transact(key=...)` on another of this root's shards (M3), through any handle.
    /// With no transaction open anywhere in the process, that check is one relaxed load; while any
    /// is open (a `Cache.transact()` on any thread counts), it asks each other shard, which stops at
    /// once for a shard with none and costs a gate entry and a mutex lock for one that has one.
    #[inline]
    fn routed(&self, key: &str) -> PyResult<&StoreHandle> {
        let handle = self.route(key);
        if Store::any_held_in_process() {
            self.refuse_other_shards(handle.shard)?;
        }
        Ok(handle)
    }

    /// `ValueError` if this thread holds a transaction on any of this root's shards but `shard`.
    /// The stores are shared by every handle on the root, so another handle's transaction counts.
    /// At most one shard can be held: a `transact` on a second one is refused here too.
    #[cold]
    #[inline(never)]
    pub(super) fn refuse_other_shards(&self, shard: usize) -> PyResult<()> {
        let Some(held) = self.shards.iter().find(|h| h.shard != shard && h.store.held_by_this_thread()) else {
            return Ok(());
        };
        Err(PyValueError::new_err(format!(
            "this key is on shard {shard}, but this thread's transaction is on shard {held} of {count}: inside \
             FanoutCache.transact(key=...), only keys on the transaction's shard can be used (see shard_of)",
            held = held.shard,
            count = self.shards.len(),
        )))
    }

    /// Shard `shard`'s handle, for a `Transaction` on it.
    pub(super) fn shard_handle(&self, shard: usize) -> &StoreHandle {
        &self.shards[shard]
    }

    /// The handle's settings, for a `Transaction` on one of its shards.
    pub(super) fn config(&self) -> &HandleConfig {
        &self.config
    }

    /// `call` on every shard in order, all under one deadline of the handle's `timeout` (D-027's
    /// one budget, not one each): a closed shard rejoins first, out of the same budget. The first
    /// failure raises, as admin calls do under `durable` (D-028); a timeout names the whole budget.
    fn every_shard<T>(&self, py: Python<'_>, call: impl Fn(&Store, Duration) -> Result<T, Error>) -> PyResult<Vec<T>> {
        let timeout = self.config.timeout;
        let deadline = lockwait::deadline_after(Instant::now(), timeout);
        let mut results = Vec::with_capacity(self.shards.len());
        for handle in &self.shards {
            if !results.is_empty() {
                // Between shards only: after the last one it would throw a finished result away.
                py.check_signals()?;
            }
            let left = deadline.saturating_duration_since(Instant::now());
            let result = handle.ensure_open(left).and_then(|left| call(&handle.store, left));
            results.push(result.map_err(|error| match error {
                Error::Timeout { .. } => to_py_err(Error::Timeout { seconds: timeout.as_secs_f64() }),
                other => to_py_err(other),
            })?);
        }
        Ok(results)
    }
}

impl FanoutCache {
    /// A removal (`clear`, `expire`, `cull`) on every shard under one deadline of `timeout` for the
    /// whole call. It bounds waiting, not work: a chunk already running finishes, and a shard reached
    /// after the deadline still gets its chunks with no wait each, so idle shards are all done even at
    /// a short timeout. Every shard's snapshot is taken first (a closed shard rejoins for it), so
    /// the call removes only what the cache held when it started, and a write made meanwhile on a
    /// shard not yet reached survives, as on `Cache`. A shard that fails doesn't stop the others;
    /// the first failure then raises, with how far the call got (`partial_failure`). Ctrl-C is
    /// checked between shards, not after the last, so a finished call is never thrown away.
    fn remove_everywhere(
        &self,
        py: Python<'_>,
        call: &str,
        timeout: Duration,
        remove: impl Fn(&Store, Snapshot, Instant) -> Result<u64, Interrupted> + Sync,
    ) -> PyResult<u64> {
        let deadline = lockwait::deadline_after(Instant::now(), timeout);
        let snapshots: Vec<Result<Snapshot, Error>> = crate::detach(py, || {
            let left = || deadline.saturating_duration_since(Instant::now());
            self.shards
                .iter()
                .map(|handle| handle.ensure_open(left()).and_then(|_| handle.store.snapshot_until(deadline)))
                .collect()
        });
        let mut removed = 0;
        let mut failures = Vec::new();
        for (i, (handle, snapshot)) in self.shards.iter().zip(snapshots).enumerate() {
            if i > 0 {
                self.check_signals_between(py, call, Some(removed), &failures)?;
            }
            let outcome = match snapshot {
                Ok(snapshot) => crate::detach(py, || remove(&handle.store, snapshot, deadline)),
                Err(error) => Err(Interrupted { removed: 0, error }),
            };
            match outcome {
                Ok(n) => removed += n,
                Err(Interrupted { removed: n, error }) => {
                    removed += n;
                    failures.push((handle.shard, error));
                }
            }
        }
        if failures.is_empty() {
            Ok(removed)
        } else {
            Err(self.partial_failure(py, call, timeout, Some(removed), failures))
        }
    }

    /// The exception a fanned-out call raises when shards failed: the first failure's own type, so
    /// `except Timeout` still works (a timeout names the call's whole budget), with `failed_shards`
    /// (a tuple of the failed shards' indexes, in order) and, for a removal, `removed` (how many it
    /// removed in all, from every shard, the failed ones' committed chunks included), plus a note
    /// naming them. `failures` is never empty.
    fn partial_failure(
        &self,
        py: Python<'_>,
        call: &str,
        timeout: Duration,
        removed: Option<u64>,
        failures: Vec<(usize, Error)>,
    ) -> PyErr {
        let failed: Vec<usize> = failures.iter().map(|(shard, _)| *shard).collect();
        let Some((_, first)) = failures.into_iter().next() else {
            return PyValueError::new_err("partial_failure needs a failure");
        };
        let raised = to_py_err(match first {
            Error::Timeout { .. } => Error::Timeout { seconds: timeout.as_secs_f64() },
            other => other,
        });
        self.annotate(py, raised, call, removed, &failed)
    }

    /// Checks for a signal between two shards. An exception it raises (Ctrl-C) after some shard
    /// already failed carries the same `failed_shards`, `removed` and note as a partial failure.
    fn check_signals_between(
        &self,
        py: Python<'_>,
        call: &str,
        removed: Option<u64>,
        failures: &[(usize, Error)],
    ) -> PyResult<()> {
        py.check_signals().map_err(|raised| {
            if failures.is_empty() {
                return raised;
            }
            let failed: Vec<usize> = failures.iter().map(|(shard, _)| *shard).collect();
            self.annotate(py, raised, call, removed, &failed)
        })
    }

    /// `raised` with `failed_shards`, `removed` (when given) and a note naming them.
    fn annotate(&self, py: Python<'_>, raised: PyErr, call: &str, removed: Option<u64>, failed: &[usize]) -> PyErr {
        let listed = failed.iter().map(ToString::to_string).collect::<Vec<_>>().join(", ");
        let noun = if failed.len() == 1 { "shard" } else { "shards" };
        let shards = self.shards.len();
        let note = match removed {
            Some(removed) => {
                format!(
                    "FanoutCache.{call}() failed on {noun} {listed} of {shards}; it removed {removed} entries in all"
                )
            }
            None => format!("FanoutCache.{call}() failed on {noun} {listed} of {shards}"),
        };
        let value = raised.value(py);
        let annotated = PyTuple::new(py, failed)
            .and_then(|shards| value.setattr("failed_shards", shards))
            .and_then(|()| removed.map_or(Ok(()), |removed| value.setattr("removed", removed)))
            .and_then(|()| value.call_method1("add_note", (note,)).map(|_| ()));
        match annotated {
            Ok(()) => raised,
            Err(error) => error,
        }
    }
}

/// Keys per shard page in an `iterkeys` merge, which keeps one page per shard buffered at once:
/// [`PAGE`] split across the shards, but at least `MIN_MERGE_PAGE`, so a shard's page is still
/// worth a read. 64 at the default 8 shards; 32 at 256 shards, about 8,000 keys in all.
fn merge_page(shards: usize) -> i64 {
    const MIN_MERGE_PAGE: i64 = 32;
    (PAGE / i64::try_from(shards).unwrap_or(i64::MAX).max(1)).max(MIN_MERGE_PAGE)
}

/// A lazy iterator over a `FanoutCache`'s keys, one page per fetch, holding no connection between
/// `next()` calls (D-025). Like `Cache`'s, it isn't a snapshot: a key stored or deleted while it
/// runs may or may not be seen, but no key is yielded twice and, for `iterkeys`, every key comes
/// out in order.
#[pyclass(frozen, module = "diskcache_oxide")]
pub(crate) struct FanoutKeyIterator {
    cache: Py<FanoutCache>,
    prefix: Option<String>,
    walk: Mutex<Walk>,
}

/// Where a [`FanoutKeyIterator`] is.
enum Walk {
    /// `__iter__`: shard `shard` in insertion order, then the next shard from its start.
    Shards { shard: usize, cursor: Cursor, buffer: VecDeque<String> },
    /// `iterkeys`: a k-way merge. `heads` holds each shard's next key, and `shards[i]` the keys
    /// buffered behind it. A shard in `pending` has nothing buffered: it is read at the start of the
    /// next `next()`, before any key is chosen, so an error there loses nothing and can be retried.
    Merge { reverse: bool, heads: BinaryHeap<Head>, shards: Vec<MergeShard>, pending: Vec<usize> },
}

impl Walk {
    fn merge(shards: usize, reverse: bool) -> Self {
        Self::Merge {
            reverse,
            heads: BinaryHeap::with_capacity(shards),
            shards: (0..shards).map(|_| MergeShard { cursor: Cursor::Start, buffer: VecDeque::new() }).collect(),
            // Every shard is read on the first `next()`, shard 0 first.
            pending: (0..shards).rev().collect(),
        }
    }
}

/// One shard's place in an `iterkeys` merge.
struct MergeShard {
    cursor: Cursor,
    buffer: VecDeque<String>,
}

/// A shard's next key in the merge. The heap's greatest is the next key out: the smallest key
/// ascending, the largest in reverse. Keys are compared as Rust strings, which is byte order on
/// UTF-8, the order SQLite's `BINARY` collation gives each shard's pages (and code-point order, as
/// `sorted()` gives). One key lives in one shard, so two heads are never equal.
#[derive(PartialEq, Eq)]
struct Head {
    key: String,
    shard: usize,
    reverse: bool,
}

impl Ord for Head {
    fn cmp(&self, other: &Self) -> KeyOrdering {
        let ascending = other.key.cmp(&self.key).then(other.shard.cmp(&self.shard));
        if self.reverse { ascending.reverse() } else { ascending }
    }
}

impl PartialOrd for Head {
    fn partial_cmp(&self, other: &Self) -> Option<KeyOrdering> {
        Some(self.cmp(other))
    }
}

impl FanoutKeyIterator {
    fn new(cache: Py<FanoutCache>, walk: Walk, prefix: Option<String>) -> Self {
        Self { cache, prefix, walk: Mutex::new(walk) }
    }

    /// The next page of `shard`'s keys after `cursor`, waiting no later than `deadline`.
    fn page(
        &self,
        shard: usize,
        size: i64,
        order: Order,
        cursor: &Cursor,
        deadline: Instant,
    ) -> Result<(Vec<String>, Cursor), Error> {
        let handle = &self.cache.get().shards[shard];
        let left = deadline.saturating_duration_since(Instant::now());
        let left = handle.ensure_open(left)?;
        handle.store.keys_page_of(size, left, order, self.prefix.as_deref(), cursor)
    }
}

#[pymethods]
impl FanoutKeyIterator {
    /// Lets the garbage collector see the cache it holds, as `Cache`'s `KeyIterator` does.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.cache)
    }

    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    /// The next key. The reads one `next()` makes (every shard's first page, on the first call of a
    /// merge) share one deadline of the cache's `timeout`.
    fn __next__(&self) -> PyResult<Option<String>> {
        // As `Cache`'s iterator: never wait for the state while attached (the gate's rules), and
        // another thread inside `next()` is an error, as for a generator.
        let mut walk = match self.walk.try_lock() {
            Ok(walk) => walk,
            Err(TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
            Err(TryLockError::WouldBlock) => return Err(PyValueError::new_err("iterator already executing")),
        };
        let timeout = self.cache.get().config.timeout;
        let deadline = lockwait::deadline_after(Instant::now(), timeout);
        let whole = |error| match error {
            Error::Timeout { .. } => to_py_err(Error::Timeout { seconds: timeout.as_secs_f64() }),
            other => to_py_err(other),
        };
        match &mut *walk {
            Walk::Shards { shard, cursor, buffer } => loop {
                if let Some(key) = buffer.pop_front() {
                    return Ok(Some(key));
                }
                if *shard >= self.cache.get().shards.len() {
                    return Ok(None);
                }
                let (keys, next) = self.page(*shard, PAGE, Order::Inserted, cursor, deadline).map_err(whole)?;
                if keys.is_empty() {
                    *shard += 1;
                    *cursor = Cursor::Start;
                } else {
                    *cursor = next;
                    buffer.extend(keys);
                }
            },
            Walk::Merge { reverse, heads, shards, pending } => {
                let size = merge_page(shards.len());
                let order = Order::Key { reverse: *reverse };
                while let Some(&shard) = pending.last() {
                    let state = &mut shards[shard];
                    let (keys, next) = self.page(shard, size, order, &state.cursor, deadline).map_err(whole)?;
                    let mut keys = VecDeque::from(keys);
                    if let Some(key) = keys.pop_front() {
                        state.cursor = next;
                        state.buffer = keys;
                        heads.push(Head { key, shard, reverse: *reverse });
                    }
                    // An empty page: this shard is done, and leaves the merge.
                    pending.pop();
                }
                let Some(Head { key, shard, .. }) = heads.pop() else { return Ok(None) };
                match shards[shard].buffer.pop_front() {
                    Some(next) => heads.push(Head { key: next, shard, reverse: *reverse }),
                    None => pending.push(shard),
                }
                Ok(Some(key))
            }
        }
    }
}

impl Drop for FanoutCache {
    /// A cache dropped without `close()`: releases every shard still open, as `close()` does but
    /// with no checkpoint, detached on a large WAL as `Cache`'s drop is.
    fn drop(&mut self) {
        let open: Vec<&std::sync::Arc<Store>> =
            self.shards.iter().filter(|h| !h.closed.swap(true, Ordering::AcqRel)).map(|h| &h.store).collect();
        if open.is_empty() {
            return;
        }
        let detached = open.iter().any(|s| s.closing_on_a_large_wal())
            && Python::try_attach(|py| crate::detach(py, || Store::close_all(&open, None))).is_some();
        if !detached {
            Store::close_all(&open, None);
        }
    }
}

/// About how many file descriptors opening one shard takes: its pin, the connection the open
/// seeds the pool with (the database and its `-wal`), and the shared `-shm`. Each further pooled
/// connection a shard opens in use adds two more.
const FDS_PER_SHARD: u64 = 4;

/// The exception a failed open raises. An I/O failure opening a shard when this process can't
/// have that many more files open (`ulimit -n`) is almost always that limit, which SQLite reports
/// only as "unable to open database file": say so, with the numbers, and keep SQLite's as the
/// cause. The check runs after the failed open has released its shards, so it compares the
/// limit with what is open now plus what the shards need, not with a count taken mid-failure.
fn open_error(py: Python<'_>, error: Error, shards: Option<u32>) -> PyErr {
    let Some(message) = shards.and_then(|shards| fd_shortfall(&error, shards)) else { return to_py_err(error) };
    let raised = pyo3::exceptions::PyOSError::new_err((libc::EMFILE, message));
    raised.set_cause(py, Some(to_py_err(error)));
    raised
}

/// The message for an open that failed for want of file descriptors, or `None` if that's not it.
fn fd_shortfall(error: &Error, shards: u32) -> Option<String> {
    let io = match error {
        Error::SqliteIo { .. } => true,
        Error::Io { source, .. } => matches!(source.raw_os_error(), Some(libc::EMFILE | libc::ENFILE)),
        _ => false,
    };
    if !io {
        return None;
    }
    let soft = soft_fd_limit()?;
    let open = open_fds()?;
    let need = FDS_PER_SHARD * u64::from(shards);
    (open + need > soft).then(|| {
        format!(
            "a FanoutCache with {shards} shards needs about {need} file descriptors to open ({FDS_PER_SHARD} per \
             shard, and 2 more per connection each shard opens in use), but this process may have only {soft} \
             open (its soft RLIMIT_NOFILE, `ulimit -n`) and already has {open}; use fewer shards, or raise the \
             limit (`ulimit -n`, or `resource.setrlimit(resource.RLIMIT_NOFILE, ...)`)"
        )
    })
}

/// This process's soft limit on open files, if it has a finite one.
#[allow(unsafe_code)]
fn soft_fd_limit() -> Option<u64> {
    let mut limit = libc::rlimit { rlim_cur: 0, rlim_max: 0 };
    // SAFETY: `getrlimit` only writes the struct it is given, which lives for the call.
    let ok = unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &raw mut limit) } == 0;
    (ok && limit.rlim_cur != libc::RLIM_INFINITY).then_some(limit.rlim_cur)
}

/// How many file descriptors this process has open now (its `/dev/fd` entries).
fn open_fds() -> Option<u64> {
    let dir = if cfg!(target_os = "linux") { "/proc/self/fd" } else { "/dev/fd" };
    std::fs::read_dir(dir).ok().map(|entries| entries.count() as u64)
}

/// The `shards=` argument: `None` (the stored count, or 8 for a new root) or an int from 1 to 256.
fn shard_count(shards: Option<&Bound<'_, PyAny>>) -> PyResult<Option<u32>> {
    let Some(n) = whole("shards", shards)? else { return Ok(None) };
    match u32::try_from(n) {
        Ok(n) if (fanout::MIN_SHARDS..=fanout::MAX_SHARDS).contains(&n) => Ok(Some(n)),
        _ => Err(PyValueError::new_err(format!(
            "shards must be between {} and {}, got {n}",
            fanout::MIN_SHARDS,
            fanout::MAX_SHARDS
        ))),
    }
}

/// Opens the root's manifest, then every shard under one deadline of the handle's `timeout`. With
/// no `max_connections`, the total is one per shard, and never fewer than a plain cache's default.
/// A failure comes with the shard count once the manifest has given it, for [`open_error`].
fn open(
    root: &std::path::Path,
    settings: &Settings,
    shards: Option<u32>,
) -> Result<Vec<std::sync::Arc<Store>>, (Error, Option<u32>)> {
    let deadline = lockwait::deadline_after(Instant::now(), settings.timeout);
    let manifest: Manifest = fanout::open_manifest(root, shards).map_err(|e| (e, None))?;
    let total = settings.max_connections.unwrap_or_else(|| Store::default_total_connections(manifest.shards()));
    // The role is set per shard by `open_shards`.
    let base = settings.options(root.to_path_buf(), total, Role::Plain);
    Store::open_shards(&base, manifest, deadline).map_err(|error| match error {
        Error::Timeout { .. } => (Error::Timeout { seconds: settings.timeout.as_secs_f64() }, None),
        other => (other, Some(manifest.shards())),
    })
}
