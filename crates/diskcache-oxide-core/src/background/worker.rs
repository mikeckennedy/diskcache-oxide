//! The maintenance thread (plan 28, decisions 1, 2, 7, 8, 14 and 16): one per process, started the
//! first time a store wants maintenance, serving every store in the registry.
//!
//! - It's a Rust thread, marked native, so it never touches the interpreter
//!   ([`gate::mark_native_thread`]), and it holds no gate level while a turn runs.
//! - Each wake it collects the stores under the gate, asks each schedule whether its turn is due,
//!   lets go of the gate, runs the due turns, then records them and works out the next wake.
//! - It parks until the next turn is due, or with no timeout when no store wants maintenance, so an
//!   idle process costs nothing. A store whose next turn comes earlier wakes it with `unpark`.
//! - **It's stopped for every fork** (Michael, 2026-11-13). `CPython` warns on `os.fork()` in a
//!   process with more than one thread, OS threads included (checked on Linux and macOS, 3.12 to
//!   3.14), so a thread kept running would make every fork of a process with an open cache warn: a
//!   gunicorn preload master, `multiprocessing` with fork, test suites. [`stop_for_fork`] runs first
//!   in the before-fork hook: it tells the thread to stop and joins it, which waits only for the chunk
//!   or checkpoint in progress (a fork waits for those anyway). After the fork the parent starts a new
//!   one if a store wants it ([`resume_after_fork`]), and so does the child, which also drops the
//!   parent's claims and health.
//! - Its handle lives in a slot locked only under the store registry, tagged with the process
//!   generation, so a child never mistakes the parent's for its own.
//! - A spawn that fails (a container's thread limit) leaves maintenance off and is counted; the next
//!   store to want it tries again. A panic in a turn is counted as that store's failure, one in a
//!   whole pass is counted process-wide; either way the thread carries on.
//! - Signals: like any Python thread it leaves the signal mask open. Blocking them would take
//!   `pthread_sigmask`, so `unsafe`, for no difference from what Python's own threads already do.

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock, PoisonError};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use super::schedule::TaskSet;
use super::turn::{Stop, TurnReport};
use super::{Registry, Tending, Upkeep};
use crate::gate::{self, GATE};
use crate::store::Store;

const NAME: &str = "diskcache-oxide-maintenance";

/// The first wait before trying again when a pending fork refuses the thread the gate; it doubles up
/// to a second, so a fork claim left stuck can't keep the thread busy.
const BEHIND_A_FORK: Duration = Duration::from_millis(10);
const BEHIND_A_FORK_MAX: Duration = Duration::from_secs(1);

/// After a panic in a pass, the thread waits this long before the next, so a bug can't spin it.
const AFTER_A_PANIC: Duration = Duration::from_secs(1);

/// A store whose turns have found work and removed nothing for this many intervals counts a failure
/// (decision 16): another connection keeps the write lock.
const STARVED_INTERVALS: u64 = 3;

/// The thread's handle and the process generation it was started in. Locked only under the store
/// registry, so a fork (which takes the gate exclusively) never finds it held.
static THREAD: Mutex<Option<(u64, JoinHandle<()>)>> = Mutex::new(None);

/// Forks in progress that stopped the thread. While it's above zero the thread stops at its next
/// check and nothing starts a new one.
static STOPPED_FOR_FORK: AtomicUsize = AtomicUsize::new(0);

static SPAWN_FAILURES: AtomicU64 = AtomicU64::new(0);

/// Turns run and rows removed in this process, every store's, for test runs that must prove
/// maintenance actually ran beside them (plan 28's gate).
static TOTAL_TURNS: AtomicU64 = AtomicU64::new(0);
static TOTAL_REMOVED: AtomicU64 = AtomicU64::new(0);

/// `(turns, rows removed)` by background maintenance in this process, every store's.
#[must_use]
pub fn totals() -> (u64, u64) {
    (TOTAL_TURNS.load(Ordering::Relaxed), TOTAL_REMOVED.load(Ordering::Relaxed))
}

