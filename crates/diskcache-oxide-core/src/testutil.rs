//! Helpers shared by this crate's unit tests.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError};

/// Held by a test that claims a fork on the global gate, so `pending` always means this test's own
/// fork. Take it first, with no gate level held: a test holding a level here would hold up a fork
/// that holds this.
pub(crate) fn one_global_fork_at_a_time() -> MutexGuard<'static, ()> {
    static FORKS: Mutex<()> = Mutex::new(());
    FORKS.lock().unwrap_or_else(PoisonError::into_inner)
}

/// A fresh directory under the system temp dir, removed on drop.
pub(crate) struct TempDir(PathBuf);

impl TempDir {
    pub(crate) fn new(name: &str) -> Self {
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        // The pid and counter alone collide under `-Zmiri-many-seeds`, whose seeds run in parallel
        // with one pid and a counter each starting at 0, and one seed's cleanup would remove
        // another's directory. A random part keeps them apart: `RandomState` draws host entropy
        // (Miri runs these tests with isolation off), and natively the pid already separates processes.
        let salt = std::hash::BuildHasher::hash_one(&std::hash::RandomState::new(), n);
        let path = std::env::temp_dir().join(format!("dco-test-{name}-{}-{n}-{salt:016x}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).unwrap();
        Self(path)
    }

    pub(crate) fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// This process's open file descriptors on the file at `path`, pending ones SQLite hasn't closed
/// yet included: `/proc/self/fd` on Linux, `lsof` on macOS (where `stat` of `/dev/fd/N` doesn't
/// reach the file, and opening it to `fstat` would add a descriptor whose close drops SQLite's locks).
pub(crate) fn fds_on(path: &Path) -> usize {
    let path = std::fs::canonicalize(path).unwrap();
    if cfg!(target_os = "linux") {
        return std::fs::read_dir("/proc/self/fd")
            .unwrap()
            .filter_map(std::result::Result::ok)
            .filter_map(|e| std::fs::read_link(e.path()).ok())
            .filter(|target| *target == path)
            .count();
    }
    let output = std::process::Command::new("lsof")
        .args(["-n", "-P", "-F", "n", "-p", &std::process::id().to_string()])
        .output()
        .unwrap();
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| line.strip_prefix('n'))
        .filter(|name| Path::new(name) == path)
        .count()
}
