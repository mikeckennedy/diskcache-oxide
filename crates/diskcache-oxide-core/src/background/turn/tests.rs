//! Plan 28 step 3: one maintenance turn on one store.
//!
//! Every test takes the global fork lock: the fork tests in this binary claim a fork on the real gate,
//! and a turn rightly refuses while one is pending. Tests that aren't about the budget run with a
//! generous one and assert the turn wasn't cut short: on an idle store, any cut is a bug.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use super::*;
use crate::open::{Requested, Role};
use crate::ops::Value;
use crate::schema::{EvictionPolicy, MIN_SIZE_LIMIT, MODE_BYTES};
use crate::store::{DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, OpenOptions};
use crate::testutil::{TempDir, one_global_fork_at_a_time};

const T: Duration = Duration::from_secs(5);
const NOW: f64 = 1_000_000.0;
const INTERVAL: Duration = Duration::from_secs(300);
const GENEROUS: Duration = Duration::from_secs(10);

fn options(dir: &Path) -> OpenOptions {
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
        maintenance: crate::background::Maintenance::OFF,
    }
}

fn open(dir: &Path) -> Arc<Store> {
    Store::open(&options(dir)).unwrap()
}

fn put(store: &Store, key: &str, bytes: usize, expire: Option<f64>) {
    let data = vec![7u8; bytes];
    store.set(T, key, Value::Bytes { mode: MODE_BYTES, data: &data }, expire, NOW).unwrap();
}

fn tasks(list: &[Task]) -> TaskSet {
    list.iter().copied().collect()
}

/// A whole turn with a generous budget, which must not be cut short.
fn run(store: &Store, list: &[Task], now: f64, interval: Duration) -> TurnReport {
    let report = store.maintenance_turn_within(tasks(list), now, interval, GENEROUS).expect("a turn");
    assert!(!report.cut_short(), "an idle store's turn was cut short: {report:?}");
    report
}

fn peer(dir: &Path) -> Connection {
    crate::open::connect(dir, T).unwrap()
}

fn data_version(conn: &Connection) -> i64 {
    conn.query_row("PRAGMA data_version", [], |r| r.get(0)).unwrap()
}

fn claim_row(dir: &Path, which: Claim) -> Option<f64> {
    read_claim(&peer(dir), which).unwrap()
}

fn write_claim(dir: &Path, which: Claim, at: f64) {
    peer(dir).execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?1, ?2)", (which.row(), at)).unwrap();
}

fn value_files(dir: &Path) -> usize {
    std::fs::read_dir(dir)
        .unwrap()
        .flatten()
        .filter(|e| e.path().is_dir())
        .flat_map(|d| std::fs::read_dir(d.path()).unwrap().flatten())
        .count()
}

fn row_exists(store: &Store, key: &str) -> bool {
    store.contains(T, key, 0.0).unwrap()
}

// -- expiry -----------------------------------------------------------------------------------------

#[test]
fn a_tidy_cache_is_only_read() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-tidy");
    let store = open(tmp.path());
    put(&store, "live", 10, None);
    put(&store, "later", 10, Some(3_600.0));
    let peer = peer(tmp.path());
    let before = data_version(&peer);
    let report = run(&store, &[Task::Expire, Task::Checkpoint], NOW + 10.0, INTERVAL);
    assert_eq!(report.done, tasks(&[Task::Expire, Task::Checkpoint]));
    assert!(report.error.is_none() && report.removed == 0 && !report.work_seen, "{report:?}");
    assert_eq!(data_version(&peer), before, "the turn wrote");
    assert_eq!(claim_row(tmp.path(), Claim::Expire), None, "no claim without work");
    store.release_handle();
}

