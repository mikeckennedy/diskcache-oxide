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
    let recorded = || store.maintenance_health().unwrap();
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
    assert!(
        eventually(Duration::from_secs(5), || store.maintenance_health().unwrap().failures >= 2),
        "no failures counted"
    );
    let warning = store.take_maintenance_warning();
    assert!(warning.as_deref().is_some_and(|w| w.contains("settings")), "{warning:?}");
    std::thread::sleep(Duration::from_millis(3 * FAST));
    assert_eq!(store.take_maintenance_warning(), None, "warned again within one streak");
    store.release_handle();
}
