//! Removing the caches an older format left in a cache directory (plan 26, `remove_old_formats()`).
//!
//! After a format change a directory `D` can hold the old cache at its root (beside the versioned
//! subdirectory now in use, see [`crate::layout`]) and older `diskcache-oxide-format-M`
//! subdirectories. [`remove_old_formats`] removes those, and only those:
//!
//! - Decided by content, never by name: a cache is removed only if its database (or its manifest, or
//!   its shard 0 database) is a format older than this build's. A newer format (a rolled-back build
//!   must not wipe the newer release's cache), this build's format, and anything it can't read are
//!   left, and reported.
//! - Only names diskcache-oxide writes: database files, value files that match the value-file rule,
//!   two-character hex value directories, a `FanoutCache`'s manifest and its temp files, its shard
//!   directories, and versioned subdirectories. Anything else stays, and a directory is removed only
//!   once empty (`rmdir`), never recursively. Links are never followed.
//! - No check for other processes (Michael's decision): calling it means "remove them now". A
//!   database a store in this process has open is left, though.
//! - An empty (zero-length) database file holds nothing, but it's also what a creator holds between
//!   SQLite's open and its first write. So it's removed only at the root and only once it's
//!   [`EMPTY_GRACE`] old (what an old worker leaves after reopening a removed cache by path); in a
//!   versioned subdirectory or as a shard it's left (review, high 1). A non-empty one with no format
//!   stamp may be a creator part-way through, so it's left too.
//!
//! Order: a cache's value files go first, then its database files back to back (database, WAL,
//! shared memory, journal), so an old process opening by path in between can't pair a fresh database
//! with the old WAL. A `FanoutCache`'s shards go highest first, shard 0 last, then the manifest, so a
//! cleanup cut short (a crash, a file held open on Windows) still leaves what detection reads, and
//! the next call finishes it.

use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use crate::fanout::{MANIFEST_FILENAME, MANIFEST_FORMAT, Manifest, manifest_path, read_manifest, shard_dirname};
use crate::layout::{format_of_dir_name, read_format};
use crate::open::db_path;
use crate::schema::{DB_FILENAME, FORMAT_VERSION};
use crate::spill::{is_hex, is_value_file_name};
use crate::store::Store;
use crate::{Error, Result};

/// How old an empty database file at the root must be before it's removed: well past any creator's
/// open-to-first-write window, which is bounded by its `timeout`.
pub const EMPTY_GRACE: Duration = Duration::from_secs(600);

/// What [`remove_old_formats`] did.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Cleanup {
    /// The database files, manifests and directories removed, in the order removed. Value files
    /// inside a removed value directory aren't listed one by one.
    pub removed: Vec<PathBuf>,
    /// What was left, and why: an entry diskcache-oxide didn't write, a cache it can't tell is
    /// older, one this process has open, or a file the system wouldn't remove (on Windows, one
    /// another process has open).
    pub left: Vec<(PathBuf, String)>,
}

impl Cleanup {
    fn removed(&mut self, path: &Path) {
        self.removed.push(path.to_path_buf());
    }

    fn left(&mut self, path: &Path, reason: impl Into<String>) {
        self.left.push((path.to_path_buf(), reason.into()));
    }
}

/// Removes every cache in an older format than this build's from `root`: the root's own cache and
/// any `diskcache-oxide-format-M` subdirectory holding one. See the module docs for what is and
/// isn't removed. Fails only if `root` can't be listed; a root that doesn't exist has nothing to
/// remove. Format reads use `timeout` and enter the fork gate until `deadline`.
pub fn remove_old_formats(root: &Path, timeout: Duration, deadline: Instant) -> Result<Cleanup> {
    let mut cleanup = Cleanup::default();
    let entries = match std::fs::read_dir(root) {
        Ok(entries) => entries,
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(cleanup),
        Err(source) => return Err(Error::Io { path: root.to_path_buf(), source }),
    };
    let mut subdirectories: Vec<PathBuf> = entries
        .filter_map(std::result::Result::ok)
        .filter(|entry| entry.file_name().to_str().and_then(format_of_dir_name).is_some())
        .map(|entry| entry.path())
        .collect();
    subdirectories.sort();

    // The root's own cache, left in place as a directory: it's the caller's.
    if let Some(cache) = detect(root, true, timeout, deadline, &mut cleanup) {
        remove_cache(root, &cache, timeout, deadline, &mut cleanup);
    }
    for directory in subdirectories {
        if !is_real_dir(&directory) {
            cleanup.left(&directory, "a link or not a directory, never followed");
            continue;
        }
        let Some(cache) = detect(&directory, false, timeout, deadline, &mut cleanup) else { continue };
        if remove_cache(&directory, &cache, timeout, deadline, &mut cleanup) {
            remove_empty_dir(&directory, &mut cleanup);
        }
    }
    Ok(cleanup)
}

