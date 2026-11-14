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
use std::sync::{Mutex, MutexGuard, PoisonError, Weak};

use crate::gate::SharedGuard;
use crate::store::Store;
use schedule::{Schedule, Tick};
pub use worker::{fail_next_turns_for_tests, pass_panics, spawn_failures};

/// The store registry's guard, which is only ever held under a shared gate level: the proof a
/// function that locks a store's [`Tending`] under it needs.
pub(crate) type Registry<'a> = MutexGuard<'a, Vec<Weak<Store>>>;

/// What a handle asks of background maintenance when it opens a store.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Maintenance {
    /// How often, in milliseconds; 0 asks for none. The store follows the shortest any open handle
    /// on it asked for.
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
    /// The interval maintenance runs at on this store in this process, in milliseconds: 0 when it's
    /// off, or vetoed by a handle with its own clock, or behind a pending fork.
    pub interval_ms: u64,
    /// Turns that ran.
    pub turns: u64,
    /// Rows the turns removed.
    pub removed: u64,
    /// Turns that failed, plus one for each streak of turns that found work and couldn't get to it
    /// for 3 intervals.
    pub failures: u64,
    /// The last failure's message. Left out behind a pending fork.
    pub last_error: Option<String>,
}

/// The interval a schedule runs at, 0 when it's off or vetoed.
fn effective_interval(schedule: &Schedule) -> u64 {
    if schedule.is_on() { schedule.interval_ms() } else { 0 }
}

/// The schedule and what the counters can't hold, kept together under one mutex.
#[derive(Debug)]
pub(crate) struct Tending {
    pub(crate) schedule: Schedule,
    /// The last failure's message.
    pub(crate) last_error: Option<String>,
    /// Since when, on the monotonic clock, turns have found work and removed nothing because they
    /// were refused or stepped aside (decision 16): None while turns get through.
    pub(crate) starving_since: Option<u64>,
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
    pub(crate) turns_in_flight: crate::genlock::Slots,
    /// The claim time this process last wrote for each claim row ([`turn::Claim`]), as `f64` bits (0:
    /// none). A claim row holding exactly this value is this process's own, so a turn continuing its
    /// own work a second later doesn't mistake it for another process's.
    own_claims: [AtomicU64; 2],
    /// The schedule and the last failure. Locked only by a thread holding a shared gate level (the
    /// registry counts, being held only under one), never across a turn and never while another
    /// store's is held, so no thread holds it when a fork takes the gate exclusively.
    tending: Mutex<Tending>,
    /// [`Health`]'s counters, readable with no gate level (behind a pending fork too).
    interval_ms: AtomicU64,
    turns: AtomicU64,
    removed: AtomicU64,
    failures: AtomicU64,
    /// A failure is waiting to be logged by the next Python call on the cache: one relaxed load on
    /// the request path (decision 16).
    warning: AtomicBool,
}

