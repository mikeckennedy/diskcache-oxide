//! Plan 28 step 4: the maintenance thread, for real, on short intervals.

use std::path::Path;
use std::sync::Arc;

use super::*;
use crate::background::Maintenance;
use crate::open::{Requested, Role};
use crate::ops::Value;
use crate::schema::MODE_BYTES;
use crate::store::{DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, OpenOptions};
use crate::testutil::{TempDir, one_global_fork_at_a_time};

const T: Duration = Duration::from_secs(5);
/// Short enough for a test, which only the core allows (the public API's minimum is a second).
const FAST: u64 = 150;

fn options(dir: &Path, maintenance: Maintenance) -> OpenOptions {
    OpenOptions {
        directory: dir.to_path_buf(),
        timeout: T,
        requested: Requested::default(),
        max_connections: DEFAULT_MAX_CONNECTIONS,
        cache_pages: DEFAULT_CACHE_PAGES,
        wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
        auto_checkpoint: false,
        role: Role::Plain,
        default_size_limit: crate::schema::DEFAULT_SIZE_LIMIT,
        maintenance,
    }
}

fn every(interval_ms: u64) -> Maintenance {
    Maintenance { interval_ms, own_clock: false }
}

/// Stores `count` rows that expired a minute ago, in one transaction (a `set` would sweep a few).
fn expired_rows(store: &Store, count: usize) {
    let then = wall_now() - 60.0;
    store
        .write(T, |w, _| {
            for i in 0..count {
                w.upsert(&crate::rows::NewRow {
                    key: format!("k{i}").as_str().into(),
                    mode: MODE_BYTES,
                    value: crate::rows::Stored::Blob(b"v"),
                    size: 1,
                    store_time: then,
                    expire_time: Some(then),
                    tag: None,
                })?;
            }
            Ok(())
        })
        .unwrap();
}

/// Waits up to `within` for `done`, polling.
fn eventually(within: Duration, mut done: impl FnMut() -> bool) -> bool {
    let deadline = Instant::now() + within;
    while Instant::now() < deadline {
        if done() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    done()
}

#[test]
fn the_thread_sweeps_a_store_nobody_touches() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("worker-sweeps");
    let store = Store::open(&options(tmp.path(), every(FAST))).unwrap();
    expired_rows(&store, 500);
    let value = [1u8];
    store.set(T, "live", Value::Bytes { mode: MODE_BYTES, data: &value }, None, wall_now()).unwrap();
    assert!(eventually(Duration::from_secs(5), || store.len(T).unwrap() == 1), "the thread never swept");
    // The turn is recorded after it ends, a moment after its rows are gone.
    let recorded = || store.maintenance_health();
    assert!(eventually(Duration::from_secs(5), || recorded().removed >= 490), "{:?}", recorded());
    let health = recorded();
    assert!(health.turns > 0 && health.failures == 0, "{health:?}");
    assert_eq!(spawn_failures(), 0);
    store.release_handle();
}

#[test]
fn a_store_with_maintenance_off_or_a_clock_of_its_own_is_left_alone() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("worker-off");
    let off = Store::open(&options(&tmp.path().join("off"), Maintenance::OFF)).unwrap();
    let clocked =
        Store::open(&options(&tmp.path().join("clock"), Maintenance { interval_ms: FAST, own_clock: true })).unwrap();
    // A third store keeps the thread busy, so "nothing happened" isn't just "no thread".
    let busy = Store::open(&options(&tmp.path().join("busy"), every(FAST))).unwrap();
    for store in [&off, &clocked, &busy] {
        expired_rows(store, 10);
    }
    assert!(eventually(Duration::from_secs(5), || busy.len(T).unwrap() == 0));
    std::thread::sleep(Duration::from_millis(3 * FAST));
    assert_eq!((off.len(T).unwrap(), clocked.len(T).unwrap()), (10, 10));
    for store in [&off, &clocked, &busy] {
        store.release_handle();
    }
}

#[test]
fn a_handle_asking_for_a_shorter_interval_brings_the_turn_forward() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("worker-join");
    // An hour: nothing would happen during this test.
    let store = Store::open(&options(tmp.path(), every(3_600_000))).unwrap();
    expired_rows(&store, 20);
    std::thread::sleep(Duration::from_millis(3 * FAST));
    assert_eq!(store.len(T).unwrap(), 20);
    let second = Store::open(&options(tmp.path(), every(FAST))).unwrap();
    assert!(Arc::ptr_eq(&store, &second));
    assert!(eventually(Duration::from_secs(5), || store.len(T).unwrap() == 0), "the shorter interval didn't win");
    store.release_handle();
    second.release_handle();
}

