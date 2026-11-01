//! Value files: values of [`THRESHOLD`] bytes or more live in their own file (04-design.md section 4).
//!
//! **Write:** the value goes to `aa/bb/<32 random hex>.tmp` with no lock or connection held. Inside
//! the write transaction the temp file is renamed to `.val` and the row is upserted pointing at it;
//! the replaced version's file is unlinked only after the commit. Names are never reused.
//!
//! **Read:** read the row, then the file, with no transaction held. The file's length must equal the
//! row's `size` (no fsync, so a crash can leave a short file). A missing or short file means the
//! value was just overwritten (retry with the new row) or is truly gone (a miss, and the D-013
//! compare-and-delete). After [`OPTIMISTIC_ATTEMPTS`] tries a reader takes the row and opens the
//! file while holding the write lock, so it can't keep losing to a fast writer (D-016).

use std::fs::{DirBuilder, File, OpenOptions as FileOptions};
use std::io::{ErrorKind, Read, Write};
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};

use crate::{Error, Result, gate};

/// Values this size or larger are stored in a value file (the conformance adapter's
/// `spill_threshold`).
pub const THRESHOLD: usize = 64 * 1024;

/// Optimistic read attempts before the locked fallback.
pub const OPTIMISTIC_ATTEMPTS: usize = 8;

/// A value file created for a write, not yet committed.
#[derive(Debug)]
pub struct PendingFile {
    /// Relative to the cache directory, as stored in the `filename` column (`aa/bb/<hex>.val`).
    pub relative: String,
    tmp: PathBuf,
    val: PathBuf,
}

impl PendingFile {
    /// Writes `data` to a new temp file under `directory`, with the interpreter released.
    pub fn write(directory: &Path, data: &[u8]) -> Result<Self> {
        let hex = random_hex()?;
        let relative_dir = format!("{}/{}", &hex[0..2], &hex[2..4]);
        let parent = directory.join(&relative_dir);
        let relative = format!("{relative_dir}/{hex}.val");
        let pending = Self { relative, tmp: parent.join(format!("{hex}.tmp")), val: parent.join(format!("{hex}.val")) };
        let mut outcome = Ok(());
        {
            let outcome_slot = std::sync::Mutex::new(&mut outcome);
            gate::run_blocking(&|| {
                let result = (|| -> std::io::Result<()> {
                    DirBuilder::new().recursive(true).mode(0o700).create(&parent)?;
                    let mut file = FileOptions::new().write(true).create_new(true).open(&pending.tmp)?;
                    file.write_all(data)
                })();
                if let Err(error) = result
                    && let Ok(mut slot) = outcome_slot.lock()
                {
                    **slot = Err(error);
                }
            });
        }
        outcome.map_err(|source| io_error(&pending.tmp, source))?;
        Ok(pending)
    }

    /// Renames the temp file into place. Called inside the write transaction (under the write
    /// lock); safe to call again if a retried attempt already did it.
    pub fn commit_name(&self) -> Result<()> {
        match std::fs::rename(&self.tmp, &self.val) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == ErrorKind::NotFound && self.val.exists() => Ok(()),
            Err(e) => Err(io_error(&self.tmp, e)),
        }
    }

    /// Removes both names after a write that didn't commit.
    pub fn discard(&self) {
        let _ = std::fs::remove_file(&self.tmp);
        let _ = std::fs::remove_file(&self.val);
    }
}

/// Reads a value file whose row says it holds `size` bytes. `Ok(None)` if it's missing or its
/// length doesn't match (treated alike: the value isn't readable from this file).
pub fn read(directory: &Path, relative: &str, size: i64) -> Result<Option<Vec<u8>>> {
    let path = directory.join(relative);
    match File::open(&path) {
        Ok(file) => read_open(&file, &path, size),
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(None),
        Err(e) => Err(io_error(&path, e)),
    }
}

/// Opens a value file (for the locked fallback, which reads it after releasing the lock).
pub fn open(directory: &Path, relative: &str) -> Result<Option<(File, PathBuf)>> {
    let path = directory.join(relative);
    match File::open(&path) {
        Ok(file) => Ok(Some((file, path))),
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(None),
        Err(e) => Err(io_error(&path, e)),
    }
}

/// Reads an open value file, checking its length against `size`.
pub fn read_open(file: &File, path: &Path, size: i64) -> Result<Option<Vec<u8>>> {
    let Ok(expected) = u64::try_from(size) else { return Ok(None) };
    let mut outcome = Ok(None);
    {
        let slot = std::sync::Mutex::new(&mut outcome);
        gate::run_blocking(&|| {
            if let Ok(mut slot) = slot.lock() {
                **slot = read_checked(file, expected);
            }
        });
    }
    outcome.map_err(|source| io_error(path, source))
}