#[test]
fn expired_rows_and_their_files_go_and_the_turn_claims_the_work() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-expire");
    let store = open(tmp.path());
    for i in 0..250 {
        put(&store, &format!("gone{i}"), 10, Some(1.0));
    }
    // Spilled to value files: the turn's unlinks must take them too.
    for i in 0..3 {
        put(&store, &format!("big{i}"), spill::THRESHOLD * 2, Some(1.0));
    }
    put(&store, "live", 10, None);
    let report = run(&store, &[Task::Expire], NOW + 10.0, INTERVAL);
    assert_eq!((report.done, report.removed, report.work_seen), (tasks(&[Task::Expire]), 253, true));
    assert_eq!(store.len(T).unwrap(), 1);
    assert_eq!(claim_row(tmp.path(), Claim::Expire), Some(NOW + 10.0));
    assert_eq!(store.upkeep().own_claim(Claim::Expire), Some(NOW + 10.0));
    assert_eq!(value_files(tmp.path()), 0, "a value file outlived its row");
    store.release_handle();
}

#[test]
fn a_value_rewritten_before_the_turn_is_never_removed() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-rewrite");
    let store = open(tmp.path());
    for i in 0..50 {
        put(&store, &format!("k{i}"), 10, Some(1.0));
    }
    // Rewritten with no expiry before they expire (so these sets sweep nothing themselves): the
    // delete re-checks the expiry in its own transaction, so they stay.
    for i in 0..10 {
        put(&store, &format!("k{i}"), 4, None);
    }
    assert_eq!(run(&store, &[Task::Expire], NOW + 10.0, INTERVAL).removed, 40);
    assert_eq!(store.len(T).unwrap(), 10);
    store.release_handle();
}

/// Rows whose expiry has passed by `NOW + 10`, written in one transaction.
fn bulk_expired(store: &Store, count: usize) {
    store
        .write(T, |w, _| {
            for i in 0..count {
                w.upsert(&crate::rows::NewRow {
                    key: format!("k{i}").as_str().into(),
                    mode: MODE_BYTES,
                    value: crate::rows::Stored::Blob(b"v"),
                    size: 1,
                    store_time: NOW,
                    expire_time: Some(NOW + 1.0),
                    tag: None,
                })?;
            }
            Ok(())
        })
        .unwrap();
}

#[test]
fn a_turn_cut_short_continues_its_own_claim_and_spares_values_rewritten_in_between() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-continue");
    let store = open(tmp.path());
    bulk_expired(&store, 6_000);
    // A 1 ms budget can't remove 6,000 rows in one turn.
    let first =
        store.maintenance_turn_within(tasks(&[Task::Expire]), NOW + 10.0, INTERVAL, Duration::from_millis(1)).unwrap();
    assert_eq!(first.stopped, Some(Stop::Budget), "{first:?}");
    assert!(first.done.is_empty() && first.removed > 0 && first.work_seen);
    // Between the turns, ten rows the sweep hasn't reached yet get fresh values.
    let survivors: Vec<String> =
        (0..6_000).map(|i| format!("k{i}")).filter(|k| row_exists(&store, k)).take(10).collect();
    assert_eq!(survivors.len(), 10);
    for key in &survivors {
        put(&store, key, 4, None);
    }
    // The continuation a second later finds its own claim, not another process's, and goes on.
    let rest = run(&store, &[Task::Expire], NOW + 11.0, INTERVAL);
    assert_eq!(rest.done, tasks(&[Task::Expire]));
    assert_eq!(store.len(T).unwrap(), 10);
    assert!(survivors.iter().all(|k| row_exists(&store, k)));
    store.release_handle();
}

// -- eviction ---------------------------------------------------------------------------------------

/// A cache over its 1 MiB limit with the stored `cull_limit` given, written with culling off so the
/// writes themselves evicted nothing.
fn over_the_limit(dir: &Path, cull_limit: i64, policy: Option<EvictionPolicy>) -> Arc<Store> {
    let requested = Requested { size_limit: Some(MIN_SIZE_LIMIT), cull_limit: Some(0), eviction_policy: policy };
    let store = Store::open(&OpenOptions { requested, ..options(dir) }).unwrap();
    for i in 0..40 {
        put(&store, &format!("k{i:02}"), 60_000, None);
    }
    assert!(store.volume(T).unwrap() > MIN_SIZE_LIMIT);
    peer(dir).execute("UPDATE state SET cull_limit = ?1", [cull_limit]).unwrap();
    store
}

