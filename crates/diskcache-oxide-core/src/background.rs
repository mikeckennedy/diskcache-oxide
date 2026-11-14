//! Background maintenance (plan 28): one thread per process that does each open store's housekeeping
//! on a schedule, so a cache that's mostly read still frees its expired rows' space, gets back under
//! its size limit, keeps its write-ahead log small and loses its stray temporary files.
//!
//! [`schedule`] is the timing, as pure functions over plain values: when each store's turn comes, and
//! which tasks a turn runs. `turn` is one turn on one store, and `worker` the thread that runs them.

pub mod schedule;
pub(crate) mod turn;
pub(crate) mod worker;

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError};

use crate::gate::SharedGuard;
use schedule::Schedule;
pub use worker::{after_fork_in_child, spawn_failures};

/// What a handle asks of background maintenance when it opens a store.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Maintenance {
    /// How often, in milliseconds; 0 asks for none. The store follows the shortest any handle on it
    /// asked for.
    pub interval_ms: u64,
    /// The handle reads time from its own clock (`clock=`): no background maintenance for the store in
    /// this process while it's open (decision 15).
    pub own_clock: bool,
}

impl Maintenance {
    /// No background maintenance: what tests and tools that open stores directly ask for.
    pub const OFF: Self = Self { interval_ms: 0, own_clock: false };

    /// The default interval (decision 4).
    pub const DEFAULT: Self = Self { interval_ms: schedule::DEFAULT_INTERVAL_MS, own_clock: false };
}

/// How a store's maintenance has gone in this process (decision 16).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Health {
    /// Turns that ran.
    pub turns: u64,
    /// Rows the turns removed.
    pub removed: u64,
    /// Turns that failed, or found work they couldn't get through to for 3 intervals.
    pub failures: u64,
    /// The last failure's message.
    pub last_error: Option<String>,
}

/// The schedule and health, kept together under one mutex.
#[derive(Debug)]
pub(crate) struct Tending {
    pub(crate) schedule: Schedule,
    pub(crate) health: Health,
    /// When a turn last got through (no error, and not refused with work waiting), on the
    /// monotonic clock; for the "3 intervals" rule.
    pub(crate) last_progress_ms: u64,
    /// A warning was raised for the current streak of failures and not yet cleared by a success.
    pub(crate) warned: bool,
}

/// A store's maintenance state in this process.
#[derive(Debug)]
pub(crate) struct Upkeep {
    /// Turns running on this store (decision 13), as a counter tagged with the process generation:
    /// a forked child, which has no maintenance thread, reads a turn the parent had in flight as none.
    /// Raised only under the registry while the store is live, so the release that retires it,
    /// decided under the same lock, knows whether to wait.
    pub(crate) turns: crate::genlock::Slots,
    /// The claim time this process last wrote for each claim row ([`turn::Claim`]), as `f64` bits (0:
    /// none). A claim row holding exactly this value is this process's own, so a turn continuing its
    /// own work a second later doesn't mistake it for another process's.
    own_claims: [AtomicU64; 2],
    /// The schedule and health. Locked only by a thread holding a shared gate level (the registry
    /// counts, being held only under one), never across a turn and never while another store's is
    /// held, so no thread holds it when a fork takes the gate exclusively.
    tending: Mutex<Tending>,
    /// A failure is waiting to be logged by the next Python call on the cache: one relaxed load on
    /// the request path (decision 16).
    warning: AtomicBool,
}

impl Upkeep {
    pub(crate) fn new(schedule: Schedule, now_ms: u64) -> Self {
        Self {
            turns: crate::genlock::Slots::new(),
            own_claims: [AtomicU64::new(0), AtomicU64::new(0)],
            tending: Mutex::new(Tending {
                schedule,
                health: Health::default(),
                last_progress_ms: now_ms,
                warned: false,
            }),
            warning: AtomicBool::new(false),
        }
    }

    pub(crate) fn own_claim(&self, claim: turn::Claim) -> Option<f64> {
        let bits = self.own_claims[claim as usize].load(Ordering::Acquire);
        (bits != 0).then(|| f64::from_bits(bits))
    }

    pub(crate) fn set_own_claim(&self, claim: turn::Claim, at: f64) {
        self.own_claims[claim as usize].store(at.to_bits(), Ordering::Release);
    }

    /// Forgets this process's own claims: a forked child must not mistake its parent's for its own.
    pub(crate) fn forget_own_claims(&self) {
        for claim in &self.own_claims {
            claim.store(0, Ordering::Release);
        }
    }

    /// The schedule and health, on a gate level the caller holds.
    pub(crate) fn tending(&self, _gate: &SharedGuard<'_>) -> MutexGuard<'_, Tending> {
        self.tending.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The schedule and health, under the store registry, which is only ever held under a gate level.
    pub(crate) fn tending_under(
        &self,
        _registry: &MutexGuard<'_, Vec<std::sync::Weak<crate::store::Store>>>,
    ) -> MutexGuard<'_, Tending> {
        self.tending.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Takes the pending warning, if a failure raised one since the last take.
    pub(crate) fn take_warning(&self) -> bool {
        self.warning.load(Ordering::Relaxed) && self.warning.swap(false, Ordering::AcqRel)
    }

    pub(crate) fn raise_warning(&self) {
        self.warning.store(true, Ordering::Release);
    }
}