impl Upkeep {
    pub(crate) fn new(schedule: Schedule) -> Self {
        Self {
            turns_in_flight: crate::genlock::Slots::new(),
            own_claims: [AtomicU64::new(0), AtomicU64::new(0)],
            interval_ms: AtomicU64::new(effective_interval(&schedule)),
            tending: Mutex::new(Tending { schedule, last_error: None, starving_since: None, warned: false }),
            turns: AtomicU64::new(0),
            removed: AtomicU64::new(0),
            failures: AtomicU64::new(0),
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

    /// The schedule and last failure, on a gate level the caller holds.
    pub(crate) fn tending(&self, _gate: &SharedGuard<'_>) -> MutexGuard<'_, Tending> {
        self.tending.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The same, under the store registry, which is only ever held under a gate level.
    pub(crate) fn tending_under(&self, _registry: &Registry<'_>) -> MutexGuard<'_, Tending> {
        self.tending.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Whether [`Upkeep::ask`] would place the store among the others, so the caller must gather
    /// their spread first: never while holding this store's mutex.
    pub(crate) fn needs_placement(&self, registry: &Registry<'_>, asked: Maintenance) -> bool {
        asked.interval_ms != 0 && self.tending_under(registry).schedule.interval_ms() == 0
    }

    /// This store's place in the spread, for placing another.
    pub(crate) fn spread(&self, registry: &Registry<'_>) -> Option<Tick> {
        self.tending_under(registry).schedule.spread()
    }

    /// Merges what a handle being counted asks for into the schedule, under the registry, in the same
    /// step that counts it (decisions 3, 7 and 15), so nothing (a busy gate, a turn due in between) can
    /// come between a handle and its veto. On a revive (`revived`), the interval becomes this handle's
    /// and the veto starts over, since no earlier handle is open (Michael, 2026-11-13). `others` is
    /// the other stores' spread, needed only when [`Upkeep::needs_placement`] said so. Returns
    /// whether the thread should be woken: the next turn came earlier, or the store was revived.
    pub(crate) fn ask(
        &self,
        registry: &Registry<'_>,
        now_ms: u64,
        asked: Maintenance,
        revived: bool,
        others: Vec<Tick>,
    ) -> bool {
        let mut tending = self.tending_under(registry);
        let before = tending.schedule.next_ms();
        if revived {
            tending.schedule.revive(now_ms, asked.interval_ms, others);
            tending.starving_since = None;
        } else {
            tending.schedule.join(now_ms, asked.interval_ms, others);
        }
        if asked.own_clock {
            tending.schedule.veto();
        }
        self.interval_ms.store(effective_interval(&tending.schedule), Ordering::Relaxed);
        let after = tending.schedule.next_ms();
        // A revived store wakes the thread whatever its tick: while the store was retired the thread
        // left it out of its next wake, and may be parked with nothing else to do.
        after.is_some() && (revived || before.is_none_or(|before| after.is_some_and(|after| after < before)))
    }

    /// How maintenance has gone. The counters and interval need no lock; the last error is left out
    /// behind a pending fork.
    pub(crate) fn health(&self) -> Health {
        let last_error = crate::gate::GATE.try_enter_shared().and_then(|gate| self.tending(&gate).last_error.clone());
        Health {
            interval_ms: self.interval_ms.load(Ordering::Relaxed),
            turns: self.turns.load(Ordering::Relaxed),
            removed: self.removed.load(Ordering::Relaxed),
            failures: self.failures.load(Ordering::Relaxed),
            last_error,
        }
    }

    pub(crate) fn count_turn(&self, removed: u64) {
        self.turns.fetch_add(1, Ordering::Relaxed);
        self.removed.fetch_add(removed, Ordering::Relaxed);
    }

    /// Counts a failure, keeps its message, and raises the warning once per streak.
    pub(crate) fn fail(&self, tending: &mut Tending, message: String) {
        self.failures.fetch_add(1, Ordering::Relaxed);
        tending.last_error = Some(message);
        if !tending.warned {
            tending.warned = true;
            self.warning.store(true, Ordering::Release);
        }
    }

    /// In a forked child: the parent's claims, health and pending warning aren't this process's.
    pub(crate) fn reset_after_fork(&self, gate: &SharedGuard<'_>) {
        for claim in &self.own_claims {
            claim.store(0, Ordering::Release);
        }
        for counter in [&self.turns, &self.removed, &self.failures] {
            counter.store(0, Ordering::Relaxed);
        }
        self.warning.store(false, Ordering::Relaxed);
        let mut tending = self.tending(gate);
        tending.last_error = None;
        tending.starving_since = None;
        tending.warned = false;
    }

    /// Takes the pending warning, if a failure raised one since the last take.
    pub(crate) fn take_warning(&self) -> bool {
        self.warning.load(Ordering::Relaxed) && self.warning.swap(false, Ordering::AcqRel)
    }

    pub(crate) fn raise_warning(&self) {
        self.warning.store(true, Ordering::Release);
    }
}
