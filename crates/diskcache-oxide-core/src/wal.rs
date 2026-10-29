//! Keeping the WAL bounded (`wal_size_limit`, D-024; plans/06-phase-2.5-concurrency-review.md, step 1).
//!
//! SQLite's automatic checkpoint is passive. It copies WAL frames into the database but never
//! waits, and the WAL can only wrap back to its start when no reader still uses it. With readers in
//! other processes, a burst of writes (a fill, a `clear()`) therefore grows the file to fit the
//! burst, and SQLite never shrinks it: the soak, and a 4-process reproduction, left WALs of
//! 300 MiB to 1 GiB behind.
//!
//! Three settings and one action keep it bounded. All three settings are connection settings, so
//! they write nothing (D-019):
//! - `journal_size_limit` = half the limit: a WAL that resets normally is truncated back to it.
//! - `wal_autocheckpoint`, scaled from the limit (at most SQLite's 1,000 pages), so that a healthy
//!   WAL stays well under the trigger.
//! - After a committed write, at most every [`CHECK_EVERY`] or [`CHECK_EVERY_COMMITS`], the
//!   `-wal` file is `stat`ed. If it's over the limit, a forced checkpoint runs with the interpreter
//!   released: `PRAGMA wal_checkpoint(PASSIVE)` (copy what it can, no waiting), then
//!   `wal_checkpoint(TRUNCATE)` under a fresh busy window ([`crate::lockwait::ATTACHED_WINDOW`]),
//!   which waits for in-flight readers (ours are microseconds long), copies the remainder and
//!   truncates the file. If it reports busy, the store backs off for [`RETRY_AFTER_BUSY`]. The
//!   counters' pause times measure the SQLite work only, not re-taking the interpreter.
//!
//! The check runs after the write's retry loop has returned, still holding the in-process write
//! lock, and never for a write at gate depth > 0, inside a held transaction, or through
//! `write_nowait`. A checkpoint that fails or reports busy never fails the write: it has already
//! committed. Outcomes go to the counters in [`WalStats`].

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

/// Default `wal_size_limit` (Michael, 2026-10-28).
pub const DEFAULT_SIZE_LIMIT: u64 = 64 * 1024 * 1024;

/// Smallest accepted `wal_size_limit`.
pub const MIN_SIZE_LIMIT: u64 = 1024 * 1024;

/// How often, at most, a store `stat`s its WAL after a write...
pub const CHECK_EVERY: Duration = Duration::from_millis(250);
/// ...or after this many writes, whichever comes first, so a fast burst is caught early.
pub const CHECK_EVERY_COMMITS: u64 = 64;
/// After a busy or failed forced checkpoint, the store waits this long before the next attempt.
pub const RETRY_AFTER_BUSY: Duration = Duration::from_secs(1);

/// A forced checkpoint at least this long counts as slow in [`WalStats::slow`].
pub const SLOW_PAUSE: Duration = Duration::from_millis(100);

/// SQLite's own default `wal_autocheckpoint`, in pages.
const SQLITE_AUTOCHECKPOINT: u64 = 1_000;
/// Bytes per WAL frame on disk: one page plus the 24-byte frame header. Assumes SQLite's default
/// 4,096-byte page, which the schema never changes.
const FRAME_BYTES: u64 = 4_096 + 24;

/// `PRAGMA journal_size_limit` for a `wal_size_limit`: half of it, strictly under the trigger, so a
/// normally reset WAL never looks over the limit.
#[must_use]
pub const fn journal_size_limit(limit: u64) -> u64 {
    limit / 2
}

/// `PRAGMA wal_autocheckpoint` (pages) for a `wal_size_limit`: a quarter of the limit in frames,
/// capped at SQLite's default, so a healthy WAL stays far under the trigger.
#[must_use]
pub fn autocheckpoint_pages(limit: u64) -> u64 {
    (limit / FRAME_BYTES / 4).clamp(16, SQLITE_AUTOCHECKPOINT)
}

