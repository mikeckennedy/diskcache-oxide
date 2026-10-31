//! A pending fork can't outlast an operation's `timeout` (plan 09, step 1b): one test per call site.
//!
//! These use the global gate, which every core test shares, so a fork stays pending for well under a
//! second (other tests' `T` is 5 s, and `RELEASE_BUDGET` is 1 s) and each wait has a watchdog: the
//! wedge these tests pin hung `cargo test` for 28 minutes. A thread that holds a level here only ever
//! enters again reentrantly.

use std::sync::atomic::AtomicBool;
use std::sync::mpsc;

use rusqlite::types::ValueRef;

use super::*;
use crate::ops::Value;
use crate::rows::{NewRow, Stored};
use crate::schema::MODE_BYTES;
use crate::spill;
use crate::testutil::{TempDir, one_global_fork_at_a_time};

const T: Duration = Duration::from_secs(5);
/// A call's timeout behind a fork.
const SHORT: Duration = Duration::from_millis(150);
/// How far past its deadline a call may return here, on a loaded machine.
const SLACK: Duration = Duration::from_millis(500);
const NOW: f64 = 1_000_000.0;

fn options(dir: &Path, timeout: Duration) -> OpenOptions {
    OpenOptions {
        directory: dir.to_path_buf(),
        timeout,
        requested: Requested::default(),
        max_connections: DEFAULT_MAX_CONNECTIONS,
        cache_pages: DEFAULT_CACHE_PAGES,
        wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
        auto_checkpoint: false,
    }
}

fn open_store(dir: &Path) -> Arc<Store> {
    Store::open(&options(dir, T)).unwrap()
}

fn put(store: &Store, key: &str) {
    store
        .write(T, |w, _| {
            w.upsert(&NewRow { key, mode: 2, value: Stored::Blob(b"v"), size: 1, store_time: 1.0, expire_time: None })
        })
        .unwrap();
}

/// Runs `body` on another thread with a fork pending, claimed by this thread, under a watchdog. The
/// fork ends however `body` went, so a failure here can't wedge the rest of the binary.
fn behind_a_fork<R: Send>(body: impl FnOnce() -> R + Send) -> R {
    gate::begin_fork();
    let (done, finished) = mpsc::channel();
    std::thread::scope(|s| {
        s.spawn(move || {
            let _ = done.send(body());
        });
        let result = finished.recv_timeout(T);
        gate::end_fork_parent();
        result.expect("the watchdog: a call behind a fork didn't return")
    })
}

/// Waits for what a thread behind this thread's fork reports, under a watchdog, and ends the fork
/// whichever way it went. A plain `join` here would hang on a regression: that thread waits for the
/// fork, and this one for that thread before it ends the fork.
fn end_fork_after<R>(finished: &mpsc::Receiver<R>) -> R {
    let result = finished.recv_timeout(T);
    gate::end_fork_parent();
    result.expect("the watchdog: a call behind a fork didn't return")
}

/// `call` fails with `Timeout { seconds: SHORT }` no earlier than `SHORT`, and not much later.
#[track_caller]
fn times_out<T: std::fmt::Debug>(call: impl FnOnce() -> Result<T>) {
    let started = Instant::now();
    let result = call();
    let took = started.elapsed();
    match result {
        Err(Error::Timeout { seconds }) => {
            assert!((seconds - SHORT.as_secs_f64()).abs() < 1e-9, "reported {seconds} s");
        }
        other => panic!("expected a timeout, got {other:?}"),
    }
    assert!(took >= SHORT, "gave up after {took:?}, before its deadline");
    assert!(took < SHORT + SLACK, "took {took:?}");
}

fn file_count(dir: &Path) -> usize {
    fn walk(dir: &Path) -> usize {
        std::fs::read_dir(dir).map_or(0, |entries| {
            entries
                .filter_map(std::result::Result::ok)
                .map(|e| if e.path().is_dir() { walk(&e.path()) } else { 1 })
                .sum()
        })
    }
    std::fs::read_dir(dir)
        .unwrap()
        .filter_map(std::result::Result::ok)
        .filter(|e| e.path().is_dir())
        .map(|e| walk(&e.path()))
        .sum()
}

