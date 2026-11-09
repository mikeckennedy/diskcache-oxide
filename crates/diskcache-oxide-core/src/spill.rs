//! Value files: values of [`THRESHOLD`] bytes or more live in their own file (04-design.md section 4).
//!
//! **Layout:** one level deep, `aa/<32 random hex>.val`, where `aa` is the name's first two
//! characters, so a store never has more than 256 value directories (plan 17). Nothing removes a
//! directory when a file in it goes; [`sweep`] removes the empty ones when a store's last handle is
//! released and after `clear()`.
//!
//! **Write:** the value goes to `aa/<hex>.tmp` with no lock or connection held. Inside the write
//! transaction the temp file is renamed to `.val` and the row is upserted pointing at it; the
//! replaced version's file is unlinked only after the commit. Names are never reused. A sweep can
//! remove `aa` between the write's `mkdir` and its `create_new`, so the write retries that step
//! (see [`PendingFile::write`]); once the `.tmp` exists the directory isn't empty and can't go.
//!
//! **Read:** read the row, then the file, with no transaction held. The file's length must equal the
//! row's `size` (no fsync, so a crash can leave a short file). A missing or short file means the
//! value was just overwritten (retry with the new row) or is truly gone (a miss, and the D-013
//! compare-and-delete). After [`OPTIMISTIC_ATTEMPTS`] tries a reader takes the row under the write
//! lock, opening its file or copying an inline value, so it can't keep losing to a fast writer
//! (D-016).
//!
//! **Names from the database** are checked before they become paths ([`value_path`]): a damaged row
//! reads as a miss and unlinks nothing, instead of reaching outside the cache directory.

use std::ffi::OsString;
use std::fs::{DirBuilder, File, OpenOptions as FileOptions};
use std::hash::{BuildHasher, Hasher, RandomState};
use std::io::{ErrorKind, Read, Write};
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};

use crate::{Error, Result, gate};

/// Values this size or larger are stored in a value file (the conformance adapter's
/// `spill_threshold`).
pub const THRESHOLD: usize = 64 * 1024;

/// Optimistic read attempts before the locked fallback.
pub const OPTIMISTIC_ATTEMPTS: usize = 8;

/// Rounds of "make `aa`, create the temp file" before a write gives up on a directory that keeps
/// vanishing. Only a sweep removes one, and a sweep visits each directory once, so a writer loses a
/// round or two at most; the bound is for a pathological loop, not for traffic.
const CREATE_ROUNDS: usize = 64;

/// How old a value directory must be before the sweep on close may remove it. A directory's mtime
/// changes whenever an entry is added or removed, so this leaves alone any directory a writer or a
/// delete touched recently: no churn on hot directories (Michael, 2026-11-06). Not a safety
/// property: the write's retry is.
pub const SWEEP_MIN_AGE: Duration = Duration::from_secs(30);

/// How long a sweep may run. A placeholder until plan 17's step 4 measures how long one really
/// takes; a sweep cut short leaves the rest for the next.
pub const SWEEP_BUDGET: Duration = Duration::from_millis(100);
/// How long a `.tmp` must have gone unwritten before a sweep removes it as a dead writer's leftover
/// (plan 19). A live `.tmp` lasts one operation: the write of the data, then at most one wait for the
/// write lock, bounded by the handle's `timeout`, before `commit_name` renames it. An hour is far past
/// any sensible timeout. The trade: a live writer that goes an hour without touching its file (paused,
/// or waiting past an hour on the lock) loses it, its `commit_name` fails, and its write raises an
/// I/O error with nothing stored.
pub const TMP_MIN_AGE: Duration = Duration::from_secs(3_600);

/// A value file created for a write, not yet committed.
#[derive(Debug)]
pub struct PendingFile {
    /// Relative to the cache directory, as stored in the `filename` column (`aa/<hex>.val`).
    pub relative: String,
    tmp: PathBuf,
    val: PathBuf,
}

impl PendingFile {
    /// Writes `data` to a new temp file under `directory`, with the interpreter released.
    pub fn write(directory: &Path, data: &[u8]) -> Result<Self> {
        Self::write_named(directory, &random_hex()?, data)
    }

