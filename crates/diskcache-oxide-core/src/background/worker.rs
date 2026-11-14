//! The maintenance thread (plan 28, decisions 1, 2, 7, 8, 14 and 16): one per process, started the
//! first time a store wants maintenance, serving every store in the registry.
//!
//! - It's a Rust thread, marked native, so it never touches the interpreter
//!   ([`gate::mark_native_thread`]), and it holds no gate level while a turn runs.
//! - Each wake it collects the stores under the gate, asks each schedule whether its turn is due,
//!   lets go of the gate, runs the due turns, then records them and works out the next wake.
//! - It parks until the next turn is due, or with no timeout when no store wants maintenance, so an
//!   idle process costs nothing. A store whose schedule turns on wakes it with `unpark`. It never
//!   exits: one parked thread is cheaper and simpler than a stop-and-restart protocol that would
//!   have to decide, under the registry, that no store wants it.
//! - Its handle lives in a slot locked only under the store registry, tagged with the process
//!   generation: a forked child (which has no thread) sees none and starts its own, from the
//!   after-fork hook if a store wants maintenance there, or from the next store that does.
//! - A spawn that fails (a container's thread limit) leaves maintenance off and is counted; the next
//!   store to want it tries again. A panic in a turn or a pass is caught, counted as a failure, and
//!   the thread carries on.

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, PoisonError, Weak};
use std::thread::Thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use super::Tending;
use super::schedule::TaskSet;
use super::turn::{Stop, TurnReport};
use crate::gate::{self, GATE};
use crate::store::Store;

const NAME: &str = "diskcache-oxide-maintenance";

/// How long the thread waits before trying again when a pending fork refuses it the gate.
const BEHIND_A_FORK: Duration = Duration::from_millis(10);

/// After a panic in a pass, the thread waits this long before the next, so a bug can't spin it.
const AFTER_A_PANIC: Duration = Duration::from_secs(1);

/// A store that has had work waiting this many intervals without a turn getting through to it
/// counts a failure (decision 16): another connection keeps the write lock.
const STARVED_INTERVALS: u64 = 3;

/// The thread's handle and the process generation it was started in. Locked only under the store
/// registry, so a fork (which takes the gate exclusively) never finds it held.
static THREAD: Mutex<Option<(u64, Thread)>> = Mutex::new(None);

static SPAWN_FAILURES: AtomicU64 = AtomicU64::new(0);

/// How many times starting the maintenance thread failed in this process (decision 1).
#[must_use]
pub fn spawn_failures() -> u64 {
    SPAWN_FAILURES.load(Ordering::Relaxed)
}

/// Milliseconds on the monotonic clock since this process first asked: the schedule's time.
pub(crate) fn mono_ms() -> u64 {
    static ORIGIN: OnceLock<Instant> = OnceLock::new();
    u64::try_from(ORIGIN.get_or_init(Instant::now).elapsed().as_millis()).unwrap_or(u64::MAX)
}

/// The wall time in seconds, as the cache's rows record expiry: what a turn removes by.
fn wall_now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

/// Makes sure this process's thread is running and wakes it, after a store's schedule turned on or
/// came forward. Called under the store registry, whose guard is the proof.
pub(crate) fn ensure_running(_registry: &MutexGuard<'_, Vec<Weak<Store>>>) {
    let mut slot = THREAD.lock().unwrap_or_else(PoisonError::into_inner);
    let generation = gate::generation();
    if let Some((started_in, thread)) = &*slot
        && *started_in == generation
    {
        thread.unpark();
        return;
    }
    if let Ok(handle) = std::thread::Builder::new().name(NAME.into()).spawn(run) {
        *slot = Some((generation, handle.thread().clone()));
    } else {
        SPAWN_FAILURES.fetch_add(1, Ordering::Relaxed);
        *slot = None;
    }
}

/// Checks that no thread held the slot at fork time; see `pool::quiesce_all`.
pub(crate) fn quiesce() {
    let busy = matches!(THREAD.try_lock(), Err(std::sync::TryLockError::WouldBlock));
    debug_assert!(!busy, "the maintenance thread's slot was locked at fork time");
    if busy {
        drop(THREAD.lock());
    }
}

/// The after-fork hook in the child: forgets the parent's claims, and starts the child's own thread
/// if any store wants maintenance. The parent's thread didn't come across.
pub fn after_fork_in_child() {
    let Some(gate) = GATE.try_enter_shared() else { return };
    let stores = Store::all_in_registry(&gate);
    let mut wanted = false;
    for store in &stores {
        store.upkeep().forget_own_claims();
        wanted |= !store.is_retired() && store.upkeep().tending(&gate).schedule.is_on();
    }
    if wanted {
        Store::with_registry(&gate, ensure_running);
    }
    // The `Arc`s go here, under the level and outside every mutex.
    drop(stores);
}