/// Open file descriptors on files inside `dir`, this process's.
fn fds_inside(dir: &Path) -> usize {
    let dir = std::fs::canonicalize(dir).unwrap();
    if cfg!(target_os = "linux") {
        return std::fs::read_dir("/proc/self/fd")
            .unwrap()
            .filter_map(std::result::Result::ok)
            .filter_map(|e| std::fs::read_link(e.path()).ok())
            .filter(|target| target.starts_with(&dir))
            .count();
    }
    let output = std::process::Command::new("lsof")
        .args(["-n", "-P", "-F", "n", "-p", &std::process::id().to_string()])
        .output()
        .unwrap();
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| line.strip_prefix('n'))
        .filter(|path| Path::new(path).starts_with(&dir))
        .count()
}

/// Polls until `done`, for a state another test's fork can delay.
#[track_caller]
fn eventually(mut done: impl FnMut() -> bool) {
    let deadline = Instant::now() + T;
    while !done() {
        assert!(Instant::now() < deadline, "never happened");
        std::thread::sleep(Duration::from_millis(1));
    }
}

/// Drains until `store`'s deferred releases are gone: another test's fork, whose end doesn't run the
/// drain as the Python hooks do, can have been the one that failed its try.
#[track_caller]
fn drained(store: &Store) {
    eventually(|| {
        drain_deferred_releases();
        store.deferred_releases.load(Ordering::SeqCst) == 0
    });
}

// -- the regression ------------------------------------------------------------------------------

/// The wedge on a real store: an operation stays inside its closure until another thread's call has
/// returned, and a fork claims in between. Before step 1b that call waited for the fork, the fork for
/// the operation, and the operation for that call.
#[test]
fn a_call_behind_a_fork_that_waits_for_its_caller_returns_and_the_cycle_clears() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-cycle");
    let store = open_store(tmp.path());
    put(&store, "k");

    let forked = Arc::new(AtomicBool::new(false));
    let (inside, entered) = mpsc::channel();
    let (claimed, wait_for_claim) = mpsc::channel();
    let (result_tx, result_rx) = mpsc::channel();

    let fork_thread = {
        let forked = Arc::clone(&forked);
        std::thread::spawn(move || {
            entered.recv().unwrap();
            let waiter = std::thread::spawn(move || {
                // Claimed once the flag is up, with begin_fork waiting for the operation's level.
                eventually(|| GATE.stats().pending);
                claimed.send(()).unwrap();
            });
            gate::begin_fork();
            forked.store(true, Ordering::Release);
            gate::end_fork_parent();
            waiter.join().unwrap();
        })
    };
    let other = {
        let store = Arc::clone(&store);
        std::thread::spawn(move || {
            wait_for_claim.recv().unwrap();
            let _ = result_tx.send(store.contains(SHORT, "k", NOW));
        })
    };

    let (received, forked_early) = store
        .read(T, |_| {
            let _ = inside.send(());
            let received = result_rx.recv_timeout(T);
            Ok((received, forked.load(Ordering::Acquire)))
        })
        .unwrap();
    let result = received.expect("the watchdog: the other thread's call waited for the fork");
    assert!(matches!(result, Err(Error::Timeout { .. })), "{result:?}");
    assert!(!forked_early, "the fork completed while the operation still held its level");
    fork_thread.join().unwrap();
    other.join().unwrap();
    assert!(forked.load(Ordering::Acquire), "the fork completes once the operation returns");
    assert!(store.contains(T, "k", NOW).unwrap(), "and a fresh operation succeeds");
}

// -- rows 1 to 3: opening ------------------------------------------------------------------------

#[test]
fn row_1_opening_a_new_directory_behind_a_fork_times_out() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-open-new");
    let dir = tmp.path().join("c");
    behind_a_fork(|| times_out(|| Store::open(&options(&dir, SHORT))));
    assert_eq!(open_store(&dir).handles(), 1, "and opens once the fork has ended");
}

