//! Where a cache directory's cache lives: the directory itself, or a versioned subdirectory when the
//! directory holds a cache in another format (plan 26).
//!
//! A cache opened at `D` by a build of format N uses `D` unless `D` holds a cache in another format,
//! in which case it uses `D/diskcache-oxide-format-N` (`S`) and the caller logs a warning. Once `S`
//! exists every later open uses it, whatever the root holds, so two workers of one format never end
//! up in two caches. Nothing redirects twice: a directory named like `S` is opened as given.
//!
//! The contract below is frozen across formats, or an older build that has this rule and a newer
//! one would disagree about where a cache lives: the database file's name ([`DB_FILENAME`]),
//! `PRAGMA user_version` as its format, the manifest's first two lines, the subdirectory's name
//! ([`FORMAT_DIR_PREFIX`] and the format number) and the order of the rule in [`resolve`].

use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use crate::fanout::{MANIFEST_FORMAT, manifest_path, read_manifest, shard_dirname};
use crate::gate::GATE;
use crate::open::{self, db_path};
#[cfg(doc)]
use crate::schema::DB_FILENAME;
use crate::schema::FORMAT_VERSION;
use crate::{Error, Result};

/// The versioned subdirectory's name, before the format number.
pub const FORMAT_DIR_PREFIX: &str = "diskcache-oxide-format-";

/// The versioned subdirectory of `root` for this build's format: `root/diskcache-oxide-format-N`.
/// Joined onto `root` as given, never onto a canonical path, so a Windows `\\?\` prefix never
/// reaches `directory` (plan 25, R-L13).
#[must_use]
pub fn format_dir(root: &Path) -> PathBuf {
    root.join(format_dir_name(FORMAT_VERSION))
}

/// `diskcache-oxide-format-<version>`.
#[must_use]
pub fn format_dir_name(version: i64) -> String {
    format!("{FORMAT_DIR_PREFIX}{version}")
}

/// The format a directory's name declares, if it's named like a versioned subdirectory.
#[must_use]
pub fn format_of_dir_name(name: &str) -> Option<i64> {
    let digits = name.strip_prefix(FORMAT_DIR_PREFIX)?;
    crate::fanout::parse_decimal(digits).map(i64::from)
}

/// Whether `path` is named like a versioned subdirectory. Looks at the absolute path, so `.` run
/// from inside one counts too (review, low 10).
#[must_use]
pub fn is_format_dir(path: &Path) -> bool {
    let absolute = std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf());
    absolute.file_name().and_then(|n| n.to_str()).and_then(format_of_dir_name).is_some()
}

/// What kind of cache is being opened, which decides what counts as the root's own files.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// A plain `Cache`: the root's database.
    Plain,
    /// A `FanoutCache`: the root's manifest, and shard 0's database.
    Fanout,
}

/// The cache in another format found at the root.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Found {
    /// A database whose `user_version` is `version` (a plain cache's, or a shard's).
    Database { path: PathBuf, version: i64 },
    /// A `FanoutCache` manifest whose `format` line is `format`, a number compared with
    /// [`MANIFEST_FORMAT`], not [`FORMAT_VERSION`].
    Manifest { path: PathBuf, format: i64 },
}

impl Found {
    /// The file that declared the other format.
    #[must_use]
    pub fn path(&self) -> &Path {
        match self {
            Self::Database { path, .. } | Self::Manifest { path, .. } => path,
        }
    }

    /// Whether a newer build made it (else an older one).
    #[must_use]
    pub fn is_newer(&self) -> bool {
        match self {
            Self::Database { version, .. } => *version > FORMAT_VERSION,
            Self::Manifest { format, .. } => *format > MANIFEST_FORMAT,
        }
    }

    /// The format number as found, for messages: a database's `user_version`, or for a manifest
    /// `None`, since its number isn't a database format.
    #[must_use]
    pub fn database_format(&self) -> Option<i64> {
        match self {
            Self::Database { version, .. } => Some(*version),
            Self::Manifest { .. } => None,
        }
    }
}

