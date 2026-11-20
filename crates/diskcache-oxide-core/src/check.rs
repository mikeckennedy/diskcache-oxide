//! `check(fix)`: the database and the value files against each other (plan 24, release 2).
//!
//! Chunked and mostly lock-free, in the style of `clear()` (D-011): no step holds the write lock
//! across a scan, so writers aren't stalled. The long single steps are `PRAGMA integrity_check`,
//! the two reads that visit every row (step 0 on a cache with nothing damaged, and the totals),
//! which hold a connection and a gate level for one pass over the table, and, with `fix`, `VACUUM`.
//!
//! 0. **Column types:** a row whose `mode`, `size`, `store_seq`, `expire_time` or `filename` holds a
//!    value of the wrong SQLite type (a damaged page, or a hand that edited the file) is reported,
//!    and with `fix` removed, before anything below reads those columns as the types the schema
//!    gives them. The later steps leave such rows out, so they neither raise nor loop on one.
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
    /// A row with a column of the wrong SQLite type (a damaged row): `column` holds a `found`
    /// (`typeof()`'s word for it) where the schema stores something else.
    BadColumn {
        id: i64,
        column: &'static str,
        found: String,
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
    /// A column of the one `state` row that doesn't hold what the schema stores there (damage):
    /// `count`, `size` or `next_seq`, holding a `found` (`typeof()`'s word for it, or
    /// `rows::SEQ_OUT_OF_RANGE`). With `fix`, the totals are recounted and the next sequence number
    /// is set past every row's.
    BadState {
        column: &'static str,
        found: String,
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
        self.check_column_types(fix, timeout, found)?;
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

    /// Step 0: rows with a column of the wrong type. A scan of the table, a page at a time by `id`
    /// (the rowid, which can't hold another type), reading only `typeof()` of each column, so
    /// nothing here can fail on what it finds. With `fix` the row goes, by `id` alone: its
    /// `store_seq` may be the damaged column.
    fn check_column_types(&self, fix: bool, timeout: Duration, found: &mut Vec<Finding>) -> Result<()> {
        const COLUMNS: [(&str, &[&str]); 5] = [
            ("mode", &["integer"]),
            ("size", &["integer"]),
            ("store_seq", &["integer"]),
            ("expire_time", &["real", "integer", "null"]),
            ("filename", &["text", "null"]),
        ];
        let mut after = 0_i64;
        loop {
            let rows: Vec<(i64, [String; 5])> = self.read(timeout, |conn| {
                conn.prepare_cached(&format!(
                    "SELECT id, typeof(mode),
                            CASE WHEN typeof(size) = 'integer' AND (size < 0 OR size > {})
                                 THEN '{}' ELSE typeof(size) END,
                            CASE WHEN typeof(store_seq) = 'integer' AND store_seq >= 4611686018427387903
                                 THEN '{}' ELSE typeof(store_seq) END,
                            typeof(expire_time), typeof(filename)
                     FROM cache WHERE id > ?1 AND {} ORDER BY id LIMIT ?2",
                    crate::rows::MAX_SIZE,
                    crate::rows::SIZE_OUT_OF_RANGE,
                    crate::rows::SEQ_OUT_OF_RANGE,
                    crate::rows::WRONG_TYPE
                ))
                .and_then(|mut stmt| {
                    stmt.query_map((after, PAGE), |r| {
                        Ok((r.get(0)?, [r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?]))
                    })?
                    .collect()
                })
                .map_err(|e| Error::from_sqlite(e, timeout))
            })?;
            let Some((last, _)) = rows.last() else { return Ok(()) };
            after = *last;
            for (id, kinds) in rows {
                let fixed = fix
                    && self.write_files(timeout, crate::store::LockPriority::Normal, |writer, _| {
                        let removed = writer.delete_damaged(id)?;
                        Ok(crate::store::Changed::removing(removed.is_some(), removed))
                    })?;
                if fix && !fixed {
                    // Deleted or overwritten since the page was read: no longer a damaged row.
                    continue;
                }
                for ((column, allowed), kind) in COLUMNS.into_iter().zip(kinds) {
                    if !allowed.contains(&kind.as_str()) {
                        merge(found, Finding::BadColumn { id, column, found: kind, fixed });
                    }
                }
            }
        }
    }

    fn check_rows(&self, fix: bool, timeout: Duration, found: &mut Vec<Finding>) -> Result<()> {
        let sqlite = |e| Error::from_sqlite(e, timeout);
        // Paged on the partial index over spilled rows, by name, so a cache of mostly inline values
        // reads only its spilled rows (paging by `id` would scan every row to fill a page).
        // The cursor is the name's bytes as stored, and each name is read as bytes: a TEXT value
        // needn't be valid UTF-8 (a flipped bit in a name keeps the column's type), and reading one
        // as a `String` would end the check with an error on the damage it's here to find.
        //
        // The cursor is the name and the row's `id`, which is how the index orders its entries: by
        // name alone, rows sharing a name (damage: no two rows are given one) would be skipped
        // past a page's end. It starts before the empty name, which is damage too and text all the same.
        let mut after: (Vec<u8>, i64) = (Vec::new(), i64::MIN);
        loop {
            let rows: Vec<(i64, i64, Vec<u8>, i64)> = self.read(timeout, |conn| {
                conn.prepare_cached(CHECK_ROWS_SQL)
                    .and_then(|mut stmt| {
                        stmt.query_map((TextBytes(&after.0), after.1, PAGE), |r| {
                            Ok((r.get(0)?, r.get(1)?, r.get_ref(2)?.as_bytes()?.to_vec(), r.get(3)?))
                        })?
                        .collect()
                    })
                    .map_err(sqlite)
            })?;
            let Some((last_id, _, last, _)) = rows.last() else { return Ok(()) };
            after = (last.clone(), *last_id);
            for (id, store_seq, name, size) in rows {
                // Lossy: a name that isn't UTF-8 becomes one that can't be a value path (below).
                let name = String::from_utf8_lossy(&name).into_owned();
                let version = Version::new(id, store_seq);
                let problem = match spill::value_path(self.directory(), &name) {
                    None => Some(Finding::BadFilename { name, fixed: false }),
                    Some(path) => match std::fs::metadata(&path) {
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                            Some(Finding::MissingFile { path, fixed: false })
                        }
                        // Only a file that isn't there is missing, as a read has it (`spill::read`):
                        // one that can't be looked at (a folder without permission, an I/O error) may
                        // be whole, so `fix` mustn't remove its row. The check stops with the error.
                        Err(e) => return Err(Error::Io { path, source: e }),
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
            let fixed = if fix {
                match self.remove_unnamed(&path, &relative, timeout)? {
                    Orphan::Named => continue,
                    Orphan::Removed => true,
                    Orphan::Kept => false,
                }
            } else {
                false
            };
            merge(found, Finding::UnknownFile { path, fixed });
        }
        Ok(())
    }

    /// Removes a value file no committed row names, unless an uncommitted one does.
    ///
    /// A transaction held open for over an hour has written such a file: old, and named only by a
    /// row nobody else can see yet. Unlinking it would let that transaction commit a row with no
    /// file. So the unlink runs inside a write transaction of its own: with SQLite's write lock held
    /// no other connection has an uncommitted row, and asking again whether a row names the file
    /// gives the final answer. A transaction held elsewhere makes this wait, and fail with
    /// [`Error::Timeout`] at `timeout`, as the `VACUUM` that follows would.
    ///
    /// The lock is taken per file, not for the scan. [`Store::write`] may run its closure more than
    /// once (a busy commit), so a file already gone counts as removed.
    fn remove_unnamed(&self, path: &Path, relative: &str, timeout: Duration) -> Result<Orphan> {
        self.write(timeout, |_, conn| {
            let named = conn
                .prepare_cached("SELECT 1 FROM cache WHERE filename = ?1")
                .and_then(|mut stmt| stmt.exists([relative]))
                .map_err(|e| Error::from_sqlite(e, timeout))?;
            if named {
                return Ok(Orphan::Named);
            }
            // Windows: another program holding the file for a moment is waited out (plan 25, R-M7).
            Ok(match crate::os_error::retry_sharing_violations_detached(|| std::fs::remove_file(path)) {
                Ok(()) => Orphan::Removed,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => Orphan::Removed,
                Err(_) => Orphan::Kept,
            })
        })
    }

    /// The last step: the one `state` row against the table. Its own columns are read by type
    /// first, so a damaged one is a finding here and not an error (an overflowed total is a REAL,
    /// which no typed read takes). `next_seq` is damaged when it isn't an integer below 2^62: every
    /// write fails on such a floor, so `fix` sets it past every row's number.
    fn check_totals(&self, fix: bool, timeout: Duration, found: &mut Vec<Finding>) -> Result<()> {
        type Totals = (Option<i64>, String, Option<i64>, String, String, i64, i64);
        let sqlite = |e| Error::from_sqlite(e, timeout);
        // One statement, so one snapshot: two reads outside a transaction could each see a different
        // commit, and a write landing between them would look like drift.
        let (count, count_kind, size, size_kind, seq_kind, rows, sum): Totals = self.read(timeout, |conn| {
            conn.query_row(
                // A size that isn't one counts as 0, as it does when its row is removed
                // (`rows::lenient_size`); summed as it is, it would make the total a REAL, or overflow.
                &format!(
                    "SELECT CASE WHEN typeof(count) = 'integer' THEN count END, typeof(count),
                            CASE WHEN typeof(size) = 'integer' THEN size END, typeof(size),
                            CASE WHEN typeof(next_seq) = 'integer' AND next_seq >= 4611686018427387904
                                 THEN '{}' ELSE typeof(next_seq) END,
                            (SELECT COUNT(*) FROM cache),
                            (SELECT COALESCE(SUM({}), 0) FROM cache)
                     FROM state WHERE only = 1",
                    crate::rows::SEQ_OUT_OF_RANGE,
                    crate::rows::sound_size!()
                ),
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?)),
            )
            .map_err(sqlite)
        })?;
        let mut problems = Vec::new();
        match count {
            Some(count) if count == rows => {}
            Some(stored) => problems.push(Finding::Count { stored, actual: rows, fixed: fix }),
            None => problems.push(Finding::BadState { column: "count", found: count_kind, fixed: fix }),
        }
        match size {
            Some(size) if size == sum => {}
            Some(stored) => problems.push(Finding::Size { stored, actual: sum, fixed: fix }),
            None => problems.push(Finding::BadState { column: "size", found: size_kind, fixed: fix }),
        }
        if seq_kind != "integer" {
            problems.push(Finding::BadState { column: "next_seq", found: seq_kind, fixed: fix });
        }
        if problems.is_empty() {
            return Ok(());
        }
        if fix {
            self.write(timeout, |_, tx| {
                tx.execute(
                    concat!(
                        "UPDATE state SET
                             count = (SELECT COUNT(*) FROM cache),
                             size = (SELECT COALESCE(SUM(",
                        crate::rows::sound_size!(),
                        "), 0) FROM cache),
                             next_seq = CASE WHEN typeof(next_seq) = 'integer' AND next_seq < 4611686018427387904
                                             THEN next_seq ELSE ",
                        crate::rows::max_store_seq!(),
                        " + 1 END
                         WHERE only = 1"
                    ),
                    [],
                )
                .map(drop)
                .map_err(sqlite)
            })?;
        }
        for problem in problems {
            merge(found, problem);
        }
        Ok(())
    }
}

/// A page of [`Store::check_rows`]: spilled rows after the cursor `(filename, id)`, in the order of
/// the partial index over `filename` (whose entries end in the rowid), so nothing is sorted.
///
/// Only rows whose columns hold what the schema says: the others are step 0's, and a `filename` of
/// another type would sort after every text one and come back on every page.
const CHECK_ROWS_SQL: &str = "SELECT id, store_seq, filename, size FROM cache
     WHERE filename IS NOT NULL AND (filename, id) > (?1, ?2) AND typeof(filename) = 'text'
       AND typeof(size) = 'integer' AND size BETWEEN 0 AND 281474976710656 AND typeof(store_seq) = 'integer'
     ORDER BY filename, id LIMIT ?3";

/// A cursor over `filename`, bound as the TEXT bytes it was read as (see `check_rows`).
struct TextBytes<'a>(&'a [u8]);

impl rusqlite::ToSql for TextBytes<'_> {
    fn to_sql(&self) -> rusqlite::Result<rusqlite::types::ToSqlOutput<'_>> {
        Ok(rusqlite::types::ToSqlOutput::Borrowed(rusqlite::types::ValueRef::Text(self.0)))
    }
}

/// What [`Store::remove_unnamed`] did with a file no committed row named.
enum Orphan {
    /// A row names it after all: one committed since the look, or a transaction's that just ended.
    Named,
    Removed,
    /// It couldn't be removed.
    Kept,
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
        | Finding::BadColumn { fixed, .. }
        | Finding::UnknownFile { fixed, .. }
        | Finding::Count { fixed, .. }
        | Finding::Size { fixed, .. }
        | Finding::BadState { fixed, .. } => *fixed,
        Finding::Integrity(_) => false,
    }
}