#[test]
fn row_3_opening_an_open_directory_behind_a_fork_times_out_with_no_handle_claimed() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-open-lookup");
    let store = open_store(tmp.path());
    behind_a_fork(|| times_out(|| Store::open(&options(tmp.path(), SHORT))));
    assert_eq!(store.handles(), 1, "a timed-out lookup claimed a handle");
}

/// Stops the fork thread when dropped, so a failed assert can't leave it forking for good.
struct StopForks(Arc<AtomicBool>);
impl Drop for StopForks {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}

/// Row 2: an open beside a stream of forks succeeds, claims one handle and leaves no level behind.
/// That's all this pins. That one level covers the insert is the shape of `open_until`, where the
/// attempt returns its guard: no test here can tell it from a second entry that happened to get in.
#[test]
fn row_2_an_open_beside_forks_succeeds_and_leaves_no_level_behind() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-open-insert");
    let stop = Arc::new(AtomicBool::new(false));
    let stop_forks = StopForks(Arc::clone(&stop));
    let fork_thread = {
        let stop = Arc::clone(&stop);
        std::thread::spawn(move || {
            while !stop.load(Ordering::Acquire) {
                gate::begin_fork();
                gate::end_fork_parent();
                std::thread::sleep(Duration::from_millis(1));
            }
        })
    };
    for i in 0..25 {
        let store = Store::open(&options(&tmp.path().join(format!("c{i}")), T)).unwrap();
        assert_eq!(store.handles(), 1);
        assert_eq!(GATE.own_depth(), 0, "the open kept a level");
        store.release_handle();
    }
    drop(stop_forks);
    fork_thread.join().unwrap();
}

/// `Cache(dir, size_limit=...)`: the lookup got in, then `join`'s write times out (here on the write
/// lock, while a fork arrives). Its failure path adds no wait, and the handle goes at the drain.
#[test]
fn row_5_a_failed_join_behind_a_fork_defers_its_release_and_adds_no_wait() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-join");
    let store = open_store(tmp.path());
    let token = store.write_lock.try_lock().unwrap();
    let budget = Duration::from_millis(400);
    let requested = Requested { size_limit: Some(7 << 20), cull_limit: None };
    let joiner = {
        let directory = tmp.path().to_path_buf();
        std::thread::spawn(move || {
            let started = Instant::now();
            let result = Store::open(&OpenOptions { requested, ..options(&directory, budget) });
            (result.map(|_| ()), started.elapsed())
        })
    };
    eventually(|| store.write_waiters() > 0);
    gate::begin_fork();
    let pending_in_time = store.write_waiters() > 0;
    let (result, took) = joiner.join().unwrap();
    let deferred = store.deferred_releases.load(Ordering::SeqCst);
    gate::end_fork_parent();
    drop(token);
    assert!(matches!(result, Err(Error::Timeout { seconds }) if (seconds - budget.as_secs_f64()).abs() < 1e-9));
    assert!(took < budget + SLACK, "the failure path waited: {took:?} on a {budget:?} budget");
    if pending_in_time {
        assert_eq!(deferred, 1, "the release wasn't deferred");
    }
    drained(&store);
    assert_eq!(store.handles(), 1, "the failed join's handle was released");
    assert_eq!(GATE.own_depth(), 0);
}

// -- row 4: reopening ----------------------------------------------------------------------------

#[test]
fn row_4_a_timed_out_reopen_leaves_the_handle_closed_and_uncounted() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-reopen");
    let store = open_store(tmp.path());
    store.release_handle();
    let closed = AtomicBool::new(true);
    behind_a_fork(|| {
        let deadline = Instant::now() + SHORT;
        let result = store.add_handle_until(deadline, || closed.swap(false, Ordering::AcqRel));
        assert!(matches!(result, Err(Error::Timeout { .. })), "{result:?}");
    });
    assert!(closed.load(Ordering::Acquire), "the handle was marked open");
    assert_eq!(store.handles(), 0, "a handle was counted");
    store.add_handle_until(Instant::now() + T, || closed.swap(false, Ordering::AcqRel)).unwrap();
    assert!(!closed.load(Ordering::Acquire));
    assert_eq!(store.handles(), 1);
    // Only the caller that flips the flag counts.
    store.add_handle_until(Instant::now() + T, || closed.swap(false, Ordering::AcqRel)).unwrap();
    assert_eq!(store.handles(), 1);
}