#[test]
fn a_cache_left_over_its_limit_is_trimmed_cull_limit_rows_at_a_time() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-cull");
    let store = over_the_limit(tmp.path(), 5, None);
    let before = store.volume(T).unwrap();
    let report = run(&store, &[Task::Expire], NOW + 10.0, INTERVAL);
    assert!(report.error.is_none() && report.removed > 0 && report.work_seen, "{report:?}");
    let after = store.volume(T).unwrap();
    assert!(after < MIN_SIZE_LIMIT, "still over the limit");
    // At most one chunk of 5 past the limit: putting the last chunk back would be over it again.
    let per_row = (before - after) / i64::try_from(report.removed).unwrap();
    assert!(after + 5 * per_row >= MIN_SIZE_LIMIT, "evicted more than a chunk past the limit");
    // The least recently stored went first.
    assert!(!row_exists(&store, "k00") && row_exists(&store, "k39"));
    store.release_handle();
}

#[test]
fn cull_limit_zero_means_no_background_eviction() {
    // diskcache's meaning: the app culls itself (Michael, 2026-11-13). Expired rows still go.
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-cull-zero");
    let store = over_the_limit(tmp.path(), 0, None);
    put(&store, "gone", 10, Some(1.0));
    let report = run(&store, &[Task::Expire], NOW + 10.0, INTERVAL);
    assert_eq!(report.removed, 1, "only the expired row");
    assert_eq!(store.len(T).unwrap(), 40);
    store.release_handle();
}

#[test]
fn eviction_policy_none_is_never_evicted() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-none");
    let store = over_the_limit(tmp.path(), 10, Some(EvictionPolicy::None));
    let report = run(&store, &[Task::Expire], NOW + 10.0, INTERVAL);
    assert_eq!((report.removed, report.done, report.work_seen), (0, tasks(&[Task::Expire]), false));
    assert_eq!(store.len(T).unwrap(), 40);
    store.release_handle();
}

#[test]
fn expired_rows_go_first_then_live_ones_only_if_still_over() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-both");
    // Culling off while the entries are swapped, so these writes evict nothing themselves.
    let store = over_the_limit(tmp.path(), 0, None);
    // Swap thirty live entries for thirty that expire: removing those alone brings the cache back
    // under its limit.
    store
        .write(T, |w, _| {
            for i in 0..30 {
                w.delete_key(format!("k{i:02}").as_str())?;
            }
            Ok(())
        })
        .unwrap();
    for i in 0..30 {
        put(&store, &format!("e{i:02}"), 60_000, Some(1.0));
    }
    assert!(store.volume(T).unwrap() > MIN_SIZE_LIMIT);
    peer(tmp.path()).execute("UPDATE state SET cull_limit = 10", []).unwrap();
    let report = run(&store, &[Task::Expire], NOW + 10.0, INTERVAL);
    assert!(store.volume(T).unwrap() < MIN_SIZE_LIMIT);
    assert_eq!(report.removed, 30, "only the expired ones");
    assert!((30..40).all(|i| row_exists(&store, &format!("k{i:02}"))), "a live entry was evicted needlessly");
    store.release_handle();
}

// -- the claim ----------------------------------------------------------------------------------------

#[test]
fn another_processes_recent_claim_skips_the_work() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-claimed");
    let store = open(tmp.path());
    for i in 0..20 {
        put(&store, &format!("k{i}"), 10, Some(1.0));
    }
    let now = NOW + 10.0;
    // Swept a minute ago by another process: this one skips, and counts the work as done.
    write_claim(tmp.path(), Claim::Expire, now - 60.0);
    let report = run(&store, &[Task::Expire], now, INTERVAL);
    assert_eq!((report.done, report.removed), (tasks(&[Task::Expire]), 0));
    // On a 60 s interval, though, a minute is past 90% of the cadence: this process takes the work,
    // so the shortest interval among the processes sets the pace.
    assert_eq!(run(&store, &[Task::Expire], now, Duration::from_secs(60)).removed, 20);
    store.release_handle();
}

