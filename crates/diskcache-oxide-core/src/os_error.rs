//! Every raw OS error code core looks at, in one place (plan 25, R-M5). `raw_os_error()` is an
//! errno on Unix and a Win32 error code on Windows, so a bare number means different things per
//! platform: 22 is `EINVAL` on Unix and `ERROR_BAD_COMMAND` on Windows, 28 is `ENOSPC` and
//! `ERROR_OUT_OF_PAPER`. Prefer [`ErrorKind`]; a code goes here only when no kind covers it, named
//! per platform. `tests::raw_codes_live_only_here` keeps every other module off them.

use std::io::{self, ErrorKind};

/// The disk or the user's quota is full: `ENOSPC`/`EDQUOT` on Unix, `ERROR_DISK_FULL`,
/// `ERROR_HANDLE_DISK_FULL` and `ERROR_DISK_QUOTA_EXCEEDED` on Windows. std maps all of them to
/// these two kinds.
pub(crate) fn is_storage_full(error: &io::Error) -> bool {
    matches!(error.kind(), ErrorKind::StorageFull | ErrorKind::QuotaExceeded)
}

/// Codes from `link()` on a filesystem without hard links (FAT, exFAT, SMB, some FUSE), besides
/// [`ErrorKind::Unsupported`]: `EPERM`, and on macOS `ENOTSUP` (45), which std reports as
/// uncategorized (only `EOPNOTSUPP` is `Unsupported`).
#[cfg(target_os = "macos")]
pub(crate) const LINK_UNSUPPORTED: &[i32] = &[1, 45];
#[cfg(all(unix, not(target_os = "macos")))]
pub(crate) const LINK_UNSUPPORTED: &[i32] = &[1];
/// Windows: FAT and exFAT answer `ERROR_INVALID_FUNCTION` (1), some redirectors
/// `ERROR_NOT_SUPPORTED` (50, which std already maps to `Unsupported`).
#[cfg(windows)]
pub(crate) const LINK_UNSUPPORTED: &[i32] = &[1, 50];

/// Whether a failed `link()` means the filesystem has no hard links.
pub(crate) fn is_link_unsupported(error: &io::Error) -> bool {
    error.kind() == ErrorKind::Unsupported || error.raw_os_error().is_some_and(|code| LINK_UNSUPPORTED.contains(&code))
}

/// `EINVAL`, which macOS's APFS can answer for a create whose directory a sweep removes during the
/// lookup (see `spill::directory_vanished`).
#[cfg(unix)]
pub(crate) const EINVAL: i32 = 22;

/// Whether a create's error is the code a directory removed under it can give besides `NotFound`
/// (`EINVAL` on Unix; none yet on Windows, see plan 25 R-H4).
pub(crate) fn is_vanished_directory_code(error: &io::Error) -> bool {
    #[cfg(unix)]
    return error.raw_os_error() == Some(EINVAL);
    #[cfg(not(unix))]
    {
        let _ = error;
        false
    }
}

/// Whether an error can mean the name is delete-pending: on Windows, a name whose delete succeeded
/// while another handle was open lingers until that handle closes, and opening or creating it (or
/// creating inside a pending directory) answers `ERROR_ACCESS_DENIED` (5, which std reports as
/// `PermissionDenied`) or `ERROR_DELETE_PENDING` (303). Never on Unix, where a delete removes the
/// name at once (plan 25, R-H4).
pub(crate) fn is_delete_pending(error: &io::Error) -> bool {
    #[cfg(windows)]
    return matches!(error.raw_os_error(), Some(5 | 303));
    #[cfg(not(windows))]
    {
        let _ = error;
        false
    }
}

/// Windows: `ERROR_SHARING_VIOLATION` and `ERROR_LOCK_VIOLATION`, which another handle on the file
/// or its directory gives for a moment (a sweep's `rmdir` attempt, antivirus, the indexer). std
/// reports both as uncategorized.
#[cfg(windows)]
const SHARING_VIOLATIONS: &[i32] = &[32, 33];

/// The sleeps between [`retry_sharing_violations`]'s tries: about 50 ms in all.
#[cfg(windows)]
const SHARING_RETRY_SLEEPS_MS: &[u64] = &[1, 2, 4, 8, 16, 16];