// -- row 5: closing ------------------------------------------------------------------------------

#[test]
fn row_5_a_release_behind_a_stuck_fork_defers_and_the_drain_finishes_it() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-release");
    let store = open_store(tmp.path());
    put(&store, "k");
    let weak = Arc::downgrade(&store);
    assert!(fds_inside(tmp.path()) > 0);
    let budget = Duration::from_millis(100);
    behind_a_fork(move || {
        let started = Instant::now();
        store.release_handle_within(budget);
        let took = started.elapsed();
        assert!(took >= budget, "deferred after {took:?}");
        assert!(took < budget + SLACK, "took {took:?}");
        assert_eq!(store.handles(), 1, "the handle is still counted until the drain");
        assert_eq!(store.deferred_releases.load(Ordering::SeqCst), 1);
        // The last `Arc` this thread had goes here.
    });
    let store = weak.upgrade().expect("the store left the registry with a release still deferred");
    assert!(
        GATE.try_enter_shared().is_none_or(|gate| registry(&gate).iter().any(|w| w.ptr_eq(&weak))),
        "the store left the registry"
    );
    drained(&store);
    assert_eq!(store.handles(), 0);
    assert!(store.is_retired(), "the drain retired the store");
    assert!(store.pin.lock().unwrap().is_none(), "the drain closed the pin");
    let gate = GATE.enter_shared_blocking();
    assert_eq!(store.pool.idle(&gate), 0);
    drop(gate);
    assert_eq!(fds_inside(tmp.path()), 0, "D-018 holds once the release has drained");
}

#[test]
fn row_5_a_release_beside_an_ordinary_fork_waits_it_out_and_defers_nothing() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-release-ordinary");
    let store = open_store(tmp.path());
    gate::begin_fork();
    let releaser = {
        let store = Arc::clone(&store);
        std::thread::spawn(move || store.release_handle())
    };
    std::thread::sleep(Duration::from_millis(50));
    gate::end_fork_parent();
    releaser.join().unwrap();
    assert_eq!(store.deferred_releases.load(Ordering::SeqCst), 0, "an ordinary fork deferred a release");
    assert_eq!(store.handles(), 0);
    assert!(store.is_retired());
}

/// The deferrer's own try: with the fork already over, no hook is coming to drain this.
#[test]
fn row_5_a_deferrer_with_no_fork_pending_releases_for_itself() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-release-own-try");
    let store = open_store(tmp.path());
    store.defer_release();
    assert_eq!(store.deferred_releases.load(Ordering::SeqCst), 0, "the deferrer left it for a drain nobody will run");
    assert_eq!(store.handles(), 0);
    assert!(store.is_retired());
    assert!(store.pin.lock().unwrap().is_none());
}

#[test]
fn row_5_the_release_budget_is_a_flat_second() {
    assert_eq!(RELEASE_BUDGET, Duration::from_secs(1));
}

/// A fork ending just as the release is recorded: nothing is stranded, however it lands.
#[test]
fn row_5_a_deferral_racing_the_end_of_a_fork_is_never_stranded() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-release-race");
    let store = open_store(tmp.path());
    store.release_handle();
    for i in 0..30 {
        add_handle(&store);
        gate::begin_fork();
        let budget = Duration::from_millis(1);
        let releaser = {
            let store = Arc::clone(&store);
            std::thread::spawn(move || store.release_handle_within(budget))
        };
        if i % 2 == 0 {
            std::thread::sleep(budget);
        }
        gate::end_fork_parent();
        drain_deferred_releases();
        releaser.join().unwrap();
        // The fork mutex keeps other forks out, so this needs no further drain. Either the fork was
        // pending at the deferrer's try, so the drain above saw the flag, or the try got in.
        assert_eq!(store.deferred_releases.load(Ordering::SeqCst), 0, "a deferred release was stranded");
        assert_eq!(store.handles(), 0);
        assert!(store.is_retired());
    }
    assert_eq!(fds_inside(tmp.path()), 0);
}