#[test]
fn a_claim_from_the_future_or_long_ago_lets_this_process_take_the_work() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-stale-claim");
    let store = open(tmp.path());
    put(&store, "a", 10, Some(1.0));
    let now = NOW + 10.0;
    // The clock stepped back: a claim far in the future is stale.
    write_claim(tmp.path(), Claim::Expire, now + 1_000.0);
    assert_eq!(run(&store, &[Task::Expire], now, INTERVAL).removed, 1);
    put(&store, "b", 10, Some(1.0));
    // 95% of the interval ago: past the 90% that still counts.
    write_claim(tmp.path(), Claim::Expire, now - 0.95 * INTERVAL.as_secs_f64());
    assert_eq!(run(&store, &[Task::Expire], now, INTERVAL).removed, 1);
    assert_eq!(claim_row(tmp.path(), Claim::Expire), Some(now));
    store.release_handle();
}

#[test]
fn the_claim_is_checked_again_inside_the_write() {
    // Two processes that both read an old row: the second to get the write lock finds the first's
    // claim there and backs off.
    let tmp = TempDir::new("turn-claim-cas");
    let store = open(tmp.path());
    let conn = peer(tmp.path());
    write_claim(tmp.path(), Claim::Expire, NOW - 1.0);
    conn.execute_batch("BEGIN IMMEDIATE").unwrap();
    assert!(!claim(&conn, Claim::Expire, NOW, None, 300.0).unwrap(), "took a claim another process holds");
    assert!(claim(&conn, Claim::Expire, NOW, Some(NOW - 1.0), 300.0).unwrap(), "its own claim");
    conn.execute_batch("COMMIT").unwrap();
    assert_eq!(claim_row(tmp.path(), Claim::Expire), Some(NOW));
    store.release_handle();
}

#[test]
fn a_claim_is_another_processes_only_while_it_is_recent_and_not_ours() {
    let cadence = 300.0;
    assert!(!held_elsewhere(None, None, NOW, cadence));
    assert!(held_elsewhere(Some(NOW - 10.0), None, NOW, cadence));
    assert!(held_elsewhere(Some(NOW - 269.0), None, NOW, cadence), "under 90% of the cadence");
    assert!(!held_elsewhere(Some(NOW - 271.0), None, NOW, cadence), "past 90%");
    assert!(!held_elsewhere(Some(NOW - 10.0), Some(NOW - 10.0), NOW, cadence), "our own");
    assert!(held_elsewhere(Some(NOW - 10.0), Some(NOW - 20.0), NOW, cadence), "someone took it since ours");
    assert!(held_elsewhere(Some(NOW + 2.0), None, NOW, cadence), "a moment ahead counts");
    assert!(!held_elsewhere(Some(NOW + 60.0), None, NOW, cadence), "far ahead is stale");
}

#[test]
fn claim_rows_never_trouble_a_reopen_or_check() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-claim-rows");
    let plain = tmp.path().join("plain");
    let shard = tmp.path().join("000");
    for (dir, role) in [(&plain, Role::Plain), (&shard, Role::Shard { index: 0, count: 1 })] {
        let store = Store::open(&OpenOptions { role, ..options(dir) }).unwrap();
        store.release_handle();
        write_claim(dir, Claim::Expire, NOW);
        write_claim(dir, Claim::Dirs, NOW);
        peer(dir).execute("INSERT INTO settings (name, value) VALUES ('maintenance.later', x'00')", []).unwrap();
        let store = Store::open(&OpenOptions { role, ..options(dir) }).unwrap();
        let mut found = Vec::new();
        store.check(false, T, &mut found).unwrap();
        assert!(found.is_empty(), "{found:?}");
        store.release_handle();
    }
}

// -- stops, failures and refusals ---------------------------------------------------------------------

#[test]
fn a_failed_task_is_reported_and_the_rest_still_run() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-failed");
    let store = open(tmp.path());
    put(&store, "gone", 10, Some(1.0));
    peer(tmp.path()).execute_batch("DROP TABLE settings").unwrap();
    let report = run(&store, &[Task::Expire, Task::Checkpoint], NOW + 10.0, INTERVAL);
    assert!(report.error.is_some(), "{report:?}");
    assert_eq!(report.done, tasks(&[Task::Checkpoint]), "expiry failed, the checkpoint still ran");
    store.release_handle();
}