/// A cache found in a directory, with an older format.
enum OldCache {
    Plain,
    Fanout(Option<Manifest>),
}

/// What older-format cache `directory` holds, if any. A cache in this build's format or a newer one
/// isn't old; one it can't read is reported as left. `at_root` is whether `directory` is the
/// handle's own directory (see [`older`]).
fn detect(
    directory: &Path,
    at_root: bool,
    timeout: Duration,
    deadline: Instant,
    cleanup: &mut Cleanup,
) -> Option<OldCache> {
    let manifest = manifest_path(directory);
    if is_real_file(&manifest) {
        let (format, parsed) = match read_manifest(directory) {
            Ok(parsed) => (MANIFEST_FORMAT, parsed),
            Err(Error::FormatMismatch { found, .. }) => (found, None),
            Err(error) => {
                cleanup.left(&manifest, format!("can't tell its format: {error}"));
                return None;
            }
        };
        if format > MANIFEST_FORMAT {
            return None;
        }
        if format < MANIFEST_FORMAT {
            return Some(OldCache::Fanout(parsed));
        }
        // The manifest is current, so shard 0's database decides.
        return match older(&directory.join(shard_dirname(0)), false, timeout, deadline, cleanup) {
            Some(true) => Some(OldCache::Fanout(parsed)),
            _ => None,
        };
    }
    match older(directory, at_root, timeout, deadline, cleanup) {
        Some(true) => Some(OldCache::Plain),
        _ => None,
    }
}

/// Whether `directory`'s database is an older format: `None` if it has none, or its format can't be
/// told (then reported as left).
fn older(directory: &Path, at_root: bool, timeout: Duration, deadline: Instant, cleanup: &mut Cleanup) -> Option<bool> {
    let db = db_path(directory);
    let Ok(meta) = std::fs::symlink_metadata(&db) else { return None };
    if !meta.is_file() {
        return None;
    }
    // An empty file: nothing was ever written to it. It's what an old worker leaves when it reopens
    // a removed cache by path and fails before writing (a 0.1.2 handle reused after
    // `remove_old_formats()`), but also what any creator holds between SQLite's open and its first
    // write, a newer build's included. So only at the root, and only once it's stale (review, high 1).
    if meta.len() == 0 {
        let stale = meta.modified().ok().and_then(|m| m.elapsed().ok()).is_some_and(|age| age > EMPTY_GRACE);
        if at_root && stale {
            return Some(true);
        }
        cleanup.left(&db, "an empty database, possibly being created");
        return None;
    }
    match read_format(directory, timeout, deadline) {
        Ok(Some(0)) => {
            cleanup.left(&db, "no format stamp yet (being created, or not diskcache-oxide's)");
            None
        }
        Ok(Some(version)) => Some(version < FORMAT_VERSION),
        Ok(None) => None,
        Err(error) => {
            cleanup.left(&db, format!("can't tell its format: {error}"));
            None
        }
    }
}

/// Removes the old cache in `directory`, but not `directory` itself. Returns whether every file of
/// it went (so a subdirectory holding it can then be removed).
fn remove_cache(
    directory: &Path,
    cache: &OldCache,
    timeout: Duration,
    deadline: Instant,
    cleanup: &mut Cleanup,
) -> bool {
    match cache {
        OldCache::Plain => remove_plain(directory, deadline, cleanup),
        OldCache::Fanout(manifest) => remove_fanout(directory, *manifest, timeout, deadline, cleanup),
    }
}