    /// [`PendingFile::write`] with the name given: 32 lowercase hex characters. Tests use it to put
    /// several files in one directory.
    ///
    /// Each round makes `directory/aa` (0700, non-recursive) and then creates the temp file in it.
    /// `AlreadyExists` from the `mkdir` means go on, with no `stat` (one would race a sweep). Its
    /// `NotFound` is terminal: with a non-recursive `mkdir` that means the cache directory itself is
    /// gone, and nothing in oxide removes it, so the write fails instead of re-creating the tree. A
    /// `NotFound` from the create (or macOS's `EINVAL`, [`directory_vanished`]) means a sweep removed
    /// `aa` after the `mkdir`: the round starts
    /// again, up to [`CREATE_ROUNDS`]. A directory this call made is removed again if the write
    /// fails, so a failed create leaves nothing behind (if another file took it meanwhile, the
    /// `rmdir` fails and it stays).
    pub(crate) fn write_named(directory: &Path, hex: &str, data: &[u8]) -> Result<Self> {
        debug_assert!(is_hex(hex, 32), "a value file's name is 32 lowercase hex characters: {hex:?}");
        let dir_name = &hex[0..2];
        let parent = directory.join(dir_name);
        let relative = format!("{dir_name}/{hex}.val");
        let pending = Self { relative, tmp: parent.join(format!("{hex}.tmp")), val: parent.join(format!("{hex}.val")) };
        let mut outcome = Ok(());
        {
            let outcome_slot = std::sync::Mutex::new(&mut outcome);
            gate::run_blocking(&|| {
                if let Err(error) = create_in(&parent, &pending.tmp, data)
                    && let Ok(mut slot) = outcome_slot.lock()
                {
                    **slot = Err(error);
                }
            });
        }
        outcome.map_err(|(path, source)| io_error(&path, source))?;
        Ok(pending)
    }