fn run() {
    gate::mark_native_thread();
    loop {
        match catch_unwind(AssertUnwindSafe(pass)) {
            Ok(Some(wait)) => std::thread::park_timeout(wait),
            Ok(None) => std::thread::park(),
            Err(_) => std::thread::park_timeout(AFTER_A_PANIC),
        }
    }
}

/// One wake: every due store's turn. Returns how long until the next turn is due, or None if no store
/// wants maintenance.
fn pass() -> Option<Duration> {
    let Some(gate) = GATE.try_enter_shared() else { return Some(BEHIND_A_FORK) };
    let now = mono_ms();
    let stores = Store::all_in_registry(&gate);
    let mut due = Vec::new();
    for store in &stores {
        let mut tending = store.upkeep().tending(&gate);
        if store.is_retired() {
            // Kept on its grid while retired, so a revive doesn't meet a long-overdue turn.
            if tending.schedule.next_ms().is_some_and(|next| next <= now) {
                tending.schedule.skip(now);
            }
            continue;
        }
        if let Some(tasks) = tending.schedule.begin(now) {
            due.push((Arc::clone(store), tasks, tending.schedule.interval_ms()));
        }
    }
    // Under the level, outside every mutex: a last `Arc` closes files.
    drop(stores);
    drop(gate);

    let reports: Vec<_> = due
        .iter()
        .map(|(store, tasks, interval_ms)| {
            catch_unwind(AssertUnwindSafe(|| {
                store.maintenance_turn(*tasks, wall_now(), Duration::from_millis(*interval_ms))
            }))
        })
        .collect();

    // Back under the gate to record the turns and find the next wake. A fork that arrived meanwhile
    // goes first; this thread holds nothing it waits for.
    let gate = loop {
        if let Some(gate) = GATE.try_enter_shared() {
            break gate;
        }
        std::thread::sleep(BEHIND_A_FORK);
    };
    let now = mono_ms();
    for ((store, _, interval_ms), report) in due.iter().zip(reports) {
        record(store, &mut store.upkeep().tending(&gate), now, *interval_ms, report);
    }
    drop(due);
    let stores = Store::all_in_registry(&gate);
    let next = stores
        .iter()
        .filter(|store| !store.is_retired())
        .filter_map(|store| store.upkeep().tending(&gate).schedule.next_ms())
        .min();
    drop(stores);
    drop(gate);
    next.map(|next| Duration::from_millis(next.saturating_sub(mono_ms())))
}

/// Records a turn on its store's schedule and health (decision 16).
fn record(
    store: &Store,
    tending: &mut Tending,
    now: u64,
    interval_ms: u64,
    report: std::thread::Result<Option<TurnReport>>,
) {
    let report = match report {
        Ok(Some(report)) => report,
        // No turn after all: the store retired, a fork arrived, or it's poisoned.
        Ok(None) => {
            tending.schedule.skip(now);
            return;
        }
        Err(_) => {
            tending.schedule.finish(now, TaskSet::EMPTY, false);
            fail(store, tending, "a background maintenance turn panicked".into());
            return;
        }
    };
    tending.schedule.finish(now, report.done, report.cut_short());
    tending.health.turns += 1;
    tending.health.removed += report.removed;
    let starved = report.work_seen && matches!(report.stopped, Some(Stop::Refused | Stop::Yielded));
    if let Some(error) = report.error {
        fail(store, tending, error.to_string());
    } else if !starved {
        tending.last_progress_ms = now;
        tending.warned = false;
    } else if now.saturating_sub(tending.last_progress_ms) >= STARVED_INTERVALS.saturating_mul(interval_ms) {
        let seconds = now.saturating_sub(tending.last_progress_ms) / 1_000;
        fail(
            store,
            tending,
            format!(
                "background maintenance has found work on this cache for {seconds} s without getting the write \
                 lock: another connection keeps it"
            ),
        );
    }
}

/// Counts a failure, keeps its message, and raises the warning once per streak.
fn fail(store: &Store, tending: &mut Tending, message: String) {
    tending.health.failures += 1;
    tending.health.last_error = Some(message);
    if !tending.warned {
        tending.warned = true;
        store.upkeep().raise_warning();
    }
}

#[cfg(test)]
mod tests;