/// A `FanoutCache` root: its shards highest first, then the manifest's temp files, then the manifest.
fn remove_fanout(
    root: &Path,
    manifest: Option<Manifest>,
    timeout: Duration,
    deadline: Instant,
    cleanup: &mut Cleanup,
) -> bool {
    // The count from the manifest when this build can read it; otherwise every shard-named entry.
    let mut shards: Vec<(usize, PathBuf)> = match manifest {
        Some(manifest) => (0..manifest.shards() as usize).map(|i| (i, root.join(shard_dirname(i)))).collect(),
        None => std::fs::read_dir(root)
            .map(|entries| {
                entries
                    .filter_map(std::result::Result::ok)
                    .filter_map(|e| {
                        let name = e.file_name().into_string().ok()?;
                        let digits = name.len() == 3 && name.bytes().all(|b| b.is_ascii_digit());
                        let index = name.parse::<usize>().ok().filter(|_| digits)?;
                        Some((index, e.path()))
                    })
                    .collect()
            })
            .unwrap_or_default(),
    };
    shards.sort_by_key(|shard| std::cmp::Reverse(shard.0));
    let mut complete = true;
    for (index, shard) in &shards {
        match std::fs::symlink_metadata(shard) {
            Err(e) if e.kind() == ErrorKind::NotFound => {}
            Ok(meta) if meta.is_dir() => {
                // A shard in this build's format or a newer one is never removed, whatever shard 0
                // says: two releases racing to create one root can each win some shards (review, high 1).
                if let Ok(Some(version)) = read_format(shard, timeout, deadline)
                    && version >= FORMAT_VERSION
                {
                    cleanup.left(&db_path(shard), format!("format {version}, not older: kept"));
                    complete = false;
                    continue;
                }
                // Shard 0's database is what tells a later call this root is old, so shard 0 goes
                // whole or not at all: never its database alone, with something foreign left.
                if *index == 0
                    && let Some(foreign) = foreign_entry(shard)
                {
                    cleanup.left(&foreign, "not written by diskcache-oxide, so shard 0 is kept whole");
                    complete = false;
                    continue;
                }
                if remove_plain(shard, deadline, cleanup) {
                    complete &= remove_empty_dir(shard, cleanup);
                } else {
                    complete = false;
                }
            }
            _ => {
                cleanup.left(shard, "a link or not a directory, never followed");
                complete = false;
            }
        }
    }
    if !complete {
        cleanup.left(&manifest_path(root), "kept until every shard is gone, so a later call can finish");
        return false;
    }
    if let Ok(entries) = std::fs::read_dir(root) {
        let prefix = format!("{MANIFEST_FILENAME}.tmp-");
        for entry in entries.filter_map(std::result::Result::ok) {
            if entry.file_name().to_str().is_some_and(|n| n.starts_with(&prefix)) {
                complete &= remove_file(&entry.path(), cleanup, false);
            }
        }
    }
    complete & remove_file(&manifest_path(root), cleanup, true)
}

/// A plain cache's files in `directory`: value files, then the database files back to back. Left
/// whole if a store in this process has the database open. Returns whether everything went.
fn remove_plain(directory: &Path, deadline: Instant, cleanup: &mut Cleanup) -> bool {
    let db = db_path(directory);
    match Store::is_open_in_process(&db, deadline) {
        Ok(false) => {}
        Ok(true) => {
            cleanup.left(&db, "this process has it open");
            return false;
        }
        Err(error) => {
            cleanup.left(&db, format!("couldn't check whether this process has it open: {error}"));
            return false;
        }
    }
    let mut complete = true;
    let Ok(entries) = std::fs::read_dir(directory) else {
        cleanup.left(directory, "can't be listed");
        return false;
    };
    let mut value_dirs: Vec<PathBuf> = Vec::new();
    for entry in entries.filter_map(std::result::Result::ok) {
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        if is_hex(name, 2) {
            value_dirs.push(entry.path());
        }
    }
    value_dirs.sort();
    for dir in &value_dirs {
        complete &= remove_value_dir(dir, cleanup);
    }
    // Back to back, nothing in between (plan 26, M1).
    for suffix in ["", "-wal", "-shm", "-journal"] {
        complete &= remove_file(&directory.join(format!("{DB_FILENAME}{suffix}")), cleanup, suffix.is_empty());
    }
    complete
}

/// One value directory: its value and temp files (names under the value-file rule only), then the
/// directory if that left it empty.
fn remove_value_dir(dir: &Path, cleanup: &mut Cleanup) -> bool {
    if !is_real_dir(dir) {
        cleanup.left(dir, "a link or not a directory, never followed");
        return false;
    }
    let Some(prefix) = dir.file_name().and_then(|n| n.to_str()).map(str::to_owned) else { return false };
    let Ok(entries) = std::fs::read_dir(dir) else {
        cleanup.left(dir, "can't be listed");
        return false;
    };
    let mut complete = true;
    for entry in entries.filter_map(std::result::Result::ok) {
        let path = entry.path();
        let ours = entry.file_name().to_str().is_some_and(|name| is_value_file_name(name, &prefix));
        if ours && is_real_file(&path) {
            complete &= remove_file(&path, cleanup, false);
        } else {
            cleanup.left(&path, "not a value file diskcache-oxide wrote");
            complete = false;
        }
    }
    complete & remove_empty_dir(dir, cleanup)
}