fn add_handle(store: &Store) {
    store.add_handle_until(Instant::now() + T, || true).unwrap();
}

// -- rows 6 and 7: reads -------------------------------------------------------------------------

#[test]
fn rows_6_and_7_a_read_behind_a_fork_times_out_and_gives_back_its_slot() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-read");
    let store = open_store(tmp.path());
    put(&store, "k");
    behind_a_fork(|| {
        times_out(|| store.contains(SHORT, "k", NOW));
        assert_eq!(store.slots.in_use(), 0, "the timed-out read kept its slot");
    });
    assert!(store.contains(T, "k", NOW).unwrap());
}

/// A read that leaves the fast path because the pool is full: the slot comes free partway through
/// its budget, so it times out at the gate in the full loop, not at the slot.
#[test]
fn row_7_the_full_loop_also_times_out() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-read-loop");
    let store = Store::open(&OpenOptions { max_connections: 1, ..options(tmp.path(), T) }).unwrap();
    let permit = store.slots.try_acquire(1).unwrap();
    gate::begin_fork();
    let fork_timeouts = GATE.stats().fork_timeouts;
    let (done, finished) = mpsc::channel();
    std::thread::scope(|s| {
        s.spawn(|| {
            times_out(|| store.len(SHORT));
            let _ = done.send(());
        });
        std::thread::sleep(SHORT / 3);
        drop(permit);
        end_fork_after(&finished);
    });
    // At least: a test not holding the fork mutex can time out behind this fork too.
    assert!(GATE.stats().fork_timeouts > fork_timeouts, "it timed out at the slot, not the gate");
    assert_eq!(store.slots.in_use(), 0);
}

fn big(data: &[u8]) -> Value<'_> {
    Value::Bytes { mode: MODE_BYTES, data }
}

/// Removes every value file under `dir`.
fn remove_value_files(dir: &Path) {
    fn walk(dir: &Path) {
        for entry in std::fs::read_dir(dir).unwrap().filter_map(std::result::Result::ok) {
            let path = entry.path();
            if path.is_dir() {
                walk(&path);
            } else {
                std::fs::remove_file(path).unwrap();
            }
        }
    }
    for entry in std::fs::read_dir(dir).unwrap().filter_map(std::result::Result::ok) {
        if entry.path().is_dir() {
            walk(&entry.path());
        }
    }
}

/// One budget across two forks and a missing file. The first read spends part of it behind fork A in
/// its slow path; a fresh `timeout` behind fork B would be twice the budget. `refuse`, which runs
/// between the first read and the file read with nothing held, starts fork B, so this can't race.
#[test]
fn get_with_stays_within_one_timeout_across_two_forks_and_a_missing_file() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-get-with");
    let store = open_store(tmp.path());
    let data = vec![b'x'; spill::THRESHOLD];
    store.set(T, "k", big(&data), None, NOW).unwrap();
    remove_value_files(tmp.path());

    let timeout = Duration::from_millis(600);
    let fork_a = Duration::from_millis(300);
    let (start_b, wait_for_b) = mpsc::channel();
    let (b_claimed, wait_for_claim) = mpsc::channel();
    let (done, finished) = mpsc::channel();
    gate::begin_fork();
    {
        let store = Arc::clone(&store);
        std::thread::spawn(move || {
            let started = Instant::now();
            let asked = AtomicBool::new(false);
            let result = store.get_with(
                timeout,
                "k",
                NOW,
                |_| {
                    if !asked.swap(true, Ordering::SeqCst) {
                        start_b.send(()).unwrap();
                        wait_for_claim.recv().unwrap();
                    }
                    false
                },
                |_, _: ValueRef<'_>| Ok(()),
            );
            let _ = done.send((result, started.elapsed()));
        });
    }
    std::thread::sleep(fork_a);
    gate::end_fork_parent();
    wait_for_b.recv_timeout(T).expect("the first read never returned");
    gate::begin_fork();
    b_claimed.send(()).unwrap();
    let (result, took) = end_fork_after(&finished);
    assert!(matches!(result, Err(Error::Timeout { seconds }) if (seconds - timeout.as_secs_f64()).abs() < 1e-9));
    assert!(took >= timeout, "gave up after {took:?}");
    // A fresh timeout behind fork B would take fork_a + timeout, 900 ms.
    assert!(took + Duration::from_millis(50) < fork_a + timeout, "the second read got a fresh budget: {took:?}");
}