/// What forced checkpoints have done on one store (a test and debug view).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct WalStats {
    /// `-wal` file checks.
    pub checks: u64,
    /// Forced checkpoints attempted.
    pub attempts: u64,
    /// ...that completed (the WAL was reset and truncated).
    pub successes: u64,
    /// ...that reported busy (another checkpointer, or a reader or writer that outlasted the window).
    pub busy: u64,
    /// ...that failed with an error (or couldn't get a connection).
    pub errors: u64,
    /// ...that were skipped because every connection slot was in use.
    pub no_slot: u64,
    /// The longest forced checkpoint (both steps), in microseconds.
    pub max_pause_us: u64,
    /// The longest PASSIVE step (copying frames without waiting), in microseconds.
    pub max_passive_us: u64,
    /// The longest TRUNCATE step (waiting for readers, the remainder, the truncation), in microseconds.
    pub max_truncate_us: u64,
    /// Forced checkpoints that took at least [`SLOW_PAUSE`].
    pub slow: u64,
    /// The `-wal` file size at the last check, in bytes.
    pub last_wal_bytes: u64,
}

/// A store's checkpoint bookkeeping. Times are milliseconds since `created`, on the monotonic clock,
/// which carries over `fork()` unchanged, so inherited values stay meaningful in a child. Writers
/// reach it holding the in-process write lock, so they're serialized and plain loads and stores
/// are enough.
#[derive(Debug)]
pub(crate) struct WalState {
    created: Instant,
    commits_since_check: AtomicU64,
    next_check_ms: AtomicU64,
    next_attempt_ms: AtomicU64,
    checks: AtomicU64,
    attempts: AtomicU64,
    successes: AtomicU64,
    busy: AtomicU64,
    errors: AtomicU64,
    no_slot: AtomicU64,
    max_pause_us: AtomicU64,
    max_passive_us: AtomicU64,
    max_truncate_us: AtomicU64,
    slow: AtomicU64,
    last_wal_bytes: AtomicU64,
}

/// What a check after a write decided.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Decision {
    /// Not time to look yet, or the WAL is within the limit.
    Nothing,
    /// The WAL is over the limit: run a forced checkpoint now.
    Checkpoint,
}

/// How a forced checkpoint went.
#[derive(Debug)]
pub(crate) enum Outcome {
    Done,
    Busy,
    Failed,
    /// No connection slot was free; skipped.
    NoSlot,
}

impl Default for WalState {
    fn default() -> Self {
        Self::new()
    }
}

impl WalState {
    #[must_use]
    pub fn new() -> Self {
        Self {
            created: Instant::now(),
            commits_since_check: AtomicU64::new(0),
            next_check_ms: AtomicU64::new(0),
            next_attempt_ms: AtomicU64::new(0),
            checks: AtomicU64::new(0),
            attempts: AtomicU64::new(0),
            successes: AtomicU64::new(0),
            busy: AtomicU64::new(0),
            errors: AtomicU64::new(0),
            no_slot: AtomicU64::new(0),
            max_pause_us: AtomicU64::new(0),
            max_passive_us: AtomicU64::new(0),
            max_truncate_us: AtomicU64::new(0),
            slow: AtomicU64::new(0),
            last_wal_bytes: AtomicU64::new(0),
        }
    }

    fn now_ms(&self) -> u64 {
        u64::try_from(self.created.elapsed().as_millis()).unwrap_or(u64::MAX)
    }

    /// Called after each committed write: decides whether to look at the WAL, and whether it needs
    /// a forced checkpoint. `wal_bytes` is only called when it's time to look.
    pub fn after_commit(&self, limit: u64, wal_bytes: impl FnOnce() -> u64) -> Decision {
        let commits = self.commits_since_check.fetch_add(1, Ordering::Relaxed) + 1;
        let now = self.now_ms();
        if commits < CHECK_EVERY_COMMITS && now < self.next_check_ms.load(Ordering::Relaxed) {
            return Decision::Nothing;
        }
        self.commits_since_check.store(0, Ordering::Relaxed);
        self.next_check_ms.store(now + millis(CHECK_EVERY), Ordering::Relaxed);
        self.checks.fetch_add(1, Ordering::Relaxed);
        let bytes = wal_bytes();
        self.last_wal_bytes.store(bytes, Ordering::Relaxed);
        if bytes <= limit || now < self.next_attempt_ms.load(Ordering::Relaxed) {
            return Decision::Nothing;
        }
        Decision::Checkpoint
    }

    /// True while backing off after a busy or failed attempt.
    pub(crate) fn in_backoff(&self) -> bool {
        self.now_ms() < self.next_attempt_ms.load(Ordering::Relaxed)
    }

    /// Counts a forced checkpoint attempt (automatic, explicit or on close).
    pub(crate) fn note_attempt(&self) {
        self.attempts.fetch_add(1, Ordering::Relaxed);
    }

