//! `check(fix)`: the database and the value files against each other (plan 24, release 2).
//!
//! Chunked and mostly lock-free, in the style of `clear()` (D-011): no step holds the write lock or
//! a gate level across a scan, so writers and forks aren't stalled. Only `PRAGMA integrity_check`
//! and, with `fix`, `VACUUM` are long single steps.
//!
//! 1. **Rows against files:** page through the spilled rows by `id`; stat each file with nothing
//!    held. A missing file, a file of the wrong size (a crash cut it short: nothing is fsynced, and
//!    the read path already treats it as missing), or a name that isn't one `spill` writes is
//!    reported if its row is still that version, and with `fix` removed by the D-013
//!    compare-and-delete, which keeps `state` right and does nothing if the row changed. Never
//!    "corrected": that would serve the cut-short bytes as the value.
//! 2. **Files against rows:** every file in the value directories older than
//!    [`spill::TMP_MIN_AGE`] that no row names is reported, and with `fix` removed (regular files
//!    and links only, never a directory). A younger one may be a live writer's: its `.tmp` while it
//!    waits for the lock, or a renamed `.val` between a busy COMMIT and the retry. Unknown files in
//!    the store directory itself are reported (by the same age), never removed; the database's own
//!    files (`diskcache-oxide.sqlite*`) are never reported or touched.
//! 3. **Empty value directories** aren't reported (they're normal on an open cache, D-042); with
//!    `fix` they're swept.
//! 4. **Totals** against the rows, in one read; on a mismatch, with `fix`, recomputed in one short
//!    write.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};

use rusqlite::OptionalExtension;

use crate::ops::Version;
use crate::schema::DB_FILENAME;
use crate::spill;
use crate::store::Store;
use crate::{Error, Result};

/// Rows per page when walking the spilled rows.
const PAGE: i64 = 512;

/// One thing `check()` found, and whether `fix` repaired it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Finding {
    /// A line of `PRAGMA integrity_check` other than `ok`.
    Integrity(String),
    MissingFile {
        path: PathBuf,
        fixed: bool,
    },
    WrongSize {
        path: PathBuf,
        expected: i64,
        actual: u64,
        fixed: bool,
    },
    /// A row whose `filename` isn't one `spill` writes (a damaged row): never made into a path.
    BadFilename {
        name: String,
        fixed: bool,
    },
    UnknownFile {
        path: PathBuf,
        fixed: bool,
    },
    Count {
        stored: i64,
        actual: i64,
        fixed: bool,
    },
    Size {
        stored: i64,
        actual: i64,
        fixed: bool,
    },
}

impl Store {
    /// Checks this store's database and value files against each other; with `fix`, repairs what it
    /// can and `VACUUM`s. Every wait is bounded by `timeout`. Findings are added to `found`, merged
    /// with what's there (a retried check passes the same list, so what an earlier round fixed stays
    /// reported, once), and kept even when a later step fails. Refused inside this thread's own held
    /// transaction (its rows aren't final), and with `fix` inside any (its writes would be another
    /// cache's transaction's to commit, and `VACUUM` can't run in one).
    ///
    /// # Errors
    ///
    /// [`Error::InsideTransaction`] as above; otherwise the reads' and writes' errors (a `Timeout`
    /// among them).
    pub fn check(&self, fix: bool, timeout: Duration, found: &mut Vec<Finding>) -> Result<()> {
        if self.held_by_this_thread() {
            return Err(Error::InsideTransaction("check() can't run inside this thread's transaction on the cache"));
        }
        if fix && crate::gate::GATE.own_depth() > 0 {
            return Err(Error::InsideTransaction(
                "check(fix=True) can't run inside a transaction on another cache: its repairs would be that \
                 transaction's to commit, and VACUUM can't run in one",
            ));
        }
        for line in self.read(timeout, |conn| {
            let mut stmt = conn.prepare("PRAGMA integrity_check").map_err(|e| Error::from_sqlite(e, timeout))?;
            stmt.query_map([], |r| r.get::<_, String>(0))
                .and_then(Iterator::collect::<rusqlite::Result<Vec<String>>>)
                .map_err(|e| Error::from_sqlite(e, timeout))
        })? {
            if line != "ok" {
                merge(found, Finding::Integrity(line));
            }
        }
        self.check_rows(fix, timeout, found)?;
        self.check_files(fix, timeout, found)?;
        if fix {
            let deadline = crate::lockwait::deadline_after(Instant::now(), timeout);
            spill::sweep(self.directory(), deadline, spill::SWEEP_MIN_AGE, spill::sweep_start());
        }
        self.check_totals(fix, timeout, found)?;
        if fix {
            self.vacuum(timeout)?;
        }
        Ok(())
    }