/// After a fast-path first read, the budget starts at the first missing file.
#[test]
fn get_with_starts_its_budget_at_the_first_missing_file_after_a_fast_path_read() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-get-with-fast");
    let store = open_store(tmp.path());
    let data = vec![b'x'; spill::THRESHOLD];
    store.set(T, "k", big(&data), None, NOW).unwrap();
    remove_value_files(tmp.path());
    let (start, wait_for_start) = mpsc::channel();
    let (claimed, wait_for_claim) = mpsc::channel();
    let (done, finished) = mpsc::channel();
    {
        let store = Arc::clone(&store);
        std::thread::spawn(move || {
            let asked = AtomicBool::new(false);
            let result = store.get_with(
                SHORT,
                "k",
                NOW,
                |_| {
                    if !asked.swap(true, Ordering::SeqCst) {
                        start.send(()).unwrap();
                        wait_for_claim.recv().unwrap();
                    }
                    false
                },
                |_, _: ValueRef<'_>| Ok(()),
            );
            let _ = done.send(result);
        });
    }
    wait_for_start.recv_timeout(T).expect("the first read never returned");
    gate::begin_fork();
    let claim_time = Instant::now();
    claimed.send(()).unwrap();
    let result = end_fork_after(&finished);
    let took = claim_time.elapsed();
    assert!(matches!(result, Err(Error::Timeout { .. })), "{result:?}");
    // The budget starts at the missing file, which is after the claim.
    assert!(took >= SHORT, "gave up after {took:?}");
    assert!(took < SHORT + SLACK, "took {took:?}");
}

// -- row 8: writes -------------------------------------------------------------------------------

#[test]
fn row_8_a_write_behind_a_fork_times_out_and_gives_everything_back() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-write");
    let store = open_store(tmp.path());
    behind_a_fork(|| {
        times_out(|| store.set(SHORT, "k", Value::Int(1), None, NOW));
        assert_eq!(store.slots.in_use(), 0, "the timed-out write kept its slot");
        assert!(store.write_lock.try_lock().is_some(), "the timed-out write kept the write lock");
    });
    assert!(!store.contains(T, "k", NOW).unwrap());
    assert!(store.set(T, "k", Value::Int(1), None, NOW).unwrap());
}

#[test]
fn row_8_a_spilled_write_behind_a_fork_discards_its_file() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-write-spill");
    let store = open_store(tmp.path());
    let data = vec![b'x'; spill::THRESHOLD];
    behind_a_fork(|| {
        times_out(|| store.set(SHORT, "k", big(&data), None, NOW));
        assert_eq!(file_count(tmp.path()), 0, "the timed-out write left its value file");
        assert!(store.write_lock.try_lock().is_some(), "the timed-out write kept the write lock");
    });
}

// -- row 9: checkpoints --------------------------------------------------------------------------

#[test]
fn row_9_a_checkpoint_behind_a_fork_returns_false_at_its_deadline_and_starts_no_backoff() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-checkpoint");
    let store = open_store(tmp.path());
    put(&store, "k");
    behind_a_fork(|| {
        let started = Instant::now();
        assert!(!store.checkpoint(SHORT).unwrap(), "it can't have checkpointed beside a fork");
        let took = started.elapsed();
        assert!(took >= SHORT, "gave up after {took:?}");
        assert!(took < SHORT + SLACK, "took {took:?}");
    });
    let stats = store.wal_stats();
    assert_eq!(stats.fork_pending, 1, "{stats:?}");
    assert_eq!(stats.busy, 0, "a fork counted as WAL contention: {stats:?}");
    assert!(!store.wal.in_backoff(), "an ordinary fork started the backoff");
    assert!(store.checkpoint(T).unwrap());
}