    /// Renames the temp file into place. Called inside the write transaction (under the write
    /// lock); safe to call again if a retried attempt already did it. Same directory, which the
    /// temp file keeps non-empty, so a sweep can't take it.
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

/// The rounds of [`PendingFile::write_named`]. On failure, the path the error is about.
fn create_in(parent: &Path, tmp: &Path, data: &[u8]) -> std::result::Result<(), (PathBuf, std::io::Error)> {
    let mut made = false;
    let mut round = 0;
    let outcome = loop {
        round += 1;
        #[cfg(test)]
        hooks::count_round();
        match DirBuilder::new().mode(0o700).create(parent) {
            Ok(()) => made = true,
            Err(e) if e.kind() == ErrorKind::AlreadyExists => {}
            Err(e) => break Err((parent.to_path_buf(), e)),
        }
        // Only the create's error can mean a sweep took `aa`; once the file exists the directory
        // isn't empty, so a failed write is terminal (and never rewrites a large value).
        #[cfg(test)]
        let created = hooks::create(parent).unwrap_or_else(|| create_new(tmp));
        #[cfg(not(test))]
        let created = create_new(tmp);
        match created {
            Ok(file) => break write_into(file, tmp, data).map_err(|e| (tmp.to_path_buf(), e)),
            Err(e) if directory_vanished(&e) && round < CREATE_ROUNDS => {}
            Err(e) => break Err((tmp.to_path_buf(), e)),
        }
    };
    if outcome.is_err() && made {
        let _ = std::fs::remove_dir(parent);
    }
    outcome
}

/// Whether a create in a value directory failed because a sweep removed the directory under it.
/// `ENOENT` on Linux; macOS's APFS can also answer `EINVAL` when the directory goes during the
/// create's lookup (seen in `writers_racing_a_sweeper_in_one_directory_never_fail`). Retrying an
/// `EINVAL` with another cause costs at most [`CREATE_ROUNDS`] quick failures.
fn directory_vanished(error: &std::io::Error) -> bool {
    const EINVAL: i32 = 22; // Linux and macOS.
    error.kind() == ErrorKind::NotFound || error.raw_os_error() == Some(EINVAL)
}

/// Creates `path`, which must not exist. A failed create removes nothing, since whatever holds that
/// name isn't this call's.
fn create_new(path: &Path) -> std::io::Result<File> {
    FileOptions::new().write(true).create_new(true).open(path)
}

/// Writes `data` to the file just created at `path`. A write that fails partway removes the file:
/// the caller gets no [`PendingFile`] to discard, and no row or total will ever count it.
fn write_into(mut file: File, path: &Path, data: &[u8]) -> std::io::Result<()> {
    file.write_all(data).inspect_err(|_| {
        let _ = std::fs::remove_file(path);
    })
}

/// The path of a value file named `relative` in the database, or `None` if the name isn't one this
/// module writes: exactly `aa/<32 hex>.val`, lowercase, with `aa` the hex's first two characters.
/// `Path::join` follows absolute paths and `..`, so a damaged row must never reach it unchecked.
#[must_use]
pub fn value_path(directory: &Path, relative: &str) -> Option<PathBuf> {
    let (dir_name, file_name) = relative.split_once('/')?;
    let hex = file_name.strip_suffix(".val")?;
    (is_hex(dir_name, 2) && is_hex(hex, 32) && hex.starts_with(dir_name))
        .then(|| directory.join(dir_name).join(file_name))
}

/// Whether `s` is exactly `len` lowercase hex characters.
fn is_hex(s: &str, len: usize) -> bool {
    s.len() == len && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Removes the empty value directories in a store's `directory`, until `deadline`. Visits only
/// entries named exactly two lowercase hex characters, starting at the `start`-th (modulo their
/// count, in name order) and wrapping, so a sweep cut short leaves the rest for a later one with no
/// state kept. With a `min_age`, skips a directory modified less than that long ago (a future
/// mtime, or one it can't read, counts as young). In each directory old enough it first removes the
/// temp files a dead writer left (`<32 hex>.tmp` under its own directory's prefix, a regular file,
/// unwritten for [`TMP_MIN_AGE`]; never a `.val`, and never a young `.tmp`, which may be another
/// process's write in flight), then calls `rmdir`, which removes nothing but an empty directory,
/// never recursively (an old two-level `aa/bb` keeps `aa` non-empty). Never fails; an already-passed
/// `deadline` removes nothing.
///
/// Run by a store's last release and after `clear()`, with no gate level held and the interpreter
/// released (plan 17, 4.2). Not for a `FanoutCache` root: each shard is its own store.
pub fn sweep(directory: &Path, deadline: Instant, min_age: Duration, start: usize) {
    if Instant::now() >= deadline {
        return;
    }
    let Ok(entries) = std::fs::read_dir(directory) else { return };
    let mut names: Vec<OsString> = entries
        .filter_map(std::result::Result::ok)
        .map(|entry| entry.file_name())
        .filter(|name| name.to_str().is_some_and(|name| is_hex(name, 2)))
        .collect();
    if names.is_empty() {
        return;
    }
    names.sort_unstable();
    let first = start % names.len();
    let now = SystemTime::now();
    for name in names[first..].iter().chain(&names[..first]) {
        if Instant::now() >= deadline {
            return;
        }
        let path = directory.join(name);
        if !min_age.is_zero() && !old_enough(&path, now, min_age) {
            continue;
        }
        // After the age check, so a young directory costs one `lstat`, and a quiet one holding only
        // a dead temp file goes in this pass (the unlink refreshes its mtime, but the decision is made).
        remove_dead_temp_files(&path, now);
        let _ = std::fs::remove_dir(&path);
    }
}

/// Removes the `<32 hex>.tmp` regular files in `dir` that no writer has touched for [`TMP_MIN_AGE`]
/// (plan 19). Nothing else: not a `.val`, not a young `.tmp`, not a link or a directory of that
/// name, and a directory it can't read is left alone.
fn remove_dead_temp_files(dir: &Path, now: SystemTime) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    let Some(prefix) = dir.file_name().and_then(|name| name.to_str()) else { return };
    for entry in entries.filter_map(std::result::Result::ok) {
        let name = entry.file_name();
        let Some(stem) = name.to_str().and_then(|name| name.strip_suffix(".tmp")) else { continue };
        // The same rule as `value_path`: oxide only ever writes a file under its own prefix.
        if !is_hex(stem, 32) || !stem.starts_with(prefix) {
            continue;
        }
        let path = dir.join(&name);
        let dead = std::fs::symlink_metadata(&path)
            .ok()
            .filter(std::fs::Metadata::is_file)
            .and_then(|meta| meta.modified().ok())
            .and_then(|modified| now.duration_since(modified).ok())
            .is_some_and(|age| age >= TMP_MIN_AGE);
        if dead {
            let _ = std::fs::remove_file(&path);
        }
    }
}

/// Whether `path` is a directory (not a link to one) last modified at least `min_age` before `now`.
fn old_enough(path: &Path, now: SystemTime, min_age: Duration) -> bool {
    std::fs::symlink_metadata(path)
        .ok()
        .filter(std::fs::Metadata::is_dir)
        .and_then(|meta| meta.modified().ok())
        .and_then(|modified| now.duration_since(modified).ok())
        .is_some_and(|age| age >= min_age)
}

/// A random place for a sweep to start, so successive sweeps cut short cover the whole directory.
#[must_use]
pub fn sweep_start() -> usize {
    // Truncation is fine: any bits will do.
    #[allow(clippy::cast_possible_truncation)]
    let start = RandomState::new().build_hasher().finish() as usize;
    start
}

/// Reads a value file whose row says it holds `size` bytes. `Ok(None)` if it's missing or its
/// length doesn't match (treated alike: the value isn't readable from this file), or if `relative`
/// isn't a name this module writes ([`value_path`]).
pub fn read(directory: &Path, relative: &str, size: i64) -> Result<Option<Vec<u8>>> {
    let Some(path) = value_path(directory, relative) else { return Ok(None) };
    match File::open(&path) {
        Ok(file) => read_open(&file, &path, size),
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(None),
        Err(e) => Err(io_error(&path, e)),
    }
}

/// Opens a value file (for the locked fallback, which reads it after releasing the lock).
pub fn open(directory: &Path, relative: &str) -> Result<Option<(File, PathBuf)>> {
    let Some(path) = value_path(directory, relative) else { return Ok(None) };
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

/// Test-only injection points in [`PendingFile::write_named`]'s create loop, per thread (the write
/// runs on the caller's thread: core tests install no blocker).
#[cfg(test)]
pub(crate) mod hooks {
    use std::cell::Cell;
    use std::fs::File;
    use std::path::Path;

    thread_local! {
        static VANISH: Cell<usize> = const { Cell::new(0) };
        static FAIL: Cell<(usize, i32)> = const { Cell::new((0, 0)) };
        static ROUNDS: Cell<usize> = const { Cell::new(0) };
    }

    /// The next `n` rounds lose their directory between the `mkdir` and the create, as if a sweep
    /// won the race each time. Resets the round count.
    pub(crate) fn vanish_next(n: usize) {
        VANISH.set(n);
        ROUNDS.set(0);
    }

    /// The next `n` creates fail with OS error `errno` (after their `mkdir`), through the same
    /// classification as a real create's error. Resets the round count.
    pub(crate) fn fail_next_creates(n: usize, errno: i32) {
        FAIL.set((n, errno));
        ROUNDS.set(0);
    }

    /// Rounds started since the last arming.
    pub(crate) fn rounds() -> usize {
        ROUNDS.get()
    }

    pub(super) fn count_round() {
        ROUNDS.set(ROUNDS.get() + 1);
    }

    /// An injected result for this round's create, or `None` to make the real one.
    pub(super) fn create(parent: &Path) -> Option<std::io::Result<File>> {
        let (left, errno) = FAIL.get();
        if left > 0 {
            FAIL.set((left - 1, errno));
            return Some(Err(std::io::Error::from_raw_os_error(errno)));
        }
        let vanish = VANISH.get();
        if vanish > 0 {
            VANISH.set(vanish - 1);
            std::fs::remove_dir(parent).expect("the injected sweep removes an empty directory");
        }
        None
    }
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
        // Under Rosetta (`scripts/linux-check.sh --platform linux/amd64` on an arm64 Mac) the failed
        // allocation kills the process ("rosetta error: could not find free space") instead of
        // returning an error, so there's nothing to learn there. Real x86-64 runs it.
        if std::env::var_os("DCO_EMULATED").is_some() {
            eprintln!("skipped: DCO_EMULATED is set");
            return;
        }
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
    fn a_name_already_taken_is_an_error_and_keeps_that_file() {
        let tmp = TempDir::new("spill-collision");
        let path = tmp.path().join("taken.tmp");
        std::fs::write(&path, b"someone else's").unwrap();
        let error = create_new(&path).unwrap_err();
        assert_eq!(error.kind(), ErrorKind::AlreadyExists);
        assert_eq!(std::fs::read(&path).unwrap(), b"someone else's");
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

    /// The value directories (two lowercase hex characters) directly under `dir`, sorted.
    fn value_dirs(dir: &Path) -> Vec<String> {
        let mut names: Vec<String> = std::fs::read_dir(dir)
            .unwrap()
            .map(Result::unwrap)
            .filter(|e| e.file_type().unwrap().is_dir())
            .map(|e| e.file_name().into_string().unwrap())
            .filter(|n| is_hex(n, 2))
            .collect();
        names.sort();
        names
    }

    fn hex_with_prefix(prefix: &str, n: usize) -> String {
        format!("{prefix}{n:030x}")
    }

    #[test]
    fn files_live_one_level_deep() {
        let tmp = TempDir::new("spill-flat");
        let pending = PendingFile::write(tmp.path(), b"x").unwrap();
        let (dir, name) = pending.relative.split_once('/').unwrap();
        assert!(is_hex(dir, 2) && name.strip_suffix(".val").is_some_and(|hex| is_hex(hex, 32)), "{}", pending.relative);
        assert_eq!(pending.tmp.parent().unwrap(), tmp.path().join(dir));
        assert!(value_path(tmp.path(), &pending.relative).is_some());
    }

    #[test]
    fn a_write_retries_a_directory_that_vanishes_and_recreates_it_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let tmp = TempDir::new("spill-vanish");
        for n in [1, 63] {
            hooks::vanish_next(n);
            let pending = PendingFile::write(tmp.path(), b"x").unwrap();
            assert_eq!(hooks::rounds(), n + 1, "{n} lost rounds, then one that succeeds");
            let dir = pending.tmp.parent().unwrap();
            assert_eq!(std::fs::metadata(dir).unwrap().permissions().mode() & 0o777, 0o700);
            assert!(pending.tmp.exists());
            pending.discard();
        }
    }

    #[test]
    fn a_write_that_keeps_losing_its_directory_fails_and_leaves_none() {
        let tmp = TempDir::new("spill-vanish-64");
        hooks::vanish_next(CREATE_ROUNDS);
        let error = PendingFile::write_named(tmp.path(), &hex_with_prefix("ab", 1), b"x").unwrap_err();
        assert_eq!(hooks::rounds(), CREATE_ROUNDS);
        match &error {
            Error::Io { path, source } => {
                assert_eq!(source.kind(), ErrorKind::NotFound, "{error:?}");
                assert!(path.starts_with(tmp.path().join("ab")), "{error:?}");
            }
            other => panic!("expected an Io error, got {other:?}"),
        }
        assert_eq!(value_dirs(tmp.path()), Vec::<String>::new());
    }

    #[test]
    fn a_write_into_a_deleted_cache_directory_fails_at_once_and_creates_nothing() {
        let tmp = TempDir::new("spill-deleted-root");
        let root = tmp.path().join("cache");
        std::fs::create_dir(&root).unwrap();
        std::fs::remove_dir(&root).unwrap();
        hooks::vanish_next(0);
        let error = PendingFile::write(&root, b"x").unwrap_err();
        assert_eq!(hooks::rounds(), 1, "no retry when the root itself is gone");
        assert!(matches!(&error, Error::Io { source, .. } if source.kind() == ErrorKind::NotFound), "{error:?}");
        assert!(!root.exists(), "the root must not be re-created");
    }

    const EACCES: i32 = 13; // Linux and macOS.
    const EINVAL: i32 = 22;

    #[test]
    fn macos_einval_from_the_create_is_retried_like_not_found() {
        let tmp = TempDir::new("spill-einval");
        hooks::fail_next_creates(5, EINVAL);
        let pending = PendingFile::write(tmp.path(), b"x").unwrap();
        assert_eq!(hooks::rounds(), 6, "five EINVAL rounds, then one that succeeds");
        assert!(pending.tmp.exists());
    }

    #[test]
    fn a_failed_create_removes_the_directory_it_made() {
        let tmp = TempDir::new("spill-failed-create");
        hooks::fail_next_creates(1, EACCES);
        let error = PendingFile::write_named(tmp.path(), &hex_with_prefix("cd", 1), b"x").unwrap_err();
        assert_eq!(hooks::rounds(), 1, "a create error that isn't a vanished directory is terminal");
        assert!(matches!(&error, Error::Io { source, .. } if source.kind() == ErrorKind::PermissionDenied));
        assert_eq!(value_dirs(tmp.path()), Vec::<String>::new());
        // One it didn't make stays: another file lives there.
        let other = PendingFile::write_named(tmp.path(), &hex_with_prefix("cd", 2), b"x").unwrap();
        hooks::fail_next_creates(1, EACCES);
        PendingFile::write_named(tmp.path(), &hex_with_prefix("cd", 3), b"x").unwrap_err();
        assert!(other.tmp.exists());
        assert_eq!(value_dirs(tmp.path()), ["cd"]);
    }

    #[test]
    fn names_from_the_database_are_checked() {
        let dir = Path::new("/c");
        let good = format!("ab/{}.val", hex_with_prefix("ab", 7));
        assert_eq!(value_path(dir, &good), Some(dir.join(&good)));
        let bad = [
            format!("/tmp/{}.val", hex_with_prefix("ab", 7)),
            format!("../{}.val", hex_with_prefix("ab", 7)),
            format!("ab/../{}.val", hex_with_prefix("ab", 7)),
            format!("AB/{}.val", hex_with_prefix("AB", 7)),
            format!("ab/{}.val", hex_with_prefix("AB", 7)),
            format!("ab/{}.val", hex_with_prefix("cd", 7)),
            format!("ab/{}.tmp", hex_with_prefix("ab", 7)),
            format!("ab/{}0.val", hex_with_prefix("ab", 7)),
            format!("a/{}.val", hex_with_prefix("ab", 7)),
            format!("ab/cd/{}.val", hex_with_prefix("ab", 7)),
            "ab/x.val".to_string(),
            String::new(),
        ];
        for name in &bad {
            assert_eq!(value_path(dir, name), None, "{name:?}");
        }
        let tmp = TempDir::new("spill-bad-name");
        let outside = tmp.path().join("outside.val");
        std::fs::write(&outside, b"x").unwrap();
        let absolute = outside.to_str().unwrap();
        assert_eq!(read(tmp.path(), absolute, 1).unwrap(), None);
        assert!(open(tmp.path(), absolute).unwrap().is_none());
    }

    fn far() -> Instant {
        Instant::now() + Duration::from_secs(60)
    }

    #[test]
    fn a_sweep_removes_only_empty_value_directories() {
        let tmp = TempDir::new("spill-sweep");
        let root = tmp.path();
        let kept_val = PendingFile::write_named(root, &hex_with_prefix("01", 1), b"x").unwrap();
        kept_val.commit_name().unwrap();
        let kept_tmp = PendingFile::write_named(root, &hex_with_prefix("02", 1), b"x").unwrap();
        for empty in ["03", "fe"] {
            std::fs::create_dir(root.join(empty)).unwrap();
        }
        std::fs::write(root.join("ab"), b"a file").unwrap();
        for other in ["000", "notes", "CD", "a", "abc"] {
            std::fs::create_dir(root.join(other)).unwrap();
        }
        std::fs::create_dir_all(root.join("0c/0d")).unwrap(); // An old two-level tree, `bb` empty.
        std::fs::write(root.join("diskcache-oxide.sqlite"), b"").unwrap();
        sweep(root, far(), Duration::ZERO, 0);
        assert_eq!(value_dirs(root), ["01", "02", "0c"]);
        assert!(root.join("0c/0d").is_dir(), "a sweep never recurses");
        assert!(root.join("ab").is_file() && root.join("diskcache-oxide.sqlite").is_file());
        for other in ["000", "notes", "CD", "a", "abc"] {
            assert!(root.join(other).is_dir(), "{other}");
        }
        assert!(kept_tmp.tmp.exists() && kept_val.val.exists());
    }

    #[test]
    fn a_sweep_wraps_from_its_start_and_stops_at_its_deadline() {
        let tmp = TempDir::new("spill-sweep-wrap");
        let make = || {
            for i in 0..10u8 {
                std::fs::create_dir(tmp.path().join(format!("{i:02x}"))).unwrap();
            }
        };
        make();
        sweep(tmp.path(), Instant::now(), Duration::ZERO, 0);
        assert_eq!(value_dirs(tmp.path()).len(), 10, "an already-passed deadline removes nothing");
        sweep(tmp.path(), far(), Duration::ZERO, 7);
        assert_eq!(value_dirs(tmp.path()), Vec::<String>::new(), "starting at the 7th, it wraps and covers all");
        make();
        sweep(tmp.path(), far(), Duration::ZERO, usize::MAX);
        assert_eq!(value_dirs(tmp.path()), Vec::<String>::new(), "any start is taken modulo the count");
    }

    #[test]
    fn a_sweep_with_a_min_age_skips_young_directories() {
        let tmp = TempDir::new("spill-sweep-age");
        for name in ["10", "11", "12"] {
            std::fs::create_dir(tmp.path().join(name)).unwrap();
        }
        let set_mtime = |name: &str, at: SystemTime| {
            File::open(tmp.path().join(name)).unwrap().set_modified(at).unwrap();
        };
        set_mtime("10", SystemTime::now() - Duration::from_secs(120));
        set_mtime("12", SystemTime::now() + Duration::from_secs(3_600));
        sweep(tmp.path(), far(), SWEEP_MIN_AGE, 0);
        assert_eq!(value_dirs(tmp.path()), ["11", "12"], "fresh and future mtimes count as young");
        sweep(tmp.path(), far(), Duration::ZERO, 0);
        assert_eq!(value_dirs(tmp.path()), Vec::<String>::new());
    }

    #[test]
    fn writers_racing_a_sweeper_in_one_directory_never_fail_except_the_macos_rmdir_race() {
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
        type Failure = (String, Error);
        let tmp = TempDir::new("spill-sweep-race");
        let root = tmp.path();
        let stop = AtomicBool::new(false);
        let next = AtomicUsize::new(0);
        let failures: Vec<Failure> = std::thread::scope(|s| {
            s.spawn(|| {
                while !stop.load(Ordering::Relaxed) {
                    sweep(root, far(), Duration::ZERO, 0);
                }
            });
            let writers: Vec<_> = (0..8)
                .map(|_| {
                    s.spawn(|| -> std::result::Result<(), Failure> {
                        for _ in 0..300 {
                            let n = next.fetch_add(1, Ordering::Relaxed);
                            let pending = PendingFile::write_named(root, &hex_with_prefix("ee", n), b"x")
                                .map_err(|e| (format!("write {n}"), e))?;
                            pending.commit_name().map_err(|e| (format!("commit {n}"), e))?;
                            pending.discard();
                        }
                        Ok(())
                    })
                })
                .collect();
            let failures = writers.into_iter().filter_map(|w| w.join().unwrap().err()).collect();
            stop.store(true, Ordering::Relaxed);
            failures
        });
        // On macOS, APFS lets `rmdir` succeed on a directory while a create inside it succeeds too
        // (plan 19's probe: 2 orphaned files in 54,353 creates racing a tight rmdir loop, with no
        // oxide code involved), so a write there can lose its file after the create: the data
        // write or the rename then fails with ENOENT or EINVAL. No retry in user space can prevent
        // that, so this test tolerates exactly that shape on macOS, a few times in 2,400 writes
        // (about 2% of runs see one). Linux serializes the two, and stays strict.
        let kernel_race = |(_, error): &Failure| {
            cfg!(target_os = "macos") && matches!(error, Error::Io { source, .. } if directory_vanished(source))
        };
        let (tolerated, real): (Vec<Failure>, Vec<Failure>) = failures.into_iter().partition(kernel_race);
        assert!(real.is_empty(), "{real:?}");
        assert!(tolerated.len() <= 3, "more than a few files lost to the macOS rmdir race: {tolerated:?}");
        sweep(root, far(), Duration::ZERO, 0);
        assert_eq!(value_dirs(root), Vec::<String>::new());
    }

    fn backdate(path: &Path, by: Duration) {
        File::open(path).unwrap().set_modified(SystemTime::now() - by).unwrap();
    }

    fn names_in(dir: &Path) -> Vec<String> {
        let mut names: Vec<String> =
            std::fs::read_dir(dir).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
        names.sort();
        names
    }

    #[test]
    fn a_sweep_removes_only_the_temp_files_a_dead_writer_left() {
        let tmp = TempDir::new("spill-sweep-tmp");
        let root = tmp.path();
        let dir = root.join("ee");
        std::fs::create_dir(&dir).unwrap();
        let two_hours = Duration::from_secs(7_200);
        let old_tmp = dir.join(format!("{}.tmp", hex_with_prefix("ee", 1)));
        let young_tmp = dir.join(format!("{}.tmp", hex_with_prefix("ee", 2)));
        let old_val = dir.join(format!("{}.val", hex_with_prefix("ee", 3)));
        let misnamed = dir.join("not-a-value-name.tmp");
        let tmp_dir = dir.join(format!("{}.tmp", hex_with_prefix("ee", 4)));
        for path in [&old_tmp, &young_tmp, &old_val, &misnamed] {
            std::fs::write(path, b"x").unwrap();
        }
        std::fs::create_dir(&tmp_dir).unwrap();
        for path in [&old_tmp, &old_val, &misnamed, &tmp_dir] {
            backdate(path, two_hours);
        }
        backdate(&dir, two_hours);
        sweep(root, far(), SWEEP_MIN_AGE, 0);
        let mut expected: Vec<String> = [&young_tmp, &old_val, &misnamed, &tmp_dir]
            .map(|p| p.file_name().unwrap().to_str().unwrap().to_owned())
            .into();
        expected.sort();
        assert_eq!(names_in(&dir), expected, "only the old, well-named, regular .tmp went");
        // A dead .tmp under another directory's prefix isn't oxide's: left alone.
        let foreign = dir.join(format!("{}.tmp", hex_with_prefix("ef", 9)));
        std::fs::write(&foreign, b"x").unwrap();
        backdate(&foreign, two_hours);
        backdate(&dir, two_hours);
        sweep(root, far(), SWEEP_MIN_AGE, 0);
        assert!(foreign.exists());
        // Two hours old and alone in a quiet directory: file and directory go in one pass, since the
        // age is decided before the unlink refreshes the directory's mtime. A young directory isn't
        // read at all, so its dead .tmp waits for it to go quiet.
        let lonely = root.join("f0");
        std::fs::create_dir(&lonely).unwrap();
        let leftover = lonely.join(format!("{}.tmp", hex_with_prefix("f0", 1)));
        std::fs::write(&leftover, b"x").unwrap();
        backdate(&leftover, two_hours);
        sweep(root, far(), SWEEP_MIN_AGE, 0);
        assert!(leftover.exists(), "a young directory isn't read");
        backdate(&lonely, two_hours);
        sweep(root, far(), SWEEP_MIN_AGE, 0);
        assert!(!lonely.exists(), "a quiet directory loses its dead .tmp and goes, in one pass");
    }
}