/// An entry in a plain cache's directory that diskcache-oxide didn't write, if there's one: anything
/// but its database files and value directories holding only value files.
fn foreign_entry(directory: &Path) -> Option<PathBuf> {
    let entries = std::fs::read_dir(directory).ok()?;
    for entry in entries.filter_map(std::result::Result::ok) {
        let path = entry.path();
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else { return Some(path) };
        let database = ["", "-wal", "-shm", "-journal"].iter().any(|s| name == format!("{DB_FILENAME}{s}"));
        if database && is_real_file(&path) {
            continue;
        }
        if is_hex(&name, 2) && is_real_dir(&path) {
            let Ok(values) = std::fs::read_dir(&path) else { return Some(path) };
            for value in values.filter_map(std::result::Result::ok) {
                let ours = value.file_name().to_str().is_some_and(|n| is_value_file_name(n, &name));
                if !ours || !is_real_file(&value.path()) {
                    return Some(value.path());
                }
            }
            continue;
        }
        return Some(path);
    }
    None
}

/// Removes a regular file (never a link's target): absent is fine. `listed` says whether it goes in
/// `removed` (database files and manifests do; value files don't).
fn remove_file(path: &Path, cleanup: &mut Cleanup, listed: bool) -> bool {
    match std::fs::symlink_metadata(path) {
        Err(e) if e.kind() == ErrorKind::NotFound => return true,
        Ok(meta) if !meta.is_file() => {
            cleanup.left(path, "a link or not a regular file, never followed");
            return false;
        }
        _ => {}
    }
    match std::fs::remove_file(path) {
        Ok(()) => {
            if listed {
                cleanup.removed(path);
            }
            true
        }
        Err(e) if e.kind() == ErrorKind::NotFound => true,
        Err(e) => {
            cleanup.left(path, format!("couldn't be removed (on Windows, another process may have it open): {e}"));
            false
        }
    }
}

/// `rmdir`: removes `dir` only if it's empty.
fn remove_empty_dir(dir: &Path, cleanup: &mut Cleanup) -> bool {
    match std::fs::remove_dir(dir) {
        Ok(()) => {
            cleanup.removed(dir);
            true
        }
        Err(e) if e.kind() == ErrorKind::NotFound => true,
        Err(e) => {
            cleanup.left(dir, format!("kept: not empty, or couldn't be removed: {e}"));
            false
        }
    }
}

fn is_real_dir(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok_and(|meta| meta.is_dir())
}