/// Whether `error` is another handle holding the file for a moment: a sharing or lock violation.
/// Never on Unix.
pub(crate) fn is_sharing_violation(error: &io::Error) -> bool {
    #[cfg(windows)]
    return error.raw_os_error().is_some_and(|code| SHARING_VIOLATIONS.contains(&code));
    #[cfg(not(windows))]
    {
        let _ = error;
        false
    }
}

/// Runs `op`, and on Windows retries a sharing or lock violation a few times over about 50 ms
/// (plan 25, R-M7): another process's brief handle shouldn't fail a write. Any other error, the
/// last violation, and every error on Unix (which has no such thing) are returned as they are.
pub(crate) fn retry_sharing_violations<T>(mut op: impl FnMut() -> io::Result<T>) -> io::Result<T> {
    #[cfg(windows)]
    for &ms in SHARING_RETRY_SLEEPS_MS {
        match op() {
            Err(e) if is_sharing_violation(&e) => {
                std::thread::sleep(std::time::Duration::from_millis(ms));
            }
            other => return other,
        }
    }
    op()
}

/// Tests only: a create error no write retries: `EACCES` on Unix, `ERROR_WRITE_PROTECT` on Windows
/// (where `ERROR_ACCESS_DENIED` can mean a delete-pending directory, so a write retries it).
#[cfg(all(test, unix))]
pub(crate) const CREATE_REFUSED: i32 = 13;
#[cfg(all(test, windows))]
pub(crate) const CREATE_REFUSED: i32 = 19;

/// Tests only: an error carrying the raw `code`, for fault injection.
#[cfg(test)]
pub(crate) fn from_code(code: i32) -> io::Error {
    io::Error::from_raw_os_error(code)
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::*;

    #[test]
    fn raw_codes_live_only_here() {
        fn scan(dir: &Path, found: &mut Vec<String>) {
            for entry in std::fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    scan(&path, found);
                } else if path.extension().is_some_and(|e| e == "rs") && !path.ends_with("os_error.rs") {
                    let text = std::fs::read_to_string(&path).unwrap();
                    for (n, line) in text.lines().enumerate() {
                        if line.contains("raw_os_error") && !line.trim_start().starts_with("//") {
                            found.push(format!("{}:{}", path.display(), n + 1));
                        }
                    }
                }
            }
        }
        let mut found = Vec::new();
        scan(&Path::new(env!("CARGO_MANIFEST_DIR")).join("src"), &mut found);
        assert!(found.is_empty(), "raw OS error codes outside os_error.rs (name them here, per platform): {found:?}");
    }

    #[test]
    fn full_disks_and_quotas_are_storage_full() {
        assert!(is_storage_full(&ErrorKind::StorageFull.into()));
        assert!(is_storage_full(&ErrorKind::QuotaExceeded.into()));
        assert!(!is_storage_full(&ErrorKind::PermissionDenied.into()));
        #[cfg(unix)]
        for code in [28, if cfg!(target_os = "macos") { 69 } else { 122 }] {
            assert!(is_storage_full(&from_code(code)), "ENOSPC and EDQUOT: {code}");
        }
        #[cfg(windows)]
        for code in [112, 39, 1295] {
            assert!(
                is_storage_full(&from_code(code)),
                "ERROR_DISK_FULL, _HANDLE_DISK_FULL, _DISK_QUOTA_EXCEEDED: {code}"
            );
        }
        // 28 on Windows is ERROR_OUT_OF_PAPER, never a full disk.
        #[cfg(windows)]
        assert!(!is_storage_full(&from_code(28)));
    }

    #[test]
    fn the_refused_create_code_is_never_retried() {
        let refused = from_code(CREATE_REFUSED);
        assert_ne!(refused.kind(), ErrorKind::NotFound);
        assert!(!is_vanished_directory_code(&refused) && !is_delete_pending(&refused));
    }

    #[test]
    fn delete_pending_is_windows_only() {
        for code in [5, 303] {
            assert_eq!(is_delete_pending(&from_code(code)), cfg!(windows), "{code}");
        }
    }
}