fn set_fixed(finding: &mut Finding, value: bool) {
    match finding {
        Finding::MissingFile { fixed, .. }
        | Finding::WrongSize { fixed, .. }
        | Finding::BadFilename { fixed, .. }
        | Finding::BadColumn { fixed, .. }
        | Finding::UnknownFile { fixed, .. }
        | Finding::Count { fixed, .. }
        | Finding::Size { fixed, .. }
        | Finding::BadState { fixed, .. } => *fixed = value,
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

    #[cfg(unix)]
    #[test]
    fn a_value_file_that_cant_be_looked_at_is_an_error_and_fix_keeps_its_row() {
        use std::os::unix::fs::PermissionsExt;
        let tmp = TempDir::new("check-no-permission");
        let s = store(tmp.path());
        let file = put_big(&s, "kept");
        let folder = file.parent().unwrap();
        let mode = |bits| std::fs::set_permissions(folder, std::fs::Permissions::from_mode(bits)).unwrap();
        mode(0o000);
        // Root looks through any folder: then there is nothing to test.
        if std::fs::metadata(&file).is_ok() {
            mode(0o700);
            return;
        }
        let mut found = Vec::new();
        let refused = s.check(true, T, &mut found);
        mode(0o700);
        assert!(matches!(&refused, Err(Error::Io { path, .. }) if *path == file), "{refused:?} {found:?}");
        assert_eq!(len(&s), 1, "the row stays: its file is whole");
        assert_eq!(run(&s, true), []);
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

    /// Plan 32, D3. A transaction held open for over an hour has written a value file that only its
    /// own uncommitted row names. `check(fix=True)` used to unlink it as an orphan, and the commit
    /// then landed a row with no file.
    #[test]
    fn fix_waits_for_a_held_transaction_before_it_removes_a_file_no_committed_row_names() {
        let tmp = TempDir::new("check-held-orphan");
        let s = store(tmp.path());
        let committed = put_big(&s, "k");
        // The file the held transaction wrote: beside the committed one, same size, two hours old.
        let name = committed.file_name().unwrap().to_str().unwrap();
        let (stem, extension) = name.split_once('.').unwrap();
        let last = if stem.ends_with('0') { '1' } else { '0' };
        let held = committed.with_file_name(format!("{}{last}.{extension}", &stem[..stem.len() - 1]));
        std::fs::copy(&committed, &held).unwrap();
        age(&held, Duration::from_secs(7_200));
        let relative = format!(
            "{}/{}",
            held.parent().unwrap().file_name().unwrap().to_str().unwrap(),
            held.file_name().unwrap().to_str().unwrap()
        );
        assert!(spill::value_path(s.directory(), &relative).is_some(), "not a value file's name: {relative}");
        // Another connection's open transaction points the row at it, uncommitted.
        let other = crate::open::connect(tmp.path(), T).unwrap();
        other.execute_batch("BEGIN IMMEDIATE").unwrap();
        other.execute("UPDATE cache SET filename = ?1", [&relative]).unwrap();

        let short = Duration::from_millis(200);
        let mut found = Vec::new();
        let waited = s.check(true, short, &mut found);
        assert!(matches!(waited, Err(Error::Timeout { .. })), "{waited:?} {found:?}");
        assert!(held.exists(), "the held transaction's file was removed under it");

        other.execute_batch("COMMIT").unwrap();
        let found = run(&s, true);
        assert!(held.exists(), "a file a committed row names was removed");
        assert!(!found.iter().any(|f| matches!(f, Finding::UnknownFile { path, .. } if *path == held)), "{found:?}");
        // The row reads its value from the file the transaction wrote.
        assert_eq!(len(&s), 1);
        assert!(run(&s, false).iter().all(|f| !matches!(f, Finding::MissingFile { .. })));
    }

    // -- plan 32, D1: damaged columns ----------------------------------------------------------------

    fn read_of(s: &Store, key: &str) -> Result<bool> {
        s.get_with(T, key, NOW, |_| false, |_, _| Ok(())).map(|hit| hit.is_some())
    }

    /// One spilled value under `k`, one inline under `a`, and `column` of `k`'s row set to `value`
    /// (an SQL expression), as a damaged page or an edit by hand leaves it.
    fn damaged(name: &str, column: &str, value: &str) -> (TempDir, Arc<Store>, PathBuf) {
        let tmp = TempDir::new(name);
        let s = store(tmp.path());
        let file = put_big(&s, "k");
        s.set(T, "a", Value::Bytes { mode: MODE_BYTES, data: b"inline" }, None, NOW).unwrap();
        raw(&s, &format!("UPDATE cache SET {column} = {value} WHERE filename IS NOT NULL"));
        (tmp, s, file)
    }

    /// A `filename` and a `size` of each wrong type the column can hold. (A number put in
    /// `filename` is stored as text by the column's affinity: a name `spill` never writes.)
    const BAD_FILE_COLUMNS: [(&str, &str); 5] = [
        ("filename", "x'fffe'"),
        ("filename", "42"),
        // TEXT that isn't UTF-8, as a flipped bit inside a name leaves it.
        ("filename", "CAST(x'fffe' AS TEXT)"),
        ("size", "'big'"),
        ("size", "1.5"),
    ];

    /// The totals agree with the rows, unless a damaged `size` was in play: what that row really
    /// weighed is lost, so `state.size` stays off by it until `fix` recomputes it.
    #[track_caller]
    fn totals_right_unless_a_size_was_damaged(s: &Store, column: &str, value: &str) {
        let found = run(s, false);
        assert!(!found.iter().any(|f| matches!(f, Finding::Count { .. })), "{column} = {value}: {found:?}");
        if column == "filename" {
            assert!(!found.iter().any(|f| matches!(f, Finding::Size { .. })), "{column} = {value}: {found:?}");
        }
        run(s, true);
        let after = run(s, false);
        assert!(!after.iter().any(|f| matches!(f, Finding::Count { .. } | Finding::Size { .. })), "{after:?}");
    }

    #[test]
    fn a_damaged_filename_or_size_reads_as_a_miss_and_never_blocks_a_delete() {
        for (column, value) in BAD_FILE_COLUMNS {
            let (_tmp, s, file) = damaged("check-lenient-read", column, value);
            // D-013: a damaged row is a miss, and the read removes it when that needs no waiting.
            assert!(!read_of(&s, "k").unwrap(), "{column} = {value}: a hit");
            assert!(read_of(&s, "a").unwrap(), "{column} = {value}: the other row");
            assert!(!s.delete(T, "k", NOW).unwrap(), "{column} = {value}: the miss left the row");
            assert_eq!(len(&s), 1, "{column} = {value}");
            // A name that isn't text is never made into a path, so the file it once named stays
            // for `check` to find; a row with a good name and a bad size takes its file with it.
            assert_eq!(file.exists(), column == "filename", "{column} = {value}");
            totals_right_unless_a_size_was_damaged(&s, column, value);
        }
    }

    #[test]
    fn a_damaged_filename_or_size_doesnt_stop_a_delete_a_set_a_clear_or_an_eviction() {
        for (column, value) in BAD_FILE_COLUMNS {
            // A delete of the damaged row itself, and an overwrite of it.
            let (_tmp, s, _) = damaged("check-lenient-delete", column, value);
            assert!(s.delete(T, "k", NOW).unwrap(), "{column} = {value}: delete");
            let (_tmp, s, _) = damaged("check-lenient-set", column, value);
            s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: b"new" }, None, NOW).unwrap();
            assert!(read_of(&s, "k").unwrap(), "{column} = {value}: the overwrite reads back");
            totals_right_unless_a_size_was_damaged(&s, column, value);
            // The batch deletes that reach it: `clear()`, and the eviction a `set` over the limit runs.
            let (_tmp, s, _) = damaged("check-lenient-clear", column, value);
            assert_eq!(s.clear(T).unwrap(), 2, "{column} = {value}: clear");
            assert_eq!(len(&s), 0);
            let (_tmp, s, _) = damaged("check-lenient-cull", column, value);
            s.set_limits(T, Requested { size_limit: Some(crate::schema::MIN_SIZE_LIMIT), ..Requested::default() })
                .unwrap();
            let big = vec![1u8; usize::try_from(crate::schema::MIN_SIZE_LIMIT).unwrap()];
            s.set(T, "fills", Value::Bytes { mode: MODE_BYTES, data: &big }, None, NOW).unwrap();
            let left: i64 = s
                .read(T, |c| {
                    c.query_row("SELECT COUNT(*) FROM cache WHERE filename IS NOT NULL AND key != 'fills'", [], |r| {
                        r.get(0)
                    })
                    .map_err(|e| Error::from_sqlite(e, T))
                })
                .unwrap();
            assert_eq!(left, 0, "{column} = {value}: the damaged row, the oldest, wasn't evicted");
        }
    }

    #[test]
    fn check_reports_a_column_of_the_wrong_type_and_fix_removes_the_row() {
        let cases = [
            ("filename", "x'fffe'", "blob"),
            ("size", "'big'", "text"),
            ("size", "1.5", "real"),
            ("mode", "'two'", "text"),
            ("store_seq", "x'00'", "blob"),
            ("store_seq", "9223372036854775807", crate::rows::SEQ_OUT_OF_RANGE),
            ("store_seq", "4611686018427387904", crate::rows::SEQ_OUT_OF_RANGE),
            // The last number below the ceiling: no write can follow it.
            ("store_seq", "4611686018427387903", crate::rows::SEQ_OUT_OF_RANGE),
            // Sizes that would overflow `state.size` into a REAL, or `check`'s own SUM, if counted.
            ("size", "-1", crate::rows::SIZE_OUT_OF_RANGE),
            ("size", "-9223372036854775807", crate::rows::SIZE_OUT_OF_RANGE),
            ("size", "-9223372036854775807 - 1", crate::rows::SIZE_OUT_OF_RANGE),
            ("size", "9223372036854775807", crate::rows::SIZE_OUT_OF_RANGE),
            ("expire_time", "'soon'", "text"),
        ];
        for (column, value, kind) in cases {
            let (_tmp, s, file) = damaged("check-bad-column", column, value);
            let id: i64 = s
                .read(T, |c| {
                    c.query_row(
                        "SELECT id FROM cache WHERE filename IS NOT NULL OR typeof(filename) != 'null' LIMIT 1",
                        [],
                        |r| r.get(0),
                    )
                    .map_err(|e| Error::from_sqlite(e, T))
                })
                .unwrap();
            let found = run(&s, false);
            let expected = Finding::BadColumn { id, column, found: kind.to_owned(), fixed: false };
            assert!(found.contains(&expected), "{column} = {value}: {found:?}");
            assert_eq!(len(&s), 2, "a check without fix removes nothing");
            // Twice over: paging by a column that sorts past every good value must still end.
            assert!(run(&s, false).contains(&expected));

            let fixed = run(&s, true);
            assert!(
                fixed.contains(&Finding::BadColumn { id, column, found: kind.to_owned(), fixed: true }),
                "{fixed:?}"
            );
            assert_eq!(len(&s), 1, "{column} = {value}: the damaged row is gone, the other stays");
            assert!(read_of(&s, "a").unwrap());
            assert!(!read_of(&s, "k").unwrap());
            // A row with a good name takes its value file with it. One whose name was the damage
            // leaves the file, which nothing names now: a later `check(fix=True)` removes it once
            // it's over an hour old, and until then it's too young to report.
            assert_eq!(file.exists(), column == "filename", "{column} = {value}");
            assert_eq!(run(&s, false), Vec::new(), "{column} = {value}: clean afterwards");
        }
    }

    #[test]
    fn an_inline_row_with_a_damaged_size_is_reported_and_fixed_too() {
        // The spilled-rows index never visits it, so only the column pass can.
        let tmp = TempDir::new("check-bad-inline-size");
        let s = store(tmp.path());
        s.set(T, "a", Value::Bytes { mode: MODE_BYTES, data: b"inline" }, None, NOW).unwrap();
        s.set(T, "b", Value::Bytes { mode: MODE_BYTES, data: b"inline" }, None, NOW).unwrap();
        raw(&s, "UPDATE cache SET size = 'six' WHERE key = (SELECT key FROM cache ORDER BY id LIMIT 1)");
        let found = run(&s, false);
        assert!(
            found.iter().any(|f| matches!(f, Finding::BadColumn { column: "size", fixed: false, .. })),
            "{found:?}"
        );
        // An inline row weighs nothing in `state.size`, and the damaged size counts as 0: no drift.
        assert!(!found.iter().any(|f| matches!(f, Finding::Size { .. } | Finding::Count { .. })), "{found:?}");
        run(&s, true);
        assert_eq!(len(&s), 1);
        assert_eq!(run(&s, false), Vec::new());
        // Deleting what's left brings the totals to zero: nothing was miscounted on the way.
        assert_eq!(s.clear(T).unwrap(), 1);
        assert_eq!((len(&s), s.volume(T).is_ok()), (0, true));
    }

    #[test]
    fn check_reports_a_text_filename_that_isnt_utf8_and_fix_removes_its_row() {
        let (_tmp, s, _) = damaged("check-bad-utf8", "filename", "CAST(x'fffe' AS TEXT)");
        let found = run(&s, false);
        assert!(found.iter().any(|f| matches!(f, Finding::BadFilename { fixed: false, .. })), "{found:?}");
        assert_eq!(run(&s, false), found, "paged past it both times");
        let fixed = run(&s, true);
        assert!(fixed.iter().any(|f| matches!(f, Finding::BadFilename { fixed: true, .. })), "{fixed:?}");
        assert_eq!(len(&s), 1);
        assert_eq!(run(&s, false), Vec::new());
    }

    #[test]
    fn fix_leaves_a_row_that_was_overwritten_since_the_check_saw_it_damaged() {
        // An overwrite keeps the row's id, so a delete by id alone would take the healthy new value.
        let (_tmp, s, _) = damaged("check-healed", "size", "'big'");
        let id: i64 = s
            .read(T, |c| {
                c.query_row("SELECT id FROM cache WHERE typeof(size) != 'integer'", [], |r| r.get(0))
                    .map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap();
        s.set(T, "k", Value::Bytes { mode: MODE_BYTES, data: b"healthy" }, None, NOW).unwrap();
        let removed = s
            .write_files(T, crate::store::LockPriority::Normal, |writer, _| {
                let removed = writer.delete_damaged(id)?;
                Ok(crate::store::Changed::removing(removed.is_some(), removed))
            })
            .unwrap();
        assert!(!removed, "a healthy row was deleted by its id");
        assert!(read_of(&s, "k").unwrap());
    }

    /// Gemini's review of plan 32, M4: an inline row is never visited by name, and a size at the top
    /// of the range, summed with any other, overflowed `check`'s own SUM into an error.
    #[test]
    fn an_inline_row_with_a_size_out_of_range_is_reported_and_fixed_not_summed() {
        let tmp = TempDir::new("check-inline-size-range");
        let s = store(tmp.path());
        put_big(&s, "k");
        s.set(T, "a", Value::Bytes { mode: MODE_BYTES, data: b"inline" }, None, NOW).unwrap();
        raw(&s, "UPDATE cache SET size = 9223372036854775807 WHERE filename IS NULL");
        let found = run(&s, false);
        assert!(
            found.iter().any(|f| matches!(f, Finding::BadColumn { column: "size", fixed: false, .. })),
            "{found:?}"
        );
        run(&s, true);
        assert_eq!(len(&s), 1);
        assert_eq!(run(&s, false), Vec::new());
        assert!(s.volume(T).is_ok());
    }

    /// M4: a `state` column of another type (an overflowed total is a REAL) is a finding, not an
    /// error on every later call, and `fix` recounts it.
    #[test]
    fn a_damaged_total_is_reported_and_recounted_never_raised() {
        for (column, value, kind) in [
            ("size", "9.3e18", "real"),
            ("size", "'big'", "text"),
            ("count", "x'00'", "blob"),
            ("count", "1.5", "real"),
        ] {
            let tmp = TempDir::new("check-bad-state");
            let s = store(tmp.path());
            put_big(&s, "k");
            raw(&s, &format!("UPDATE state SET {column} = {value}"));
            let expected = Finding::BadState { column, found: kind.to_owned(), fixed: false };
            assert_eq!(run(&s, false), vec![expected], "{column} = {value}");
            let fixed = run(&s, true);
            assert_eq!(fixed, vec![Finding::BadState { column, found: kind.to_owned(), fixed: true }]);
            assert_eq!(run(&s, false), Vec::new(), "{column} = {value}: clean afterwards");
            assert_eq!((len(&s), s.volume(T).is_ok()), (1, true), "{column} = {value}");
        }
    }

    /// M3: a damaged `state.next_seq` fails every write (never a repeated or a REAL number), still
    /// lets `clear()` run, and `check(fix=True)` sets it past every row's number.
    #[test]
    fn a_damaged_next_seq_fails_writes_is_reported_and_fix_repairs_it() {
        use crate::ops::Number;
        let cases = [
            ("'abc'", "text"),
            ("x'00'", "blob"),
            ("1500.5", "real"),
            ("1e300", "real"),
            ("4611686018427387904", crate::rows::SEQ_OUT_OF_RANGE),
            ("9223372036854775807", crate::rows::SEQ_OUT_OF_RANGE),
        ];
        let seq_of = |s: &Store, key: &str| -> (String, i64) {
            s.read(T, |c| {
                c.query_row("SELECT typeof(store_seq), store_seq FROM cache WHERE key = ?1", [key], |r| {
                    Ok((r.get(0)?, r.get(1)?))
                })
                .map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap()
        };
        for (value, kind) in cases {
            let tmp = TempDir::new("check-bad-next-seq");
            let s = store(tmp.path());
            s.set(T, "a", Value::Bytes { mode: MODE_BYTES, data: b"inline" }, None, NOW).unwrap();
            assert_eq!(s.incr(T, "n", Number::Int(1), Some(Number::Int(0)), NOW).unwrap(), Number::Int(1));
            let before = seq_of(&s, "n");
            raw(&s, &format!("UPDATE state SET next_seq = {value}"));

            // The in-place update of a stored number reads nothing back: it must fail in SQL.
            assert!(s.incr(T, "n", Number::Int(1), Some(Number::Int(0)), NOW).is_err(), "{value}: incr ran");
            assert_eq!(seq_of(&s, "n"), before, "{value}: the row's number changed");
            assert!(s.set(T, "b", Value::Bytes { mode: MODE_BYTES, data: b"x" }, None, NOW).is_err(), "{value}");
            assert!(read_of(&s, "a").unwrap(), "{value}: reads go on");

            let expected = Finding::BadState { column: "next_seq", found: kind.to_owned(), fixed: false };
            assert_eq!(run(&s, false), vec![expected], "{value}");
            run(&s, true);
            assert_eq!(run(&s, false), Vec::new(), "{value}: clean afterwards");
            s.set(T, "b", Value::Bytes { mode: MODE_BYTES, data: b"x" }, None, NOW).unwrap();
            let (b_kind, b) = seq_of(&s, "b");
            assert!(b_kind == "integer" && b > before.1, "{value}: the next number is {b_kind} {b}");
            assert_eq!(s.incr(T, "n", Number::Int(1), Some(Number::Int(0)), NOW).unwrap(), Number::Int(2));
        }
        // And without the repair, `clear()` still removes every row.
        for (value, _) in cases {
            let tmp = TempDir::new("check-bad-next-seq-clear");
            let s = store(tmp.path());
            put_big(&s, "k");
            s.set(T, "a", Value::Bytes { mode: MODE_BYTES, data: b"inline" }, None, NOW).unwrap();
            raw(&s, &format!("UPDATE state SET next_seq = {value}"));
            assert_eq!(s.clear(T).unwrap(), 2, "{value}");
            assert_eq!(len(&s), 0, "{value}");
        }
    }

    /// M3: the last number below the ceiling. Deleting its row raises the floor to the ceiling, where
    /// every write fails; `check` says so, and `fix` brings the floor back down.
    #[test]
    fn a_row_at_the_last_store_seq_never_leaves_the_cache_unwritable_after_fix() {
        let top = "4611686018427387903";
        let set = |s: &Store, key: &str| s.set(T, key, Value::Bytes { mode: MODE_BYTES, data: b"x" }, None, NOW);
        // Removed by fix itself, as a damaged row.
        let (_tmp, s, _) = damaged("check-top-seq", "store_seq", top);
        assert!(set(&s, "b").is_err(), "no number is left after the top one");
        run(&s, true);
        assert_eq!(run(&s, false), Vec::new());
        set(&s, "b").unwrap();
        // Removed by an ordinary delete first: only the floor is left damaged.
        let (_tmp, s, _) = damaged("check-top-seq-deleted", "store_seq", top);
        assert!(s.delete(T, "k", NOW).unwrap());
        assert!(set(&s, "b").is_err());
        let found = run(&s, false);
        assert!(found.iter().any(|f| matches!(f, Finding::BadState { column: "next_seq", .. })), "{found:?}");
        run(&s, true);
        assert_eq!(run(&s, false), Vec::new());
        set(&s, "b").unwrap();
    }

    /// M4: the empty name is text, so only the pass by name can find it, and it sorts first.
    #[test]
    fn check_reports_an_empty_filename_and_fix_removes_its_row() {
        let (_tmp, s, _) = damaged("check-empty-name", "filename", "''");
        let found = run(&s, false);
        assert!(found.contains(&Finding::BadFilename { name: String::new(), fixed: false }), "{found:?}");
        let fixed = run(&s, true);
        assert!(fixed.contains(&Finding::BadFilename { name: String::new(), fixed: true }), "{fixed:?}");
        assert_eq!(len(&s), 1);
    }

    /// M4: rows sharing one name (damage) run past a page's end. Paged by name alone, the next
    /// page began after the name and skipped the rest of them.
    #[test]
    fn rows_sharing_a_filename_are_all_visited_across_a_page_boundary() {
        let tmp = TempDir::new("check-shared-name");
        let s = store(tmp.path());
        let file = put_big(&s, "k");
        let size = i64::try_from(std::fs::metadata(&file).unwrap().len()).unwrap();
        let extra = PAGE + 40;
        for n in 0..extra {
            s.set(T, format!("i{n}").as_str(), Value::Bytes { mode: MODE_BYTES, data: b"inline" }, None, NOW).unwrap();
        }
        // Every row names k's file with its true size, except the last, far past the first page.
        raw(
            &s,
            &format!(
                "UPDATE cache SET filename = (SELECT filename FROM cache WHERE key = 'k'), size = {size};
                 UPDATE cache SET size = {size} + 1 WHERE id = (SELECT MAX(id) FROM cache)"
            ),
        );
        let found = run(&s, false);
        assert!(found.iter().any(|f| matches!(f, Finding::WrongSize { fixed: false, .. })), "{found:?}");
    }

    #[test]
    fn the_pass_by_name_reads_the_filename_index_in_order_without_a_sort() {
        let tmp = TempDir::new("check-rows-plan");
        let s = store(tmp.path());
        let plan: Vec<String> = s
            .read(T, |c| {
                c.prepare(&format!("EXPLAIN QUERY PLAN {CHECK_ROWS_SQL}"))
                    .and_then(|mut stmt| stmt.query_map((TextBytes(b""), i64::MIN, PAGE), |r| r.get(3))?.collect())
                    .map_err(|e| Error::from_sqlite(e, T))
            })
            .unwrap();
        assert!(plan.iter().any(|line| line.contains("USING INDEX cache_filename")), "{plan:?}");
        assert!(!plan.iter().any(|line| line.contains("TEMP B-TREE")), "{plan:?}");
    }
}