/// A cache opened in `to` rather than the `from` it was asked for, because `from` holds a cache in
/// another format. The caller logs it (once per process per `to`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Redirect {
    /// The directory as passed.
    pub from: PathBuf,
    /// The versioned subdirectory in use.
    pub to: PathBuf,
    /// The other format's cache at `from`.
    pub found: Found,
    /// `to` already existed: this open didn't move anything, the root's other-format cache is just
    /// still there (it can be removed with `remove_old_formats()` once it's older and unused).
    pub existing: bool,
}

/// An opened cache and where it lives.
#[derive(Debug)]
pub struct Resolved<T> {
    pub value: T,
    /// The directory the cache is in: the root, or its versioned subdirectory.
    pub directory: PathBuf,
    /// Set when the cache isn't at the root because of another format there.
    pub redirect: Option<Redirect>,
}

/// Opens the cache asked for at `root` with `attempt`, in the root or in its versioned
/// subdirectory (plan 26):
///
/// 0. A root named like a versioned subdirectory is opened as given, with no redirect.
/// 1. If the subdirectory exists, it's used.
/// 2. Otherwise the root is tried. If that created the root's cache while the subdirectory appeared
///    (another opener redirected meanwhile), the new handle is given back with `release` and the
///    subdirectory is used instead, so the two openers share one cache.
/// 3. If the root holds another format, the subdirectory is created (owner-only, never its parent)
///    and used. A root deleted meanwhile runs the rule once more.
///
/// `RoleMismatch` and every other error propagate. So does a `FormatMismatch` from a plain cache
/// whose parent is a `FanoutCache` root (one of its shards, of any format), as `RoleMismatch`.
/// Format checks read the database with `timeout`, entering the fork gate until `deadline`.
pub fn resolve<T>(
    root: &Path,
    kind: Kind,
    timeout: Duration,
    deadline: Instant,
    mut attempt: impl FnMut(&Path) -> Result<T>,
    release: impl Fn(T),
) -> Result<Resolved<T>> {
    let at = |value, directory: &Path, redirect| Resolved { value, directory: directory.to_path_buf(), redirect };
    if is_format_dir(root) {
        return attempt(root).map(|value| at(value, root, None));
    }
    let sub = format_dir(root);
    for round in 0..2 {
        if is_dir(&sub)? {
            let value = attempt(&sub)?;
            let redirect = still_present(root, kind, timeout, deadline).map(|found| Redirect {
                from: root.to_path_buf(),
                to: sub.clone(),
                found,
                existing: true,
            });
            return Ok(at(value, &sub, redirect));
        }
        let existed = root_has_cache(root, kind)?;
        let found = match attempt(root) {
            Ok(value) => {
                // The handle goes back on every path but staying, a failed stat included.
                let moved = if existed { Ok(false) } else { is_dir(&sub) };
                return match moved {
                    Ok(false) => Ok(at(value, root, None)),
                    Ok(true) => {
                        release(value);
                        attempt(&sub).map(|value| at(value, &sub, None))
                    }
                    Err(error) => {
                        release(value);
                        Err(error)
                    }
                };
            }
            Err(Error::FormatMismatch { found, expected, path }) => classify(root, kind, found, expected, path)?,
            Err(error) => return Err(error),
        };
        match open::owner_only_dir_builder().create(&sub) {
            Ok(()) => {}
            Err(e) if e.kind() == ErrorKind::AlreadyExists && is_dir(&sub)? => {}
            // The root was deleted after it was read: start over, once.
            Err(e) if e.kind() == ErrorKind::NotFound && round == 0 => continue,
            Err(source) => return Err(Error::Io { path: sub, source }),
        }
        let value = attempt(&sub)?;
        let redirect = Redirect { from: root.to_path_buf(), to: sub.clone(), found, existing: false };
        return Ok(at(value, &sub, Some(redirect)));
    }
    // Only a first round's `NotFound` loops, so the second round always returns above.
    Err(Error::Io { path: sub, source: std::io::Error::other("the cache directory kept vanishing") })
}

