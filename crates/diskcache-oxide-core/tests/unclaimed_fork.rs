//! A child forked while another thread held the fork claim gets a working gate (plan 23, step 3,
//! item 4): `end_fork_child`'s unclaimed arm, for a fork whose before-hook ran on another thread or
//! not at all. Its own test binary, so it's its own process: `end_fork_child` runs once per pid and
//! bumps the global generation, and a raw fork while another test's thread holds a std mutex could
//! leave the child stuck on it. One test, so nothing else in this process runs beside it.

// Windows has no fork().
#![cfg(unix)]
// One raw `fork()`, `waitpid()` and `_exit()`; the library itself denies unsafe code.
#![allow(unsafe_code)]

use std::path::PathBuf;
use std::sync::mpsc;
use std::time::Duration;

use diskcache_oxide_core::open::{Requested, Role};
use diskcache_oxide_core::schema::DEFAULT_SIZE_LIMIT;
use diskcache_oxide_core::store::{
    DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, OpenOptions, Store,
};
use diskcache_oxide_core::{Error, gate};

/// A fresh directory under the system temp dir, removed on drop.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// What the child exits with: 0 when both calls worked, else which one failed.
const READ_FAILED: i32 = 10;
const WRITE_FAILED: i32 = 11;
const PANICKED: i32 = 12;

#[test]
#[cfg_attr(miri, ignore = "forks")]
fn a_child_forked_under_another_threads_claim_can_use_the_cache() {
    let dir = Scratch(std::env::temp_dir().join(format!("dco-unclaimed-fork-{}", std::process::id())));
    let _ = std::fs::remove_dir_all(&dir.0); // A crashed earlier run with this pid.
    let store = Store::open(&OpenOptions {
        directory: dir.0.clone(),
        timeout: Duration::from_secs(5),
        requested: Requested::default(),
        max_connections: DEFAULT_MAX_CONNECTIONS,
        cache_pages: DEFAULT_CACHE_PAGES,
        wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
        auto_checkpoint: false,
        role: Role::Plain,
        default_size_limit: DEFAULT_SIZE_LIMIT,
        maintenance: diskcache_oxide_core::background::Maintenance::OFF,
    })
    .unwrap();
    store.write(Duration::from_secs(5), |_, _| Ok(())).unwrap();

    // Thread B runs the before-fork hook and keeps its claim until told to end it.
    let (claimed, wait_claim) = mpsc::channel();
    let (release, wait_release) = mpsc::channel::<()>();
    let thread_b = std::thread::spawn(move || {
        gate::begin_fork();
        claimed.send(()).unwrap();
        wait_release.recv().unwrap();
        gate::end_fork_parent();
    });
    wait_claim.recv().unwrap();

    // While B's claim is pending, this thread can't enter the gate in the parent.
    let short = Duration::from_millis(200);
    assert!(
        matches!(store.len(short), Err(Error::Timeout { .. })),
        "the pending claim must keep the parent's callers out"
    );

    // SAFETY: the child calls only the library's after-fork hook and two store operations (whose
    // locks this thread doesn't hold, and B holds none: its claim is atomics), then `_exit`s
    // without unwinding into the test harness.
    let pid = unsafe { libc::fork() };
    assert!(pid >= 0, "fork failed");
    if pid == 0 {
        let code = std::panic::catch_unwind(|| {
            // This thread never ran the before-hook, so the child's gate still carries B's claim.
            gate::end_fork_child();
            if store.len(short).is_err() {
                return READ_FAILED;
            }
            if store.write(short, |_, _| Ok(())).is_err() {
                return WRITE_FAILED;
            }
            0
        })
        .unwrap_or(PANICKED);
        // SAFETY: ends the child at once, with no atexit handlers or destructors.
        unsafe { libc::_exit(code) };
    }

    let mut status = 0;
    // SAFETY: waits for the child forked above.
    let waited = unsafe { libc::waitpid(pid, &raw mut status, 0) };
    release.send(()).unwrap();
    thread_b.join().unwrap();
    assert_eq!(waited, pid);
    assert!(libc::WIFEXITED(status), "the child did not exit normally: status {status}");
    assert_eq!(libc::WEXITSTATUS(status), 0, "the child's cache calls failed (10 read, 11 write, 12 panic)");

    // And the parent's gate is open again once B ends its claim.
    assert_eq!(store.len(Duration::from_secs(5)).unwrap(), 0);
}