    /// Records a forced checkpoint's outcome and how long each step took.
    pub(crate) fn record(&self, outcome: &Outcome, passive: Duration, truncate: Duration) {
        let counter = match outcome {
            Outcome::Done => &self.successes,
            Outcome::Busy => &self.busy,
            Outcome::Failed => &self.errors,
            Outcome::NoSlot => &self.no_slot,
        };
        counter.fetch_add(1, Ordering::Relaxed);
        if !matches!(outcome, Outcome::Done) {
            self.next_attempt_ms.store(self.now_ms() + millis(RETRY_AFTER_BUSY), Ordering::Relaxed);
        }
        let us = |d: Duration| u64::try_from(d.as_micros()).unwrap_or(u64::MAX);
        let took = passive + truncate;
        self.max_pause_us.fetch_max(us(took), Ordering::Relaxed);
        self.max_passive_us.fetch_max(us(passive), Ordering::Relaxed);
        self.max_truncate_us.fetch_max(us(truncate), Ordering::Relaxed);
        if took >= SLOW_PAUSE {
            self.slow.fetch_add(1, Ordering::Relaxed);
        }
    }

    #[must_use]
    pub fn stats(&self) -> WalStats {
        WalStats {
            checks: self.checks.load(Ordering::Relaxed),
            attempts: self.attempts.load(Ordering::Relaxed),
            successes: self.successes.load(Ordering::Relaxed),
            busy: self.busy.load(Ordering::Relaxed),
            errors: self.errors.load(Ordering::Relaxed),
            no_slot: self.no_slot.load(Ordering::Relaxed),
            max_pause_us: self.max_pause_us.load(Ordering::Relaxed),
            max_passive_us: self.max_passive_us.load(Ordering::Relaxed),
            max_truncate_us: self.max_truncate_us.load(Ordering::Relaxed),
            slow: self.slow.load(Ordering::Relaxed),
            last_wal_bytes: self.last_wal_bytes.load(Ordering::Relaxed),
        }
    }
}

fn millis(d: Duration) -> u64 {
    u64::try_from(d.as_millis()).unwrap_or(u64::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_derived_pragmas_keep_a_healthy_wal_under_the_trigger() {
        for limit in [MIN_SIZE_LIMIT, 4 << 20, DEFAULT_SIZE_LIMIT, 1 << 34] {
            let healthy = autocheckpoint_pages(limit) * FRAME_BYTES;
            assert!(healthy * 2 <= limit, "limit {limit}: autocheckpoint lets the WAL reach {healthy}");
            assert!(journal_size_limit(limit) < limit);
        }
        assert_eq!(autocheckpoint_pages(DEFAULT_SIZE_LIMIT), SQLITE_AUTOCHECKPOINT);
    }

    #[test]
    fn checks_are_rate_limited_by_time_or_commit_count() {
        let state = WalState::new();
        let mut looks = 0;
        for _ in 0..(CHECK_EVERY_COMMITS * 3) {
            let _ = state.after_commit(u64::MAX, || {
                looks += 1;
                0
            });
        }
        // The first write looks (nothing is scheduled yet), then every CHECK_EVERY_COMMITS-th one:
        // writes 1, 65 and 129 of 192.
        assert_eq!(looks, 3);
    }

    #[test]
    fn over_the_limit_checkpoints_and_a_busy_one_backs_off() {
        let state = WalState::new();
        assert_eq!(state.after_commit(100, || 200), Decision::Checkpoint);
        state.note_attempt();
        state.record(&Outcome::Busy, Duration::from_micros(5), Duration::ZERO);
        // Next look (forced by the commit count) is still over the limit, but inside the backoff.
        for _ in 0..CHECK_EVERY_COMMITS - 1 {
            let _ = state.after_commit(100, || 200);
        }
        assert_eq!(state.after_commit(100, || 200), Decision::Nothing);
        let seen = state.stats();
        assert_eq!((seen.attempts, seen.busy, seen.successes), (1, 1, 0));
        assert_eq!(seen.last_wal_bytes, 200);
    }

    #[test]
    fn a_successful_checkpoint_does_not_back_off() {
        let state = WalState::new();
        assert_eq!(state.after_commit(100, || 200), Decision::Checkpoint);
        state.record(&Outcome::Done, Duration::from_micros(5), Duration::ZERO);
        for _ in 0..CHECK_EVERY_COMMITS - 1 {
            let _ = state.after_commit(100, || 200);
        }
        assert_eq!(
            state.after_commit(100, || 200),
            Decision::Checkpoint,
            "still over the limit, and no backoff after a success"
        );
    }
}