/// What a root's `FormatMismatch` found, or the error to raise instead.
fn classify(root: &Path, kind: Kind, found: i64, expected: i64, path: Option<PathBuf>) -> Result<Found> {
    if kind == Kind::Plain {
        // A FanoutCache shard in any format: a plain Cache must be refused, never redirected into
        // the shard's directory (plan 26, H1). A stat only.
        if let Some(parent) = root.parent() {
            let manifest = manifest_path(parent);
            if exists(&manifest)? {
                return Err(Error::RoleMismatch {
                    path: db_path(root),
                    reason: "this directory is a shard of a FanoutCache (its parent holds the FanoutCache manifest); \
                             open the FanoutCache's root directory instead of one of its shards"
                        .to_owned(),
                });
            }
        }
    }
    let manifest = manifest_path(root);
    if kind == Kind::Fanout && expected == MANIFEST_FORMAT && path.as_deref() == Some(manifest.as_path()) {
        return Ok(Found::Manifest { path: manifest, format: found });
    }
    Ok(Found::Database { path: path.unwrap_or_else(|| db_path(root)), version: found })
}

/// Whether the root holds a cache of this kind: its database, or its manifest.
fn root_has_cache(root: &Path, kind: Kind) -> Result<bool> {
    match kind {
        Kind::Plain => exists(&db_path(root)),
        Kind::Fanout => exists(&manifest_path(root)),
    }
}

/// What other-format cache is still at the root while the subdirectory is in use, if any. Reads only
/// when the root's file exists (rarely, after a format change), and never fails the open: a root it
/// can't read reports nothing. A root in this build's format (left by the race in [`resolve`], or by
/// a build without this rule) isn't reported: it's no older cache, and nothing will remove it.
fn still_present(root: &Path, kind: Kind, timeout: Duration, deadline: Instant) -> Option<Found> {
    match kind {
        Kind::Plain => other_database(root, timeout, deadline),
        Kind::Fanout => match read_manifest(root) {
            Err(Error::FormatMismatch { found, .. }) => {
                Some(Found::Manifest { path: manifest_path(root), format: found })
            }
            Ok(Some(_)) => other_database(&root.join(shard_dirname(0)), timeout, deadline),
            Ok(None) | Err(_) => None,
        },
    }
}

/// `directory`'s database, if it's there and in another format than this build's.
fn other_database(directory: &Path, timeout: Duration, deadline: Instant) -> Option<Found> {
    match read_format(directory, timeout, deadline) {
        Ok(Some(version)) if version != 0 && version != FORMAT_VERSION => {
            Some(Found::Database { path: db_path(directory), version })
        }
        _ => None,
    }
}

/// The `user_version` of `directory`'s database, or `None` if it has none. Reads and writes nothing
/// else: no WAL switch, no schema, no pin. The connection is opened and dropped inside one gate
/// level, as every connection must be (plan 26, M6), so it's closed before this returns (on Windows
/// that frees the file for a delete). 0 means a database still being created, or not ours.
pub fn read_format(directory: &Path, timeout: Duration, deadline: Instant) -> Result<Option<i64>> {
    let db = db_path(directory);
    if !exists(&db)? {
        return Ok(None);
    }
    // A store in this process only ever has this build's format open: no connection needed, and
    // none opened beside its pool (a pending descriptor, D-017; review, low 8).
    if crate::store::Store::is_open_in_process(&db, deadline)? {
        return Ok(Some(FORMAT_VERSION));
    }
    // Retried as `Store`'s open is: connecting can report busy at once while another opener sets up
    // the WAL's shared memory, which SQLite's busy handler doesn't wait out. Each attempt enters the
    // gate and drops its connection before leaving it.
    let left = deadline.saturating_duration_since(Instant::now());
    crate::lockwait::retry(left, |attempt_deadline| {
        let gate = GATE.enter_shared_until(attempt_deadline).ok_or(Error::Timeout { seconds: 0.0 })?;
        // Never creating the file: it may have been removed since the look above (review, M2).
        let version = match open::connect_existing(directory, timeout)? {
            Some(conn) => Some(
                conn.query_row("PRAGMA user_version", [], |row| row.get(0))
                    .map_err(|e| Error::from_sqlite(e, timeout))?,
            ),
            None => None,
        };
        drop(gate);
        Ok(version)
    })
}

/// Whether `path` exists. Any other `stat` failure is an error, never "absent".
fn exists(path: &Path) -> Result<bool> {
    path.try_exists().map_err(|source| Error::Io { path: path.to_path_buf(), source })
}