    fn check_rows(&self, fix: bool, timeout: Duration, found: &mut Vec<Finding>) -> Result<()> {
        let sqlite = |e| Error::from_sqlite(e, timeout);
        // Paged on the partial index over spilled rows, by name, so a cache of mostly inline values
        // reads only its spilled rows (paging by `id` would scan every row to fill a page).
        let mut after = String::new();
        loop {
            let rows: Vec<(i64, i64, String, i64)> = self.read(timeout, |conn| {
                conn.prepare_cached(
                    "SELECT id, store_seq, filename, size FROM cache
                     WHERE filename IS NOT NULL AND filename > ?1 ORDER BY filename LIMIT ?2",
                )
                .and_then(|mut stmt| {
                    stmt.query_map((&after, PAGE), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?.collect()
                })
                .map_err(sqlite)
            })?;
            let Some((_, _, last, _)) = rows.last() else { return Ok(()) };
            after.clone_from(last);
            for (id, store_seq, name, size) in rows {
                let version = Version::new(id, store_seq);
                let problem = match spill::value_path(self.directory(), &name) {
                    None => Some(Finding::BadFilename { name, fixed: false }),
                    Some(path) => match std::fs::metadata(&path) {
                        Err(_) => Some(Finding::MissingFile { path, fixed: false }),
                        Ok(meta) if i64::try_from(meta.len()).ok() != Some(size) => {
                            Some(Finding::WrongSize { path, expected: size, actual: meta.len(), fixed: false })
                        }
                        Ok(_) => None,
                    },
                };
                let Some(mut problem) = problem else { continue };
                // Overwritten or deleted since the page was read (its old file unlinked after the
                // commit): not a problem.
                if !self.is_current(version, timeout)? {
                    continue;
                }
                if fix {
                    let removed = self.delete_version(timeout, version)?;
                    set_fixed(&mut problem, removed);
                }
                merge(found, problem);
            }
        }
    }

    /// Whether `version` is still the stored one.
    fn is_current(&self, version: Version, timeout: Duration) -> Result<bool> {
        let (id, seq) = version.parts();
        self.read(timeout, |conn| {
            conn.prepare_cached("SELECT 1 FROM cache WHERE id = ?1 AND store_seq = ?2")
                .and_then(|mut stmt| stmt.exists((id, seq)))
                .map_err(|e| Error::from_sqlite(e, timeout))
        })
    }

    fn check_files(&self, fix: bool, timeout: Duration, found: &mut Vec<Finding>) -> Result<()> {
        let directory = self.directory().to_path_buf();
        let now = SystemTime::now();
        let old = |path: &Path| {
            std::fs::symlink_metadata(path)
                .ok()
                .and_then(|meta| meta.modified().ok())
                .and_then(|modified| now.duration_since(modified).ok())
                .is_some_and(|age| age >= spill::TMP_MIN_AGE)
        };
        let Ok(entries) = std::fs::read_dir(&directory) else { return Ok(()) };
        let mut names: Vec<_> = entries.filter_map(std::result::Result::ok).collect();
        names.sort_by_key(std::fs::DirEntry::file_name);
        for entry in names {
            let name = entry.file_name();
            let Some(name) = name.to_str() else { continue };
            let path = entry.path();
            let Ok(meta) = std::fs::symlink_metadata(&path) else { continue };
            if meta.is_dir() {
                if spill::is_hex(name, 2) {
                    self.check_value_dir(&path, name, fix, timeout, &old, found)?;
                }
                // Anything else is someone else's: a FanoutCache's shards are their own stores.
                continue;
            }
            if name.starts_with(DB_FILENAME) || !old(&path) {
                continue;
            }
            merge(found, Finding::UnknownFile { path, fixed: false });
        }
        Ok(())
    }

    fn check_value_dir(
        &self,
        dir: &Path,
        dir_name: &str,
        fix: bool,
        timeout: Duration,
        old: &impl Fn(&Path) -> bool,
        found: &mut Vec<Finding>,
    ) -> Result<()> {
        let Ok(entries) = std::fs::read_dir(dir) else { return Ok(()) };
        let mut paths: Vec<PathBuf> = entries.filter_map(std::result::Result::ok).map(|e| e.path()).collect();
        paths.sort();
        for path in paths {
            let Ok(meta) = std::fs::symlink_metadata(&path) else { continue };
            if meta.is_dir() || !old(&path) {
                continue;
            }
            let file_name = path.file_name().and_then(|n| n.to_str()).unwrap_or_default();
            let relative = format!("{dir_name}/{file_name}");
            let named = self.read(timeout, |conn| {
                conn.prepare_cached("SELECT 1 FROM cache WHERE filename = ?1")
                    .and_then(|mut stmt| stmt.query_row([&relative], |_| Ok(())).optional())
                    .map_err(|e| Error::from_sqlite(e, timeout))
            })?;
            if named.is_some() {
                continue;
            }
            // A delete's unlink follows its commit (and a batch's runs detached), so a file no row
            // names may be one being removed right now: report only one still there afterwards.
            if std::fs::symlink_metadata(&path).is_err() {
                continue;
            }
            // Windows: another program holding the file for a moment is waited out (plan 25, R-M7).
            let fixed =
                fix && crate::os_error::retry_sharing_violations_detached(|| std::fs::remove_file(&path)).is_ok();
            merge(found, Finding::UnknownFile { path, fixed });
        }
        Ok(())
    }

    fn check_totals(&self, fix: bool, timeout: Duration, found: &mut Vec<Finding>) -> Result<()> {
        let sqlite = |e| Error::from_sqlite(e, timeout);
        // One statement, so one snapshot: two reads outside a transaction could each see a different
        // commit, and a write landing between them would look like drift.
        let (count, size, rows, sum): (i64, i64, i64, i64) = self.read(timeout, |conn| {
            conn.query_row(
                "SELECT count, size, (SELECT COUNT(*) FROM cache), (SELECT COALESCE(SUM(size), 0) FROM cache)
                 FROM state WHERE only = 1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .map_err(sqlite)
        })?;
        if (count, size) == (rows, sum) {
            return Ok(());
        }
        if fix {
            self.write(timeout, |_, tx| {
                tx.execute(
                    "UPDATE state SET count = (SELECT COUNT(*) FROM cache),
                                       size = (SELECT COALESCE(SUM(size), 0) FROM cache) WHERE only = 1",
                    [],
                )
                .map(drop)
                .map_err(sqlite)
            })?;
        }
        if count != rows {
            merge(found, Finding::Count { stored: count, actual: rows, fixed: fix });
        }
        if size != sum {
            merge(found, Finding::Size { stored: size, actual: sum, fixed: fix });
        }
        Ok(())
    }
}

/// Adds `new` to `found` unless the same problem is there already (a retried round finding it again),
/// keeping `fixed` if either says so.
fn merge(found: &mut Vec<Finding>, new: Finding) {
    let unfixed = |f: &Finding| {
        let mut f = f.clone();
        set_fixed(&mut f, false);
        f
    };
    let key = unfixed(&new);
    if let Some(old) = found.iter_mut().find(|old| unfixed(old) == key) {
        if is_fixed(&new) {
            *old = new;
        }
        return;
    }
    found.push(new);
}

fn is_fixed(finding: &Finding) -> bool {
    match finding {
        Finding::MissingFile { fixed, .. }
        | Finding::WrongSize { fixed, .. }
        | Finding::BadFilename { fixed, .. }
        | Finding::UnknownFile { fixed, .. }
        | Finding::Count { fixed, .. }
        | Finding::Size { fixed, .. } => *fixed,
        Finding::Integrity(_) => false,
    }
}

fn set_fixed(finding: &mut Finding, value: bool) {
    match finding {
        Finding::MissingFile { fixed, .. }
        | Finding::WrongSize { fixed, .. }
        | Finding::BadFilename { fixed, .. }
        | Finding::UnknownFile { fixed, .. }
        | Finding::Count { fixed, .. }
        | Finding::Size { fixed, .. } => *fixed = value,
        Finding::Integrity(_) => {}
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::sync::Arc;
    use std::time::{Duration, SystemTime};

    use super::*;
    use crate::open::Requested;
    use crate::ops::Value;
    use crate::schema::MODE_BYTES;
    use crate::store::{DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, OpenOptions};
    use crate::testutil::TempDir;

    const T: Duration = Duration::from_secs(5);
    const NOW: f64 = 1_000_000.0;

    fn store(dir: &Path) -> Arc<Store> {
        Store::open(&OpenOptions {
            directory: dir.to_path_buf(),
            timeout: T,
            requested: Requested::default(),
            max_connections: DEFAULT_MAX_CONNECTIONS,
            cache_pages: DEFAULT_CACHE_PAGES,
            wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
            auto_checkpoint: false,
            role: crate::open::Role::Plain,
            default_size_limit: crate::schema::DEFAULT_SIZE_LIMIT,
            maintenance: crate::background::Maintenance::OFF,
        })
        .unwrap()
    }

    fn put_big(s: &Store, key: &str) -> PathBuf {
        let big = vec![7u8; spill::THRESHOLD * 2];
        s.set(T, key, Value::Bytes { mode: MODE_BYTES, data: &big }, None, NOW).unwrap();
        let name: String = s
            .read(T, |c| {
                c.query_row("SELECT filename FROM cache WHERE key = ?1", [key], |r| r.get(0))
                    .map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap();
        spill::value_path(s.directory(), &name).unwrap()
    }

    fn raw(s: &Store, sql: &str) {
        s.write(T, |_, c| c.execute_batch(sql).map_err(|e| Error::from_sqlite(e, T))).unwrap();
    }

    fn age(path: &Path, by: Duration) {
        let file = std::fs::OpenOptions::new().write(true).open(path).unwrap();
        file.set_modified(SystemTime::now() - by).unwrap();
    }

    fn run(s: &Store, fix: bool) -> Vec<Finding> {
        let mut found = Vec::new();
        s.check(fix, T, &mut found).unwrap();
        found
    }

    fn len(s: &Store) -> i64 {
        s.len(T).unwrap()
    }

    #[test]
    fn a_healthy_cache_has_no_findings() {
        let tmp = TempDir::new("check-healthy");
        let s = store(tmp.path());
        s.set(T, "small", Value::Bytes { mode: MODE_BYTES, data: b"v" }, None, NOW).unwrap();
        put_big(&s, "big");
        assert_eq!(run(&s, false), []);
        assert_eq!(run(&s, true), [], "fix on a healthy cache changes nothing");
        assert_eq!(len(&s), 2);
    }

    #[test]
    fn a_missing_or_short_value_file_is_reported_and_fix_removes_its_row() {
        let tmp = TempDir::new("check-missing");
        let s = store(tmp.path());
        let gone = put_big(&s, "gone");
        let short = put_big(&s, "short");
        std::fs::remove_file(&gone).unwrap();
        std::fs::OpenOptions::new().write(true).open(&short).unwrap().set_len(10).unwrap();
        let found = run(&s, false);
        assert!(found.contains(&Finding::MissingFile { path: gone.clone(), fixed: false }), "{found:?}");
        assert!(
            found
                .iter()
                .any(|f| matches!(f, Finding::WrongSize { path, actual: 10, fixed: false, .. } if *path == short)),
            "{found:?}"
        );
        assert_eq!(len(&s), 2, "reporting changes nothing");
        let fixed = run(&s, true);
        assert!(fixed.contains(&Finding::MissingFile { path: gone, fixed: true }), "{fixed:?}");
        assert_eq!(len(&s), 0, "both rows went, never 'corrected' to the short file's size");
        assert_eq!(run(&s, false), []);
    }

    #[test]
    fn a_damaged_filename_is_reported_and_never_made_into_a_path() {
        let tmp = TempDir::new("check-bad-name");
        let s = store(tmp.path());
        put_big(&s, "k");
        raw(&s, "UPDATE cache SET filename = '../../etc/passwd'");
        let found = run(&s, true);
        assert!(found.contains(&Finding::BadFilename { name: "../../etc/passwd".into(), fixed: true }), "{found:?}");
        assert_eq!(len(&s), 0);
    }

    #[test]
    fn only_an_old_unreferenced_file_is_reported_and_only_in_value_dirs_is_one_removed() {
        let tmp = TempDir::new("check-orphans");
        let s = store(tmp.path());
        let kept = put_big(&s, "k");
        let dir = kept.parent().unwrap();
        let young = dir.join("young-orphan.val");
        let old = dir.join("old-orphan.val");
        let old_tmp = dir.join(format!("{}.tmp", "a".repeat(32)));
        let root = tmp.path().join("stray.txt");
        for path in [&young, &old, &old_tmp, &root] {
            std::fs::write(path, b"x").unwrap();
        }
        for path in [&old, &old_tmp, &root] {
            age(path, spill::TMP_MIN_AGE * 2);
        }
        age(&kept, spill::TMP_MIN_AGE * 2); // Old, but referenced.
        let found = run(&s, false);
        let unknown: Vec<&PathBuf> = found
            .iter()
            .filter_map(|f| if let Finding::UnknownFile { path, .. } = f { Some(path) } else { None })
            .collect();
        assert_eq!(unknown.len(), 3, "{found:?}");
        assert!(unknown.contains(&&old) && unknown.contains(&&old_tmp) && unknown.contains(&&root), "{found:?}");
        run(&s, true);
        assert!(!old.exists() && !old_tmp.exists(), "old orphans in a value dir are removed");
        assert!(young.exists(), "a young file may be a live writer's");
        assert!(root.exists(), "a file outside the value dirs is reported, never removed");
        assert!(kept.exists() && tmp.path().join("diskcache-oxide.sqlite").exists());
    }

    #[test]
    fn drifted_totals_are_reported_and_fixed() {
        let tmp = TempDir::new("check-totals");
        let s = store(tmp.path());
        s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: b"v" }, None, NOW).unwrap();
        raw(&s, "UPDATE state SET count = 99, size = 1234 WHERE only = 1");
        let found = run(&s, true);
        assert!(found.contains(&Finding::Count { stored: 99, actual: 1, fixed: true }), "{found:?}");
        assert!(found.contains(&Finding::Size { stored: 1234, actual: 0, fixed: true }), "{found:?}");
        assert_eq!(len(&s), 1);
        assert_eq!(run(&s, false), []);
    }

    #[test]
    fn check_is_refused_inside_a_held_transaction() {
        let tmp = TempDir::new("check-held");
        let s = store(tmp.path());
        let held = s.begin_hold(T).unwrap();
        assert!(matches!(s.check(false, T, &mut Vec::new()), Err(Error::InsideTransaction(_))));
        held.finish(false).unwrap();
    }
}