#[test]
fn a_closed_store_gets_no_more_turns_and_a_reopened_one_does() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("worker-closed");
    let store = Store::open(&options(tmp.path(), every(FAST))).unwrap();
    store.release_handle();
    assert!(store.is_retired());
    expired_rows(&store, 10);
    std::thread::sleep(Duration::from_millis(3 * FAST));
    assert_eq!(store.len(T).unwrap(), 10, "a retired store was swept");
    let again = Store::open(&options(tmp.path(), every(FAST))).unwrap();
    assert!(eventually(Duration::from_secs(5), || again.len(T).unwrap() == 0), "the revived store wasn't swept");
    again.release_handle();
}

#[test]
fn a_failing_store_counts_failures_and_raises_one_warning() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("worker-fails");
    let store = Store::open(&options(tmp.path(), every(FAST))).unwrap();
    expired_rows(&store, 5);
    crate::open::connect(tmp.path(), T).unwrap().execute_batch("DROP TABLE settings").unwrap();
    assert!(eventually(Duration::from_secs(5), || store.maintenance_health().failures >= 2), "no failures counted");
    let warning = store.take_maintenance_warning();
    assert!(warning.as_deref().is_some_and(|w| w.contains("settings")), "{warning:?}");
    std::thread::sleep(Duration::from_millis(3 * FAST));
    assert_eq!(store.take_maintenance_warning(), None, "warned again within one streak");
    store.release_handle();
}

// -- decision 16, as `record` keeps it --------------------------------------------------------------

fn report(removed: u64, work_seen: bool, stopped: Option<Stop>, error: Option<crate::Error>) -> TurnReport {
    TurnReport { done: TaskSet::EMPTY, stopped, removed, work_seen, error }
}

fn fresh() -> (Upkeep, Tending) {
    let schedule = crate::background::schedule::Schedule::new(0, 100, []);
    let tending = Tending { schedule: schedule.clone(), last_error: None, starving_since: None, warned: false };
    (Upkeep::new(schedule), tending)
}

/// Starts a turn on the schedule so `record`'s `finish` has one to end, then records `outcome`.
fn turn_ends(upkeep: &Upkeep, tending: &mut Tending, now: u64, outcome: std::thread::Result<Option<TurnReport>>) {
    let _ = tending.schedule.begin(now);
    record(upkeep, tending, now, 100, outcome);
}

#[test]
fn a_starved_streak_counts_one_failure_per_three_intervals_and_warns_once() {
    let (upkeep, mut tending) = fresh();
    let refused = || Ok(Some(report(0, true, Some(Stop::Refused), None)));
    // Refused with work waiting: starving, but not for 3 intervals (300 ms) yet.
    for now in [100, 200, 300] {
        turn_ends(&upkeep, &mut tending, now, refused());
    }
    assert_eq!(upkeep.health().failures, 0);
    turn_ends(&upkeep, &mut tending, 400, refused());
    assert_eq!(upkeep.health().failures, 1, "three intervals starved");
    assert!(upkeep.take_warning() && !upkeep.take_warning(), "one warning");
    // Still starving: the next failure comes after another three intervals, not every turn.
    for now in [500, 600] {
        turn_ends(&upkeep, &mut tending, now, refused());
    }
    assert_eq!(upkeep.health().failures, 1);
    turn_ends(&upkeep, &mut tending, 700, refused());
    assert_eq!(upkeep.health().failures, 2);
    assert!(!upkeep.take_warning(), "warned again within one streak");
    // A turn that gets through ends the streak; the next streak warns again.
    turn_ends(&upkeep, &mut tending, 800, Ok(Some(report(10, true, None, None))));
    assert_eq!(tending.starving_since, None);
    for now in [900, 1_000, 1_100, 1_200] {
        turn_ends(&upkeep, &mut tending, now, refused());
    }
    assert!(upkeep.take_warning(), "a new streak warns");
}

#[test]
fn a_turn_that_removed_rows_before_stepping_aside_isnt_starved() {
    let (upkeep, mut tending) = fresh();
    for now in (100..=1_000).step_by(100) {
        turn_ends(&upkeep, &mut tending, now, Ok(Some(report(500, true, Some(Stop::Yielded), None))));
    }
    assert_eq!((upkeep.health().failures, upkeep.health().removed), (0, 5_000));
    assert_eq!(tending.starving_since, None);
}