/// `expected` comes from the database, so a damaged row can say anything: nothing is allocated from
/// it until the file agrees, and an allocation that fails is an error, not an abort. The read stops
/// one byte past `expected`, which is enough to see the file is longer, even if it grew since the
/// length check.
fn read_checked(file: &File, expected: u64) -> std::io::Result<Option<Vec<u8>>> {
    if file.metadata()?.len() != expected {
        return Ok(None);
    }
    let limit = expected.saturating_add(1);
    let mut data = Vec::new();
    usize::try_from(limit).ok().and_then(|room| data.try_reserve_exact(room).ok()).ok_or(ErrorKind::OutOfMemory)?;
    file.take(limit).read_to_end(&mut data)?;
    Ok((u64::try_from(data.len()) == Ok(expected)).then_some(data))
}

fn io_error(path: &Path, source: std::io::Error) -> Error {
    if source.raw_os_error() == Some(libc_enospc()) {
        Error::DiskFull { source: Box::new(source) }
    } else {
        Error::Io { path: path.to_path_buf(), source }
    }
}

/// `ENOSPC`, without a libc dependency in core (28 on Linux and macOS).
const fn libc_enospc() -> i32 {
    28
}

const HEX: &[u8; 16] = b"0123456789abcdef";

/// 128 random bits as 32 lowercase hex characters, from the OS's random source.
fn random_hex() -> Result<String> {
    let mut bytes = [0u8; 16];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|source| Error::Io { path: PathBuf::from("/dev/urandom"), source })?;
    Ok(bytes
        .iter()
        .flat_map(|b| [char::from(HEX[usize::from(b >> 4)]), char::from(HEX[usize::from(b & 0xF)])])
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TempDir;

    #[test]
    fn write_commit_read_and_discard() {
        let tmp = TempDir::new("spill");
        let data = vec![7u8; THRESHOLD + 1];
        let pending = PendingFile::write(tmp.path(), &data).unwrap();
        assert!(pending.tmp.exists() && !pending.val.exists());
        pending.commit_name().unwrap();
        pending.commit_name().unwrap(); // A retried attempt is harmless.
        let size = i64::try_from(data.len()).unwrap();
        assert_eq!(read(tmp.path(), &pending.relative, size).unwrap(), Some(data));
        assert_eq!(read(tmp.path(), &pending.relative, size + 1).unwrap(), None, "a length mismatch reads as missing");
        pending.discard();
        assert_eq!(read(tmp.path(), &pending.relative, size).unwrap(), None);
    }

    #[test]
    fn a_size_the_file_doesnt_match_reads_as_missing_without_allocating_it() {
        let tmp = TempDir::new("spill-corrupt-size");
        let pending = PendingFile::write(tmp.path(), b"x").unwrap();
        pending.commit_name().unwrap();
        // Allocating any of these up front aborts the process, or takes all its memory.
        for size in [i64::MAX, 1 << 62, 1 << 40, -1, 0, 2] {
            assert_eq!(read(tmp.path(), &pending.relative, size).unwrap(), None, "size {size}");
        }
    }

    #[test]
    fn a_file_longer_than_its_size_reads_as_missing() {
        let tmp = TempDir::new("spill-long");
        let pending = PendingFile::write(tmp.path(), &vec![7u8; THRESHOLD + 1]).unwrap();
        pending.commit_name().unwrap();
        let size = i64::try_from(THRESHOLD).unwrap();
        assert_eq!(read(tmp.path(), &pending.relative, size).unwrap(), None);
    }

    #[test]
    fn a_size_too_big_to_allocate_is_out_of_memory() {
        let tmp = TempDir::new("spill-huge");
        let pending = PendingFile::write(tmp.path(), b"x").unwrap();
        pending.commit_name().unwrap();
        // A sparse file that agrees with its row, past any address space. Not every filesystem
        // allows one, and there's nothing to check on those.
        let size = 1i64 << 54;
        let file = FileOptions::new().write(true).open(&pending.val).unwrap();
        if file.set_len(size.unsigned_abs()).is_err() {
            return;
        }
        let error = read(tmp.path(), &pending.relative, size).unwrap_err();
        assert!(error.is_out_of_memory(), "{error:?}");
        assert!(!error.is_operational(), "{error:?}");
    }

    #[test]
    fn names_are_unique_and_directories_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let tmp = TempDir::new("spill-names");
        let a = PendingFile::write(tmp.path(), b"x").unwrap();
        let b = PendingFile::write(tmp.path(), b"x").unwrap();
        assert_ne!(a.relative, b.relative);
        let dir = a.tmp.parent().unwrap();
        assert_eq!(std::fs::metadata(dir).unwrap().permissions().mode() & 0o777, 0o700);
    }
}