#[test]
fn a_lock_held_elsewhere_cuts_the_turn_with_nothing_removed() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-locked");
    let store = open(tmp.path());
    put(&store, "gone", 10, Some(1.0));
    let other = peer(tmp.path());
    other.execute_batch("BEGIN IMMEDIATE").unwrap();
    let report = store.maintenance_turn_within(tasks(&[Task::Expire]), NOW + 10.0, INTERVAL, GENEROUS).unwrap();
    assert_eq!((report.stopped, report.removed, report.work_seen), (Some(Stop::Refused), 0, true));
    assert_eq!(store.upkeep().own_claim(Claim::Expire), None, "a claim recorded for a write that never ran");
    other.execute_batch("ROLLBACK").unwrap();
    store.release_handle();
}

#[test]
fn no_turn_on_a_retired_or_poisoned_store_or_behind_a_fork() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-none-at-all");
    let store = open(tmp.path());
    let poisoned = store.poison_for_test();
    assert!(store.maintenance_turn(tasks(&[Task::Expire]), NOW, INTERVAL).is_none());
    drop(poisoned);
    gate::begin_fork();
    let behind = std::thread::scope(|s| {
        s.spawn(|| store.maintenance_turn(tasks(&[Task::Expire]), NOW, INTERVAL).is_none()).join().unwrap()
    });
    gate::end_fork_parent();
    assert!(behind, "a turn ran behind a pending fork");
    assert!(store.maintenance_turn(tasks(&[Task::Expire]), NOW, INTERVAL).is_some());
    store.release_handle();
    assert!(store.maintenance_turn(tasks(&[Task::Expire]), NOW, INTERVAL).is_none());
}

// -- the checkpoint ----------------------------------------------------------------------------------

/// A store with a small WAL limit whose WAL has grown well past its trim size.
fn grown_wal(dir: &Path, max_connections: u64) -> (Arc<Store>, PathBuf) {
    let store =
        Store::open(&OpenOptions { wal_size_limit: crate::wal::MIN_SIZE_LIMIT, max_connections, ..options(dir) })
            .unwrap();
    // A reader in another connection keeps the WAL from resetting while it grows.
    let reader = peer(dir);
    reader.execute_batch("BEGIN; SELECT COUNT(*) FROM cache;").unwrap();
    for i in 0..60 {
        put(&store, &format!("k{i}"), 50_000, None);
    }
    reader.execute_batch("COMMIT").unwrap();
    let wal = dir.join(format!("{}-wal", crate::schema::DB_FILENAME));
    let size = std::fs::metadata(&wal).unwrap().len();
    assert!(size > crate::wal::journal_size_limit(crate::wal::MIN_SIZE_LIMIT), "the WAL didn't grow: {size}");
    (store, wal)
}

fn wal_size(wal: &Path) -> u64 {
    std::fs::metadata(wal).map_or(0, |m| m.len())
}

#[test]
fn the_checkpoint_truncates_a_large_wal() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-checkpoint");
    let (store, wal) = grown_wal(tmp.path(), DEFAULT_MAX_CONNECTIONS);
    let report = run(&store, &[Task::Checkpoint], NOW, INTERVAL);
    assert_eq!(report.done, tasks(&[Task::Checkpoint]), "{report:?}");
    assert_eq!(wal_size(&wal), 0, "the WAL wasn't truncated");
    store.release_handle();
}

#[test]
fn the_checkpoint_never_copies_under_the_write_lock() {
    use crate::wal::Outcome;
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-checkpoint-pinned");
    let (store, wal) = grown_wal(tmp.path(), DEFAULT_MAX_CONNECTIONS);
    // A reader in another process pins a snapshot from before more writes: PASSIVE can't copy
    // everything, and the turn mustn't take the write lock to finish the copy.
    let reader = peer(tmp.path());
    reader.execute_batch("BEGIN; SELECT COUNT(*) FROM cache;").unwrap();
    put(&store, "after", 50_000, None);
    let size = wal_size(&wal);
    let turn = store.begin_background_turn().unwrap();
    assert!(matches!(store.background_checkpoint(&turn), Some(Outcome::Busy)));
    assert_eq!(wal_size(&wal), size, "the WAL was reset under a reader");
    // It never arms the request path's backoff, so the last close's checkpoint (which waits for
    // readers, as this one doesn't) still runs: the review's 539 MiB WAL left after close.
    assert!(!store.wal_in_backoff_for_tests(), "a background checkpoint started the close path's backoff");
    assert!(matches!(store.background_checkpoint(&turn), Some(Outcome::Busy)), "tried again next turn");
    drop(turn);
    reader.execute_batch("COMMIT").unwrap();
    store.release_handle();
}