/// Turns left to fail on purpose, for tests in other crates that check failures are reported.
static FAIL_NEXT_TURNS: AtomicU64 = AtomicU64::new(0);

/// Makes the next `turns` maintenance turns in this process fail, as a turn whose database refused
/// it would. For the Python layer's tests of failure reporting; never called by the library.
#[doc(hidden)]
pub fn fail_next_turns_for_tests(turns: u64) {
    FAIL_NEXT_TURNS.store(turns, Ordering::Release);
}

/// Takes one injected failure, if any are left.
fn injected_failure() -> bool {
    FAIL_NEXT_TURNS.try_update(Ordering::AcqRel, Ordering::Acquire, |left| left.checked_sub(1)).is_ok()
}
static PASS_PANICS: AtomicU64 = AtomicU64::new(0);

/// How many times starting the maintenance thread failed in this process (decision 1).
#[must_use]
pub fn spawn_failures() -> u64 {
    SPAWN_FAILURES.load(Ordering::Relaxed)
}

/// How many of the thread's passes panicked outside any one store's turn.
#[must_use]
pub fn pass_panics() -> u64 {
    PASS_PANICS.load(Ordering::Relaxed)
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

/// Whether a fork has asked the thread to stop.
pub(crate) fn stopping() -> bool {
    STOPPED_FOR_FORK.load(Ordering::Acquire) > 0
}

/// Makes sure this process's thread is running and wakes it, after a store's next turn came earlier.
/// Called under the store registry, whose guard is the proof. While a fork has the thread stopped it
/// does nothing: the fork's end starts it again if a store wants it.
pub(crate) fn ensure_running(_registry: &Registry<'_>) {
    if stopping() {
        return;
    }
    let mut slot = THREAD.lock().unwrap_or_else(PoisonError::into_inner);
    let generation = gate::generation();
    if let Some((started_in, handle)) = &*slot
        && *started_in == generation
    {
        handle.thread().unpark();
        return;
    }
    if let Ok(handle) = std::thread::Builder::new().name(NAME.into()).spawn(run) {
        *slot = Some((generation, handle));
    } else {
        SPAWN_FAILURES.fetch_add(1, Ordering::Relaxed);
        *slot = None;
    }
}

/// The first step of the before-fork hook, before the gate goes exclusive: stops this process's
/// thread and waits for it to end, so `CPython` counts no thread of ours and never warns. The wait is
/// for the chunk or checkpoint in progress, which a fork waits for anyway. With the interpreter
/// released, as the rest of the hook runs.
pub(crate) fn stop_for_fork() {
    STOPPED_FOR_FORK.fetch_add(1, Ordering::AcqRel);
    // Taken under a level, as the slot's rule says. Behind another thread's fork (two forks at once),
    // that fork's stop has it.
    let Some(gate) = GATE.try_enter_shared() else { return };
    let taken = Store::with_registry(&gate, |_| THREAD.lock().unwrap_or_else(PoisonError::into_inner).take());
    drop(gate);
    if let Some((started_in, handle)) = taken
        && started_in == gate::generation()
    {
        handle.thread().unpark();
        let _ = handle.join();
    }
}

/// The after-fork hook's last step, in the parent (`child` false) and in the child: lets the thread
/// run again and starts it if any store wants maintenance. In the child, which inherited the parent's
/// count and no thread, it also drops the parent's claims, health and pending warnings.
pub(crate) fn resume_after_fork(child: bool) {
    if child {
        STOPPED_FOR_FORK.store(0, Ordering::Release);
    } else if STOPPED_FOR_FORK.fetch_sub(1, Ordering::AcqRel) != 1 {
        // Another fork is still in progress: its end starts the thread.
        return;
    }
    let Some(gate) = GATE.try_enter_shared() else { return };
    let stores = Store::all_in_registry(&gate);
    let mut wanted = false;
    for store in &stores {
        if child {
            store.upkeep().reset_after_fork(&gate);
        }
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
    while !stopping() {
        match catch_unwind(AssertUnwindSafe(pass)) {
            Ok(Some(wait)) => std::thread::park_timeout(wait),
            Ok(None) => std::thread::park(),
            Err(_) => {
                PASS_PANICS.fetch_add(1, Ordering::Relaxed);
                std::thread::park_timeout(AFTER_A_PANIC);
            }
        }
    }
}

/// Enters the gate, waiting behind a pending fork with a growing pause, or None once the thread is
/// asked to stop.
fn enter_gate() -> Option<crate::gate::SharedGuard<'static>> {
    let mut pause = BEHIND_A_FORK;
    loop {
        if let Some(gate) = GATE.try_enter_shared() {
            return Some(gate);
        }
        if stopping() {
            return None;
        }
        std::thread::sleep(pause);
        pause = (pause * 2).min(BEHIND_A_FORK_MAX);
    }
}

/// One wake: every due store's turn. Returns how long until the next turn is due, or None if no store
/// wants maintenance.
fn pass() -> Option<Duration> {
    let gate = enter_gate()?;
    let now = mono_ms();
    let stores = Store::all_in_registry(&gate);
    let mut due = Vec::new();
    for store in &stores {
        if store.is_retired() {
            continue;
        }
        let mut tending = store.upkeep().tending(&gate);
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
            if stopping() {
                return Ok(None);
            }
            if injected_failure() {
                let error = crate::Error::InvalidSetting("an injected failure (a test hook)".into());
                return Ok(Some(TurnReport {
                    done: TaskSet::EMPTY,
                    stopped: None,
                    removed: 0,
                    work_seen: true,
                    error: Some(error),
                }));
            }
            catch_unwind(AssertUnwindSafe(|| {
                store.maintenance_turn(*tasks, wall_now(), Duration::from_millis(*interval_ms))
            }))
        })
        .collect();

    // Back under the gate to record the turns and find the next wake. Declared before `due` is
    // re-bound below, so on any unwind the `Arc`s drop while the level is still held.
    let gate = enter_gate()?;
    let due = due;
    let now = mono_ms();
    for ((store, _, interval_ms), report) in due.iter().zip(reports) {
        record(store.upkeep(), &mut store.upkeep().tending(&gate), now, *interval_ms, report);
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

/// Records a turn on its store's schedule and health (decision 16): a failure for an error or a
/// panic, and one for each streak of turns that found work and removed nothing because they were
/// refused or stepped aside, once it's lasted [`STARVED_INTERVALS`].
fn record(
    upkeep: &Upkeep,
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
            tending.starving_since = None;
            return;
        }
        Err(panic) => {
            tending.schedule.finish(now, TaskSet::EMPTY, false);
            let detail = panic
                .downcast_ref::<&str>()
                .map(ToString::to_string)
                .or_else(|| panic.downcast_ref::<String>().cloned())
                .unwrap_or_default();
            upkeep.fail(tending, format!("a turn panicked: {detail}"));
            return;
        }
    };
    tending.schedule.finish(now, report.done, report.cut_short());
    upkeep.count_turn(report.removed);
    TOTAL_TURNS.fetch_add(1, Ordering::Relaxed);
    TOTAL_REMOVED.fetch_add(report.removed, Ordering::Relaxed);
    let starved =
        report.work_seen && report.removed == 0 && matches!(report.stopped, Some(Stop::Refused | Stop::Yielded));
    if let Some(error) = report.error {
        upkeep.fail(tending, error.to_string());
    } else if !starved {
        tending.starving_since = None;
        tending.warned = false;
    } else {
        let since = *tending.starving_since.get_or_insert(now);
        if now.saturating_sub(since) >= STARVED_INTERVALS.saturating_mul(interval_ms) {
            // One failure per streak: the window starts over.
            tending.starving_since = Some(now);
            let seconds = now.saturating_sub(since) / 1_000;
            upkeep.fail(
                tending,
                format!(
                    "it has found work for {seconds} s without getting the write lock: another connection keeps it"
                ),
            );
        }
    }
}

#[cfg(test)]
mod tests;