/// Writers queued behind a fork hold the write lock, so a checkpoint times out there: still the fork.
#[test]
fn row_9_a_checkpoint_timing_out_on_the_write_lock_behind_a_fork_counts_as_the_fork() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-checkpoint-lock");
    let store = open_store(tmp.path());
    let token = store.write_lock.try_lock().unwrap();
    behind_a_fork(|| assert!(!store.checkpoint(SHORT).unwrap()));
    let stats = store.wal_stats();
    assert_eq!((stats.fork_pending, stats.busy), (1, 0), "{stats:?}");
    assert!(!store.wal.in_backoff());
    // With no fork, the same wait is contention.
    assert!(!store.checkpoint(SHORT).unwrap());
    let stats = store.wal_stats();
    assert_eq!((stats.fork_pending, stats.busy), (1, 1), "{stats:?}");
    drop(token);
}

#[test]
fn row_9_the_automatic_checkpoint_never_waits_behind_a_fork() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-auto-checkpoint");
    let store = Store::open(&OpenOptions {
        wal_size_limit: crate::wal::MIN_SIZE_LIMIT,
        auto_checkpoint: false,
        ..options(tmp.path(), T)
    })
    .unwrap();
    // A reader's open snapshot keeps the WAL from resetting under the burst.
    let reader = Connection::open(open::db_path(tmp.path())).unwrap();
    reader.execute_batch("BEGIN; SELECT count(*) FROM cache;").unwrap();
    let data = vec![b'x'; 2 << 10];
    for i in 0..1024 {
        store.set(T, &format!("k{i}"), Value::Bytes { mode: MODE_BYTES, data: &data }, None, NOW).unwrap();
    }
    reader.execute_batch("ROLLBACK").unwrap();
    assert!(store.wal_bytes() > crate::wal::MIN_SIZE_LIMIT, "the WAL has to be over the limit");
    behind_a_fork(|| {
        let started = Instant::now();
        store.maybe_checkpoint();
        assert!(started.elapsed() < SLACK, "it waited behind the fork");
    });
    let stats = store.wal_stats();
    assert_eq!((stats.fork_pending, stats.busy), (1, 0), "{stats:?}");
    assert!(!store.wal.in_backoff());
}

// -- row 10: nowait ------------------------------------------------------------------------------

#[test]
fn row_10_nowait_writes_skip_at_once_behind_a_fork() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-nowait");
    let store = open_store(tmp.path());
    store.set(T, "k", Value::Int(1), None, NOW).unwrap();
    let hit = store.get_with(T, "k", NOW, |_| false, |_, _| Ok(())).unwrap().unwrap();
    behind_a_fork(|| {
        let started = Instant::now();
        assert_eq!(store.delete_version_nowait(hit.version), crate::ops::VersionDelete::Skipped);
        assert!(store.write_nowait(|_| Ok(())).is_none());
        assert!(started.elapsed() < SLACK, "it waited behind the fork");
    });
    assert!(store.contains(T, "k", NOW).unwrap());
}

// -- rows 11 to 14: held transactions ------------------------------------------------------------

/// A fork stuck on the hold this thread owns: `check` runs on this thread once it's pending, and the
/// hold ends whatever `check` did.
fn with_a_fork_stuck_on_a_hold(store: &Arc<Store>, check: impl FnOnce()) {
    let held = store.begin_hold(T).unwrap();
    let forked = Arc::new(AtomicBool::new(false));
    let fork_thread = {
        let forked = Arc::clone(&forked);
        std::thread::spawn(move || {
            gate::begin_fork();
            forked.store(true, Ordering::Release);
            gate::end_fork_parent();
        })
    };
    eventually(|| GATE.stats().pending);
    check();
    assert!(!forked.load(Ordering::Acquire), "the fork didn't wait for the hold");
    held.finish(true).unwrap();
    fork_thread.join().unwrap();
    assert!(forked.load(Ordering::Acquire));
}

/// On another thread, reporting to this one through a channel, so this thread (which owns the hold
/// the fork waits for) never depends on it without a limit.
fn on_another_thread<R: Send + 'static>(work: impl FnOnce() -> R + Send + 'static) -> R {
    let (done, finished) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = done.send(work());
    });
    finished.recv_timeout(T).expect("the watchdog: a call behind a fork didn't return")
}