fn is_real_file(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok_and(|meta| meta.is_file())
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;
    use crate::fanout::{ShardRequest, open_manifest};
    use crate::layout::format_dir;
    use crate::open::{Requested, Role};
    use crate::store::{DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, OpenOptions};
    use crate::testutil::TempDir;

    const T: Duration = Duration::from_secs(5);

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

    fn clean(root: &Path) -> Cleanup {
        remove_old_formats(root, T, Instant::now() + T).unwrap()
    }

    fn stamp(dir: &Path, version: i64) {
        let conn = crate::open::connect(dir, T).unwrap();
        conn.execute_batch(&format!("PRAGMA user_version = {version}; PRAGMA wal_checkpoint(TRUNCATE);")).unwrap();
    }

    /// A plain cache in `dir` stamped `version`, with one value file in a value directory.
    fn plain(dir: &Path, version: i64) {
        Store::open(&options(dir)).unwrap().release_handle();
        stamp(dir, version);
        std::fs::create_dir(dir.join("ab")).unwrap();
        std::fs::write(dir.join("ab").join(format!("ab{}.val", "0".repeat(30))), b"value").unwrap();
    }

    fn fanout(root: &Path, shards: u32, version: i64) {
        let manifest = Manifest::new(shards).unwrap();
        open_manifest(root, ShardRequest::Exactly(manifest)).unwrap();
        let stores = Store::open_shards(&options(root), manifest, Instant::now() + T).unwrap();
        Store::close_all(&stores.iter().collect::<Vec<&Arc<Store>>>(), None);
        for i in 0..shards as usize {
            stamp(&root.join(shard_dirname(i)), version);
        }
    }

    fn names(dir: &Path) -> Vec<String> {
        let mut names: Vec<String> =
            std::fs::read_dir(dir).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
        names.sort();
        names
    }

    #[test]
    fn an_old_root_cache_goes_and_everything_else_stays() {
        let tmp = TempDir::new("cleanup-root");
        let root = tmp.path();
        plain(root, 2);
        plain(&format_dir(root), FORMAT_VERSION);
        std::fs::write(root.join("notes.txt"), b"mine").unwrap();
        let cleanup = clean(root);
        assert_eq!(names(root), ["diskcache-oxide-format-3", "notes.txt"], "{cleanup:?}");
        assert!(cleanup.removed.contains(&db_path(root)), "{cleanup:?}");
        assert!(cleanup.removed.contains(&root.join("ab")), "{cleanup:?}");
        assert!(db_path(&format_dir(root)).exists(), "the current format is never removed");
        assert_eq!(clean(root), Cleanup::default(), "a second call has nothing to do");
    }

    #[test]
    fn subdirectories_are_judged_by_content_not_name() {
        let tmp = TempDir::new("cleanup-content");
        let root = tmp.path();
        // Named for format 2 but holding this build's format: kept.
        let misnamed = root.join("diskcache-oxide-format-2");
        plain(&misnamed, FORMAT_VERSION);
        // Named for format 1 and holding format 1: removed, directory and all.
        let old = root.join("diskcache-oxide-format-1");
        plain(&old, 1);
        // A newer format: kept, so a rolled-back build never wipes it.
        let newer = root.join("diskcache-oxide-format-9");
        plain(&newer, 9);
        let cleanup = clean(root);
        assert!(db_path(&misnamed).exists());
        assert!(!old.exists(), "{cleanup:?}");
        assert!(cleanup.removed.contains(&old));
        assert!(db_path(&newer).exists());
    }

    #[test]
    fn a_current_or_newer_or_unstamped_root_is_left() {
        for (name, version) in [("current", FORMAT_VERSION), ("newer", FORMAT_VERSION + 1)] {
            let tmp = TempDir::new(&format!("cleanup-{name}"));
            plain(tmp.path(), version);
            let cleanup = clean(tmp.path());
            assert!(cleanup.removed.is_empty(), "{name}: {cleanup:?}");
            assert!(db_path(tmp.path()).exists());
        }
        let tmp = TempDir::new("cleanup-unstamped");
        plain(tmp.path(), 0);
        let cleanup = clean(tmp.path());
        assert_eq!(cleanup.removed, Vec::<PathBuf>::new());
        assert!(cleanup.left.iter().any(|(p, _)| *p == db_path(tmp.path())), "{cleanup:?}");
    }

    /// An empty database file, last written `age` ago.
    fn empty_database(dir: &Path, age: Duration) {
        std::fs::create_dir_all(dir).unwrap();
        let file = std::fs::File::create(db_path(dir)).unwrap();
        file.set_modified(std::time::SystemTime::now() - age).unwrap();
    }

    #[test]
    fn a_stale_empty_database_at_the_root_is_removed() {
        // What a 0.1.2 handle reused after a cleanup leaves at the root: nothing in it to keep.
        let tmp = TempDir::new("cleanup-empty-stale");
        std::fs::create_dir(format_dir(tmp.path())).unwrap();
        empty_database(tmp.path(), EMPTY_GRACE + Duration::from_secs(60));
        let cleanup = clean(tmp.path());
        assert_eq!(cleanup.removed, [db_path(tmp.path())], "{cleanup:?}");
        assert_eq!(names(tmp.path()), ["diskcache-oxide-format-3"]);
    }

    #[test]
    fn an_empty_database_that_may_be_being_created_is_kept() {
        // A fresh one at the root: a creator may be between SQLite's open and its first write.
        let tmp = TempDir::new("cleanup-empty-fresh");
        empty_database(tmp.path(), Duration::ZERO);
        assert_eq!(clean(tmp.path()).removed, Vec::<PathBuf>::new());
        assert!(db_path(tmp.path()).exists());

        // In a newer subdirectory, however old: a rolled-back build never touches it.
        let tmp = TempDir::new("cleanup-empty-newer");
        let newer = tmp.path().join("diskcache-oxide-format-9");
        empty_database(&newer, EMPTY_GRACE * 2);
        assert_eq!(clean(tmp.path()).removed, Vec::<PathBuf>::new());
        assert!(db_path(&newer).exists());

        // As shard 0 of a current FanoutCache: nothing says that root is old.
        let tmp = TempDir::new("cleanup-empty-shard");
        fanout(tmp.path(), 2, FORMAT_VERSION);
        let shard0 = tmp.path().join(shard_dirname(0));
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{suffix}", db_path(&shard0).display()));
        }
        empty_database(&shard0, EMPTY_GRACE * 2);
        assert_eq!(clean(tmp.path()).removed, Vec::<PathBuf>::new());
        assert!(db_path(&tmp.path().join(shard_dirname(1))).exists());
    }

    #[test]
    fn a_shard_in_the_current_format_is_kept_even_in_an_old_root() {
        // Two releases racing to create one root can each win some shards.
        let tmp = TempDir::new("cleanup-mixed-shards");
        fanout(tmp.path(), 2, 2);
        stamp(&tmp.path().join(shard_dirname(1)), FORMAT_VERSION);
        let cleanup = clean(tmp.path());
        assert!(db_path(&tmp.path().join(shard_dirname(1))).exists(), "{cleanup:?}");
        assert!(manifest_path(tmp.path()).exists(), "the manifest stays while a shard does");
    }

    #[test]
    fn a_database_this_process_has_open_is_left() {
        let tmp = TempDir::new("cleanup-open");
        plain(tmp.path(), FORMAT_VERSION);
        let store = Store::open(&options(tmp.path())).unwrap();
        // Stamp it old behind the store's back: still never removed while this process has it.
        stamp(tmp.path(), 2);
        // A store only ever has this build's format open, so it isn't even a candidate.
        let cleanup = clean(tmp.path());
        assert!(db_path(tmp.path()).exists());
        assert_eq!(cleanup.removed, Vec::<PathBuf>::new(), "{cleanup:?}");
        store.release_handle();
    }

    #[cfg(unix)]
    #[test]
    fn links_are_never_followed() {
        let tmp = TempDir::new("cleanup-links");
        let root = tmp.path().join("root");
        std::fs::create_dir(&root).unwrap();
        Store::open(&options(&root)).unwrap().release_handle();
        stamp(&root, 2);
        let elsewhere = tmp.path().join("elsewhere");
        std::fs::create_dir(&elsewhere).unwrap();
        let precious = elsewhere.join(format!("cd{}.val", "1".repeat(30)));
        std::fs::write(&precious, b"keep").unwrap();
        std::os::unix::fs::symlink(&elsewhere, root.join("cd")).unwrap();
        let other = tmp.path().join("other-cache");
        plain(&other, 1);
        std::os::unix::fs::symlink(&other, root.join("diskcache-oxide-format-1")).unwrap();
        let cleanup = clean(&root);
        assert!(precious.exists(), "a linked value directory's contents stay");
        assert!(db_path(&other).exists(), "a linked subdirectory's cache stays");
        assert!(!db_path(&root).exists(), "the root's own old database still goes: {cleanup:?}");
    }

    #[test]
    fn an_old_fanout_root_goes_shard_zero_last_then_the_manifest() {
        let tmp = TempDir::new("cleanup-fanout");
        let root = tmp.path();
        fanout(root, 3, 2);
        let cleanup = clean(root);
        assert!(names(root).is_empty(), "{cleanup:?}");
        let order: Vec<&PathBuf> = cleanup.removed.iter().filter(|p| p.parent() == Some(root)).collect();
        assert_eq!(
            order,
            [&root.join("002"), &root.join("001"), &root.join("000"), &manifest_path(root)],
            "{cleanup:?}"
        );
    }

    #[test]
    fn a_fanout_cleanup_cut_short_is_finished_by_the_next_call() {
        let tmp = TempDir::new("cleanup-fanout-partial");
        let root = tmp.path();
        fanout(root, 2, 2);
        let stray = root.join("000").join("stray.txt");
        std::fs::write(&stray, b"not ours").unwrap();
        let first = clean(root);
        assert!(stray.exists() && manifest_path(root).exists(), "{first:?}");
        assert!(!root.join("001").exists(), "the higher shard went first");
        std::fs::remove_file(&stray).unwrap();
        clean(root);
        assert_eq!(names(root), Vec::<String>::new());
    }
}