#[test]
fn a_one_connection_store_never_checkpoints_in_the_background() {
    use crate::wal::Outcome;
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-checkpoint-one");
    let (store, wal) = grown_wal(tmp.path(), 1);
    let size = wal_size(&wal);
    let turn = store.begin_background_turn().unwrap();
    assert!(matches!(store.background_checkpoint(&turn), Some(Outcome::NoSlot)));
    assert_eq!(wal_size(&wal), size);
    assert!(!store.wal_in_backoff_for_tests(), "a background NoSlot started the close path's backoff");
    drop(turn);
    store.release_handle();
}

// -- value folders -------------------------------------------------------------------------------------

/// An empty value folder last touched an hour ago, as a process that deleted its files leaves it.
fn old_empty_folder(dir: &Path, name: &str) -> PathBuf {
    let folder = dir.join(name);
    std::fs::create_dir_all(&folder).unwrap();
    let hour_ago = std::time::SystemTime::now() - Duration::from_secs(3_600);
    std::fs::File::open(&folder).unwrap().set_modified(hour_ago).unwrap();
    folder
}

#[test]
fn value_folders_are_swept_on_shards_too_and_the_sweep_is_claimed() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-dirs");
    let plain = tmp.path().join("plain");
    let shard = tmp.path().join("000");
    let stores = [
        Store::open(&options(&plain)).unwrap(),
        Store::open(&OpenOptions { role: Role::Shard { index: 0, count: 1 }, ..options(&shard) }).unwrap(),
    ];
    for (store, dir) in stores.iter().zip([&plain, &shard]) {
        let folder = old_empty_folder(dir, "ab");
        let report = run(store, &[Task::ValueDirs], NOW, INTERVAL);
        assert_eq!(report.done, tasks(&[Task::ValueDirs]), "{report:?}");
        assert!(!folder.exists(), "{} kept its empty folder", dir.display());
        assert_eq!(claim_row(dir, Claim::Dirs), Some(NOW));
        // Within the hour another process's sweep counts.
        let again = old_empty_folder(dir, "cd");
        write_claim(dir, Claim::Dirs, NOW + 1.0);
        run(store, &[Task::ValueDirs], NOW + 60.0, INTERVAL);
        assert!(again.exists(), "swept although another process had just swept");
    }
    for store in &stores {
        store.release_handle();
    }
}

// -- decision 2: a whole turn never reaches the interpreter --------------------------------------------

#[test]
fn a_whole_turn_on_a_native_thread_never_goes_through_the_blocker() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("turn-native");
    let (store, wal) = grown_wal(tmp.path(), DEFAULT_MAX_CONNECTIONS);
    // Past the unlink threshold, so the unlinks run through `run_blocking` too.
    for i in 0..12 {
        put(&store, &format!("big{i}"), spill::THRESHOLD * 2, Some(1.0));
    }
    let folder = old_empty_folder(tmp.path(), "ef");
    let _ = gate::set_blocker(|work| work());
    let (report, calls) = std::thread::scope(|s| {
        s.spawn(|| {
            gate::mark_native_thread();
            let report = run(&store, &Task::ALL, NOW + 10.0, INTERVAL);
            (report, gate::blocker_calls())
        })
        .join()
        .unwrap()
    });
    assert_eq!(calls, 0, "the native thread went through the blocker");
    assert_eq!(report.done, tasks(&Task::ALL), "{report:?}");
    assert_eq!(report.removed, 12);
    // Back within its trim size: truncated by the checkpoint, or restarted by expiry's own writes and
    // cut back by `journal_size_limit`, after which the checkpoint has nothing to do.
    assert!(wal_size(&wal) <= crate::wal::journal_size_limit(crate::wal::MIN_SIZE_LIMIT));
    assert!(!folder.exists());
    store.release_handle();
}