/// Whether `path` is a directory (following a link). Absent is `false`; any other `stat` failure is
/// an error.
fn is_dir(path: &Path) -> Result<bool> {
    match std::fs::metadata(path) {
        Ok(meta) => Ok(meta.is_dir()),
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(false),
        Err(source) => Err(Error::Io { path: path.to_path_buf(), source }),
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;
    use crate::fanout::{Manifest, ShardRequest, open_manifest};
    use crate::open::{Requested, Role};
    use crate::store::{DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, OpenOptions, Store};
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
        }
    }

    fn open(dir: &Path) -> Result<Resolved<Arc<Store>>> {
        Store::open_resolving(&options(dir))
    }

    /// Opens and closes a format-N cache in `dir`, then stamps it `version` with an empty WAL, so
    /// the database file alone holds everything and its bytes can be compared.
    fn make_cache(dir: &Path, version: i64) {
        let store = Store::open(&options(dir)).unwrap();
        store.release_handle();
        let conn = open::connect(dir, T).unwrap();
        conn.execute_batch(&format!("PRAGMA user_version = {version}; PRAGMA wal_checkpoint(TRUNCATE);")).unwrap();
    }

    fn user_version(dir: &Path) -> i64 {
        read_format(dir, T, Instant::now() + T).unwrap().unwrap()
    }

    #[test]
    fn an_older_root_redirects_to_the_subdirectory_and_is_left_unchanged() {
        let tmp = TempDir::new("layout-older");
        make_cache(tmp.path(), 2);
        let before = std::fs::read(db_path(tmp.path())).unwrap();
        // An explicit setting is applied in the subdirectory, never written at the root.
        let opts = OpenOptions {
            requested: Requested { size_limit: Some(9 << 20), ..Requested::default() },
            ..options(tmp.path())
        };
        let resolved = Store::open_resolving(&opts).unwrap();
        let sub = format_dir(tmp.path());
        assert_eq!(resolved.directory, sub);
        assert_eq!(resolved.value.directory(), sub);
        let redirect = resolved.redirect.clone().unwrap();
        assert_eq!(redirect.from, tmp.path());
        assert_eq!(redirect.to, sub);
        assert!(!redirect.existing);
        assert!(matches!(redirect.found, Found::Database { version: 2, .. }), "{redirect:?}");
        assert!(!redirect.found.is_newer());
        resolved.value.release_handle();
        assert_eq!(user_version(&sub), FORMAT_VERSION);
        assert_eq!(user_version(tmp.path()), 2);
        assert_eq!(std::fs::read(db_path(tmp.path())).unwrap(), before, "the old root is never written");
    }

    #[test]
    fn a_newer_root_redirects_too() {
        let tmp = TempDir::new("layout-newer");
        make_cache(tmp.path(), FORMAT_VERSION + 1);
        let resolved = open(tmp.path()).unwrap();
        let redirect = resolved.redirect.clone().unwrap();
        assert!(redirect.found.is_newer());
        assert_eq!(resolved.directory, format_dir(tmp.path()));
        resolved.value.release_handle();
    }

    #[test]
    fn an_existing_subdirectory_is_used_whatever_the_root_holds() {
        // Empty root: the subdirectory, nothing created at the root, nothing to report.
        let tmp = TempDir::new("layout-sticky-empty");
        std::fs::create_dir(format_dir(tmp.path())).unwrap();
        let resolved = open(tmp.path()).unwrap();
        assert_eq!(resolved.directory, format_dir(tmp.path()));
        assert_eq!(resolved.redirect, None);
        assert!(!db_path(tmp.path()).exists(), "the sticky path never creates a root database");
        resolved.value.release_handle();

        // A root in this build's format (the race, or a build without the rule): still the
        // subdirectory, and nothing reported, since it's no older cache.
        let tmp = TempDir::new("layout-sticky-current");
        make_cache(tmp.path(), FORMAT_VERSION);
        std::fs::create_dir(format_dir(tmp.path())).unwrap();
        let resolved = open(tmp.path()).unwrap();
        assert_eq!(resolved.directory, format_dir(tmp.path()));
        assert_eq!(resolved.redirect, None);
        resolved.value.release_handle();

        // An old root still there: reported as `existing`, on every open, until it's gone.
        let tmp = TempDir::new("layout-sticky-old");
        make_cache(tmp.path(), 2);
        open(tmp.path()).unwrap().value.release_handle();
        let resolved = open(tmp.path()).unwrap();
        let redirect = resolved.redirect.clone().unwrap();
        assert!(redirect.existing);
        assert!(matches!(redirect.found, Found::Database { version: 2, .. }), "{redirect:?}");
        resolved.value.release_handle();
    }

    #[test]
    fn an_empty_directory_uses_the_root() {
        let tmp = TempDir::new("layout-empty");
        let dir = tmp.path().join("cache");
        let resolved = open(&dir).unwrap();
        assert_eq!(resolved.directory, dir);
        assert_eq!(resolved.redirect, None);
        assert!(!format_dir(&dir).exists());
        resolved.value.release_handle();
    }

    #[test]
    fn a_subdirectory_in_another_format_is_refused_and_never_nested() {
        let tmp = TempDir::new("layout-sub-mismatch");
        make_cache(tmp.path(), 2);
        let sub = format_dir(tmp.path());
        make_cache(&sub, FORMAT_VERSION + 1);
        let err = open(tmp.path()).unwrap_err();
        assert!(matches!(err, Error::FormatMismatch { found, .. } if found == FORMAT_VERSION + 1), "{err:?}");
        // Opened directly, as user code might from `cache.directory`: refused, not redirected.
        let err = open(&sub).unwrap_err();
        assert!(matches!(err, Error::FormatMismatch { .. }), "{err:?}");
        assert!(!format_dir(&sub).exists(), "no second level");
    }

    #[test]
    fn layout_errors_at_the_root_still_raise_and_create_nothing() {
        // A shard count that differs, and a damaged manifest: config or damage, never redirected.
        let tmp = TempDir::new("layout-layout-errors");
        let manifest = Manifest::new(2).unwrap();
        open_manifest(tmp.path(), ShardRequest::Exactly(manifest)).unwrap();
        let deadline = Instant::now() + T;
        let err = resolve(
            tmp.path(),
            Kind::Fanout,
            T,
            deadline,
            |dir| open_manifest(dir, ShardRequest::Exactly(Manifest::new(4).unwrap())),
            |_| {},
        )
        .unwrap_err();
        assert!(matches!(err, Error::ShardCountMismatch { .. }), "{err:?}");
        std::fs::write(manifest_path(tmp.path()), "diskcache-oxide fanout\nformat = 1\nshards = x\n").unwrap();
        let err =
            resolve(tmp.path(), Kind::Fanout, T, deadline, |dir| open_manifest(dir, ShardRequest::Stored), |_| {})
                .unwrap_err();
        assert!(matches!(err, Error::ManifestInvalid { .. }), "{err:?}");
        assert!(!format_dir(tmp.path()).exists());
    }

    #[test]
    fn a_mismatch_inside_the_subdirectory_says_to_open_its_parent() {
        let tmp = TempDir::new("layout-sub-message");
        let sub = format_dir(tmp.path());
        make_cache(&sub, FORMAT_VERSION + 1);
        let message = open(&sub).unwrap_err().to_string();
        assert!(message.contains("open its parent directory"), "{message}");
    }

    #[test]
    fn a_plain_cache_on_a_fanout_shard_of_any_format_is_refused_not_redirected() {
        let tmp = TempDir::new("layout-shard");
        let manifest = Manifest::new(2).unwrap();
        open_manifest(tmp.path(), ShardRequest::Exactly(manifest)).unwrap();
        let shards = Store::open_shards(&options(tmp.path()), manifest, Instant::now() + T).unwrap();
        for shard in &shards {
            shard.release_handle();
        }
        let current = tmp.path().join(shard_dirname(0));
        let err = open(&current).unwrap_err();
        assert!(matches!(err, Error::RoleMismatch { .. }), "{err:?}");

        let old = tmp.path().join(shard_dirname(1));
        open::connect(&old, T).unwrap().execute_batch("PRAGMA user_version = 2").unwrap();
        let err = open(&old).unwrap_err();
        assert!(matches!(err, Error::RoleMismatch { .. }), "{err:?}");
        assert!(!format_dir(&old).exists(), "nothing created inside the shard");
    }

    #[test]
    fn an_open_that_created_the_root_moves_to_a_subdirectory_that_appeared_meanwhile() {
        // Another opener redirected between this one's look for the subdirectory and its create at
        // the root: this one gives its root handle back and joins the subdirectory (rule 2).
        let tmp = TempDir::new("layout-restat");
        let released = AtomicUsize::new(0);
        let resolved = resolve(
            tmp.path(),
            Kind::Plain,
            T,
            Instant::now() + T,
            |dir| {
                if dir == tmp.path() {
                    std::fs::write(db_path(dir), b"").unwrap();
                    std::fs::create_dir(format_dir(dir)).unwrap();
                }
                Ok(dir.to_path_buf())
            },
            |_| {
                released.fetch_add(1, Ordering::Relaxed);
            },
        )
        .unwrap();
        assert_eq!(resolved.directory, format_dir(tmp.path()));
        assert_eq!(resolved.value, format_dir(tmp.path()));
        assert_eq!(released.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn a_root_deleted_mid_redirect_runs_the_rule_again() {
        let tmp = TempDir::new("layout-vanished");
        let root = tmp.path().join("cache");
        std::fs::create_dir(&root).unwrap();
        let calls = AtomicUsize::new(0);
        let resolved = resolve(
            &root,
            Kind::Plain,
            T,
            Instant::now() + T,
            |dir| {
                if calls.fetch_add(1, Ordering::Relaxed) == 0 {
                    // The root reads as another format, then vanishes before the subdirectory's mkdir.
                    std::fs::remove_dir_all(dir).unwrap();
                    return Err(Error::FormatMismatch { found: 2, expected: FORMAT_VERSION, path: None });
                }
                Ok(dir.to_path_buf())
            },
            |_| {},
        )
        .unwrap();
        assert_eq!(resolved.directory, root, "the second round finds an empty root");
        assert_eq!(resolved.redirect, None);
        assert!(!root.exists(), "the rule itself never recreates the root");
    }

    #[test]
    fn racing_openers_of_an_old_root_agree_on_the_subdirectory() {
        let tmp = TempDir::new("layout-race");
        make_cache(tmp.path(), 2);
        let barrier = std::sync::Barrier::new(8);
        let dirs: Vec<PathBuf> = std::thread::scope(|s| {
            let handles: Vec<_> = (0..8)
                .map(|_| {
                    s.spawn(|| {
                        barrier.wait();
                        let resolved = open(tmp.path()).unwrap();
                        resolved.value.release_handle();
                        resolved.directory
                    })
                })
                .collect();
            handles.into_iter().map(|h| h.join().unwrap()).collect()
        });
        assert!(dirs.iter().all(|d| *d == format_dir(tmp.path())), "{dirs:?}");
    }

    /// Opens a `FanoutCache` at `root` as the py crate does: the manifest, then the shards.
    fn open_fanout(root: &Path) -> Result<Resolved<Vec<Arc<Store>>>> {
        let deadline = Instant::now() + T;
        resolve(
            root,
            Kind::Fanout,
            T,
            deadline,
            |dir| {
                let manifest = open_manifest(dir, ShardRequest::Stored)?;
                Store::open_shards(&options(dir), manifest, deadline)
            },
            |stores| Store::close_all(&stores.iter().collect::<Vec<_>>(), None),
        )
    }

    fn close(stores: &[Arc<Store>]) {
        Store::close_all(&stores.iter().collect::<Vec<_>>(), None);
    }

    /// A two-shard `FanoutCache` root whose shard databases are stamped `version`.
    fn make_fanout(root: &Path, version: i64) {
        let manifest = Manifest::new(2).unwrap();
        open_manifest(root, ShardRequest::Exactly(manifest)).unwrap();
        close(&Store::open_shards(&options(root), manifest, Instant::now() + T).unwrap());
        for index in 0..2 {
            let shard = root.join(shard_dirname(index));
            let conn = open::connect(&shard, T).unwrap();
            conn.execute_batch(&format!("PRAGMA user_version = {version}; PRAGMA wal_checkpoint(TRUNCATE);")).unwrap();
        }
    }

    #[test]
    fn a_fanout_root_with_old_shards_redirects_as_a_whole() {
        let tmp = TempDir::new("layout-fanout-shards");
        make_fanout(tmp.path(), 2);
        let resolved = open_fanout(tmp.path()).unwrap();
        let sub = format_dir(tmp.path());
        assert_eq!(resolved.directory, sub);
        assert_eq!(resolved.value.len(), 8, "a new root in the subdirectory, with the default count");
        let redirect = resolved.redirect.clone().unwrap();
        assert!(matches!(redirect.found, Found::Database { version: 2, .. }), "{redirect:?}");
        close(&resolved.value);
        for index in 0..2 {
            assert_eq!(user_version(&tmp.path().join(shard_dirname(index))), 2, "old shards untouched");
        }
        // Sticky afterwards, reporting the old root while it's there.
        let again = open_fanout(tmp.path()).unwrap();
        assert_eq!(again.directory, sub);
        assert!(again.redirect.as_ref().is_some_and(|r| r.existing));
        close(&again.value);
    }

    #[test]
    fn a_fanout_root_with_another_manifest_format_redirects() {
        let tmp = TempDir::new("layout-fanout-manifest");
        make_fanout(tmp.path(), FORMAT_VERSION);
        let manifest = manifest_path(tmp.path());
        let text = std::fs::read_to_string(&manifest).unwrap().replace("format = 1", "format = 2");
        std::fs::write(&manifest, text).unwrap();
        let resolved = open_fanout(tmp.path()).unwrap();
        assert_eq!(resolved.directory, format_dir(tmp.path()));
        let redirect = resolved.redirect.clone().unwrap();
        assert!(matches!(redirect.found, Found::Manifest { format: 2, .. }), "{redirect:?}");
        assert!(redirect.found.is_newer());
        close(&resolved.value);
    }

    #[test]
    fn an_old_fanout_root_missing_a_shard_gets_nothing_created_in_it() {
        // Shard 0 is gone and shard 1 is old: shard 1 is tried first and refused, so shard 0 is never
        // re-created in this build's format inside the old root (plan 26, M4). A 0-byte database
        // counts as missing.
        let tmp = TempDir::new("layout-fanout-missing");
        make_fanout(tmp.path(), 2);
        let shard0 = tmp.path().join(shard_dirname(0));
        std::fs::remove_dir_all(&shard0).unwrap();
        let resolved = open_fanout(tmp.path()).unwrap();
        assert_eq!(resolved.directory, format_dir(tmp.path()));
        close(&resolved.value);
        assert!(!shard0.exists(), "the missing shard was not created in the old root");

        let tmp = TempDir::new("layout-fanout-empty");
        make_fanout(tmp.path(), 2);
        let db0 = db_path(&tmp.path().join(shard_dirname(0)));
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{suffix}", db0.display()));
        }
        std::fs::write(&db0, b"").unwrap();
        let resolved = open_fanout(tmp.path()).unwrap();
        assert_eq!(resolved.directory, format_dir(tmp.path()));
        close(&resolved.value);
        assert_eq!(std::fs::metadata(&db0).unwrap().len(), 0, "the empty shard was not created in the old root");
    }

    #[test]
    fn a_current_fanout_root_opens_in_place() {
        let tmp = TempDir::new("layout-fanout-current");
        make_fanout(tmp.path(), FORMAT_VERSION);
        let resolved = open_fanout(tmp.path()).unwrap();
        assert_eq!(resolved.directory, tmp.path());
        assert_eq!(resolved.redirect, None);
        assert_eq!(resolved.value.len(), 2);
        close(&resolved.value);
    }

    #[test]
    fn format_directory_names() {
        assert_eq!(format_dir_name(3), "diskcache-oxide-format-3");
        assert_eq!(format_of_dir_name("diskcache-oxide-format-3"), Some(3));
        assert_eq!(format_of_dir_name("diskcache-oxide-format-12"), Some(12));
        for name in ["diskcache-oxide-format-", "diskcache-oxide-format-03", "diskcache-oxide-format-3x", "v3"] {
            assert_eq!(format_of_dir_name(name), None, "{name}");
        }
    }
}