#[test]
fn errors_and_panics_are_failures_with_their_message() {
    let (upkeep, mut tending) = fresh();
    let error = crate::Error::InvalidSetting("a bad chunk".into());
    turn_ends(&upkeep, &mut tending, 100, Ok(Some(report(0, true, None, Some(error)))));
    assert!(tending.last_error.as_deref().is_some_and(|e| e.contains("a bad chunk")));
    let panic: Box<dyn std::any::Any + Send> = Box::new("the turn fell over");
    turn_ends(&upkeep, &mut tending, 200, Err(panic));
    assert_eq!(upkeep.health().failures, 2);
    assert!(tending.last_error.as_deref().is_some_and(|e| e.contains("panicked: the turn fell over")));
    // No turn at all (a fork, a retired store) is neither progress nor failure, and resets starving.
    tending.starving_since = Some(150);
    turn_ends(&upkeep, &mut tending, 300, Ok(None));
    assert_eq!((upkeep.health().failures, tending.starving_since), (2, None));
}

// -- the thread around forks, revives and a held lock --------------------------------------------

#[test]
fn the_thread_stops_for_a_fork_and_comes_back_after() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("worker-fork");
    let store = Store::open(&options(tmp.path(), every(FAST))).unwrap();
    // Running, so the fork has a thread to stop.
    expired_rows(&store, 5);
    assert!(eventually(Duration::from_secs(5), || store.len(T).unwrap() == 0));
    gate::begin_fork();
    let stopped = THREAD.lock().unwrap_or_else(PoisonError::into_inner).is_none();
    expired_rows_behind_the_fork(&store);
    std::thread::sleep(Duration::from_millis(3 * FAST));
    gate::end_fork_parent();
    assert!(stopped, "the thread was still there when the fork went ahead");
    assert!(eventually(Duration::from_secs(5), || store.len(T).unwrap() == 0), "the thread didn't come back");
    store.release_handle();
}

/// Rows written while this thread holds a pending fork: through a connection of its own, since the
/// store's calls wait for the fork.
fn expired_rows_behind_the_fork(store: &Store) {
    let conn = crate::open::connect(store.directory(), T).unwrap();
    let then = wall_now() - 60.0;
    conn.execute(
        "INSERT INTO cache (key_type, key, mode, value, filename, size, store_time, expire_time, store_seq, tag_type)
         VALUES (1, 'behind', 4, x'00', NULL, 1, ?1, ?1, 1000000, 0)",
        [then],
    )
    .unwrap();
}

#[test]
fn a_reopen_takes_the_reopening_handles_ask() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("worker-reopen");
    let store = Store::open(&options(tmp.path(), every(FAST))).unwrap();
    store.release_handle();
    // Reopened by a handle with a clock of its own: the veto is in place from the moment it counts.
    let clocked = Store::open(&options(tmp.path(), Maintenance { interval_ms: FAST, own_clock: true })).unwrap();
    assert!(Arc::ptr_eq(&store, &clocked));
    expired_rows(&clocked, 10);
    std::thread::sleep(Duration::from_millis(3 * FAST));
    assert_eq!(clocked.len(T).unwrap(), 10, "swept under a handle with its own clock");
    clocked.release_handle();
    // Reopened asking for none: off, though the first handle had asked for an interval.
    let off = Store::open(&options(tmp.path(), Maintenance::OFF)).unwrap();
    std::thread::sleep(Duration::from_millis(3 * FAST));
    assert_eq!(off.len(T).unwrap(), 10, "swept although the reopening handle asked for none");
    off.release_handle();
}

#[test]
fn a_lock_held_elsewhere_starves_the_store_and_its_release_ends_the_streak() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("worker-starved");
    let store = Store::open(&options(tmp.path(), every(FAST))).unwrap();
    expired_rows(&store, 5);
    let other = crate::open::connect(tmp.path(), T).unwrap();
    other.execute_batch("BEGIN IMMEDIATE").unwrap();
    assert!(eventually(Duration::from_secs(5), || store.maintenance_health().failures >= 1), "never counted");
    let started = Instant::now();
    let warning = store.take_maintenance_warning();
    assert!(warning.as_deref().is_some_and(|w| w.contains("write lock")), "{warning:?}");
    std::thread::sleep(Duration::from_millis(5 * FAST));
    let held_for = u64::try_from(started.elapsed().as_millis()).unwrap();
    // At most one failure per three intervals, not one per (one-second) continuation.
    assert!(store.maintenance_health().failures <= 2 + held_for / (3 * FAST), "{:?}", store.maintenance_health());
    other.execute_batch("ROLLBACK").unwrap();
    assert!(eventually(Duration::from_secs(5), || store.len(T).unwrap() == 0), "never swept after the release");
    store.release_handle();
}