#[test]
fn row_11_a_non_owner_gets_a_deadline_and_the_owner_is_unaffected() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-pinned");
    let store = open_store(tmp.path());
    let other = Arc::clone(&store);
    with_a_fork_stuck_on_a_hold(&store, move || {
        // The owner's operations run inside its transaction, past the fork.
        put(&other, "inside");
        assert!(other.contains(T, "inside", NOW).unwrap());
        let reader = Arc::clone(&other);
        on_another_thread(move || times_out(|| reader.contains(SHORT, "inside", NOW)));
        put(&other, "still-inside");
    });
    assert!(store.contains(T, "still-inside", NOW).unwrap());
}

#[test]
fn rows_12_and_13_begin_hold_behind_a_fork_is_a_timeout_not_a_false_nesting() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-begin-hold");
    let store = open_store(tmp.path());
    let other_store = open_store(&tmp.path().join("other"));
    let owner = Arc::clone(&store);
    with_a_fork_stuck_on_a_hold(&store, move || {
        // A real one is still reported, past the fork.
        assert!(matches!(owner.begin_hold(T), Err(Error::NestedTransaction)));
        // Not nested, on a store whose write lock is free: it reaches the gate and times out there.
        let other = Arc::clone(&other_store);
        on_another_thread(move || {
            let result = other.begin_hold(SHORT);
            assert!(matches!(result, Err(Error::Timeout { .. })), "{result:?}");
            assert!(other.write_lock.try_lock().is_some(), "a timed-out hold kept the write lock");
        });
    });
}

// -- rows 16 and 17: drops -----------------------------------------------------------------------

#[test]
fn row_16_a_pin_dropped_behind_a_fork_leaks_its_fd_and_closes_nothing() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-pin");
    let store = open_store(tmp.path());
    let db_path = open::db_path(tmp.path());
    let gate = GATE.enter_shared_blocking();
    let (pin, _) = open_pin(&gate, &db_path).unwrap();
    drop(gate);
    let (pin_tx, pin_rx) = mpsc::channel();
    pin_tx.send(pin).unwrap();
    let dir = tmp.path();
    behind_a_fork(move || {
        // Counted here, since claiming the fork closes the idle connections.
        let before = fds_inside(dir);
        drop(pin_rx.recv().unwrap());
        assert_eq!(fds_inside(dir), before, "the pin was closed outside the gate");
    });
    store.release_handle();
}

#[test]
fn row_17_a_pool_dropped_behind_a_fork_is_closed_by_that_fork() {
    let _forks = one_global_fork_at_a_time();
    let tmp = TempDir::new("fork-pool");
    let store = open_store(tmp.path());
    // A pool with one idle connection on a file of its own, dropped while a fork waits for a level.
    let gate = GATE.enter_shared_blocking();
    let pool = Pool::new(&gate, 2);
    let pooled = pool.checkout(&gate, || open::connect(tmp.path(), T)).unwrap();
    pool.checkin(&gate, pooled);
    let weak_idle = pool.idle(&gate);
    assert_eq!(weak_idle, 1);
    store.release_handle();
    let baseline = fds_inside(tmp.path());
    assert!(baseline > 0, "the pool's connection is open");
    let (dropped, wait_for_drop) = mpsc::channel::<()>();
    let fork_thread = std::thread::spawn(|| {
        gate::begin_fork();
        gate::end_fork_parent();
    });
    eventually(|| GATE.stats().pending);
    let drop_thread = std::thread::spawn(move || {
        drop(pool);
        dropped.send(()).unwrap();
    });
    wait_for_drop.recv_timeout(T).expect("the watchdog: dropping a pool waited for a fork");
    drop_thread.join().unwrap();
    assert_eq!(fds_inside(tmp.path()), baseline, "the pool closed a connection outside the gate");
    // This thread's level is what the fork was waiting for.
    drop(gate);
    fork_thread.join().unwrap();
    assert_eq!(fds_inside(tmp.path()), 0, "the fork didn't close the dropped pool's connection");
}
