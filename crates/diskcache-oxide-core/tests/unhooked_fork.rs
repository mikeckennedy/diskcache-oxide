//! A child forked with no before-fork hook at all can't use a cache its parent had a connection
//! open on (plan 32). Nothing closed the parent's idle connections, so the child inherits them, and
//! with them SQLite's lock record for the file: a new connection there would take no kernel lock,
//! read stale data once the parent closed, and lose its writes. `end_fork_child`'s scan finds the
//! connections and every call raises [`Error::Unusable`] instead, while a cache the child opens for
//! itself works. Python only skips a before-hook when an earlier one raised, so this is the one
//! place that fork is tested for real.
//!
//! Its own test binary, like `unclaimed_fork.rs` and for the same reasons: `end_fork_child` runs
//! once per pid and bumps the global generation, and a raw fork beside another test's thread could
//! leave the child stuck on a mutex that thread held. One test, so nothing else runs beside it.

// Windows has no fork().
#![cfg(unix)]
// One raw `fork()`, `waitpid()` and `_exit()`; the library itself denies unsafe code.
#![allow(unsafe_code)]

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use diskcache_oxide_core::open::{Requested, Role};
use diskcache_oxide_core::schema::DEFAULT_SIZE_LIMIT;
use diskcache_oxide_core::store::{
    DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, OpenOptions, Store,
};
use diskcache_oxide_core::{Error, gate};

const T: Duration = Duration::from_secs(5);

/// A fresh directory under the system temp dir, removed on drop.
struct Scratch(PathBuf);

impl Scratch {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("dco-unhooked-fork-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir); // A crashed earlier run with this pid.
        Self(dir)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn open(dir: &Scratch) -> diskcache_oxide_core::Result<Arc<Store>> {
    Store::open(&OpenOptions {
        directory: dir.0.clone(),
        timeout: T,
        requested: Requested::default(),
        max_connections: DEFAULT_MAX_CONNECTIONS,
        cache_pages: DEFAULT_CACHE_PAGES,
        wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
        auto_checkpoint: false,
        role: Role::Plain,
        default_size_limit: DEFAULT_SIZE_LIMIT,
        maintenance: diskcache_oxide_core::background::Maintenance::OFF,
    })
}

/// What the child exits with: 0 when every check held, else the first that didn't.
const READ_WORKED: i32 = 10;
const WRITE_WORKED: i32 = 11;
const REOPEN_WORKED: i32 = 12;
const OWN_CACHE_FAILED: i32 = 13;
const PANICKED: i32 = 14;

#[test]
#[cfg_attr(miri, ignore = "forks")]
fn a_child_forked_with_no_before_hook_cant_use_a_cache_it_inherited_a_connection_to() {
    let (inherited_dir, own_dir) = (Scratch::new("inherited"), Scratch::new("own"));
    let store = open(&inherited_dir).unwrap();
    // Leaves a connection idle in the pool, which is what a before-fork hook would have closed.
    store.write(T, |_, _| Ok(())).unwrap();

    // SAFETY: the child calls only the library's after-fork hook and store operations, on a
    // single-threaded process (this test binary has one test, and it started no thread), then
    // `_exit`s without unwinding into the test harness.
    let pid = unsafe { libc::fork() };
    assert!(pid >= 0, "fork failed");
    if pid == 0 {
        let code = std::panic::catch_unwind(|| {
            gate::end_fork_child();
            if !matches!(store.len(T), Err(Error::Unusable)) {
                return READ_WORKED;
            }
            if !matches!(store.write(T, |_, _| Ok(())), Err(Error::Unusable)) {
                return WRITE_WORKED;
            }
            if !matches!(open(&inherited_dir), Err(Error::Unusable)) {
                return REOPEN_WORKED;
            }
            // A cache nothing was open on at the fork is the child's own.
            match open(&own_dir) {
                Ok(own) if own.write(T, |_, _| Ok(())).is_ok() && own.len(T).is_ok() => 0,
                _ => OWN_CACHE_FAILED,
            }
        })
        .unwrap_or(PANICKED);
        // SAFETY: ends the child at once, with no atexit handlers or destructors.
        unsafe { libc::_exit(code) };
    }

    let mut status = 0;
    // SAFETY: waits for the child forked above.
    let waited = unsafe { libc::waitpid(pid, &raw mut status, 0) };
    assert_eq!(waited, pid);
    assert!(libc::WIFEXITED(status), "the child did not exit normally: status {status}");
    assert_eq!(
        libc::WEXITSTATUS(status),
        0,
        "in the child: 10 a read worked, 11 a write worked, 12 a reopen worked, 13 its own cache failed, 14 panic"
    );

    // The parent is untouched: it never ran a hook, and nothing is poisoned here.
    assert_eq!(store.len(T).unwrap(), 0);
    store.write(T, |_, _| Ok(())).unwrap();
}
