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

/// Asserts `path` is a directory with `mode` as its permission bits. Windows has no mode bits (a
/// directory takes its parent's default ACL), so there it checks only that `path` is a directory.
pub(crate) fn assert_dir_mode(path: &Path, mode: u32) {
    let metadata = std::fs::metadata(path).unwrap();
    assert!(metadata.is_dir(), "{} is not a directory", path.display());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(metadata.permissions().mode() & 0o777, mode, "{}", path.display());
    }
    #[cfg(not(unix))]
    let _ = mode;
}

/// Sets the mtime of the file or directory at `path`. Windows needs a handle with write-attribute
/// access (a read-only `File::open` is denied) and backup semantics to open a directory at all.
pub(crate) fn set_mtime(path: &Path, at: std::time::SystemTime) {
    #[cfg(unix)]
    let file = std::fs::File::open(path).unwrap();
    #[cfg(windows)]
    let file = {
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_WRITE_ATTRIBUTES: u32 = 0x0100;
        const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
        std::fs::OpenOptions::new()
            .access_mode(FILE_WRITE_ATTRIBUTES)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS)
            .open(path)
            .unwrap()
    };
    file.set_modified(at).unwrap();
}

/// Windows: leaves the file at `path` delete-pending, and returns the handle that keeps it so. The
/// name stays until that handle closes, and every open of it meanwhile fails with
/// `ERROR_ACCESS_DENIED`: what a delete without POSIX semantics (FAT, exFAT, SMB, older builds)
/// leaves while a reader has the file open. A handle opened with `DELETE_ON_CLOSE` and closed while
/// this one is open makes it so on any filesystem.
#[cfg(windows)]
pub(crate) fn delete_pending(path: &Path) -> std::fs::File {
    use std::os::windows::fs::OpenOptionsExt;
    const GENERIC_READ: u32 = 0x8000_0000;
    const DELETE: u32 = 0x0001_0000;
    const FILE_SHARE_ALL: u32 = 0x7;
    const FILE_FLAG_DELETE_ON_CLOSE: u32 = 0x0400_0000;
    let held = std::fs::File::open(path).unwrap();
    let marker = std::fs::OpenOptions::new()
        .access_mode(GENERIC_READ | DELETE)
        .share_mode(FILE_SHARE_ALL)
        .custom_flags(FILE_FLAG_DELETE_ON_CLOSE)
        .open(path)
        .unwrap();
    drop(marker);
    held
}

/// This process's open file descriptors on the file at `path`, pending ones SQLite hasn't closed
/// yet included: `/proc/self/fd` on Linux, `lsof` on macOS (where `stat` of `/dev/fd/N` doesn't
/// reach the file, and opening it to `fstat` would add a descriptor whose close drops SQLite's locks).
/// Unix only: Windows has handles, not descriptors.
#[cfg(unix)]
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
