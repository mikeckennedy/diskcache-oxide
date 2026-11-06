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
