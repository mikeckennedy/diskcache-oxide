//! Phase 1, Spike 4a: throwaway storage prototype. One connection per process (no pool); the
//! experiments in `src/bin/` run it across real OS processes.
//!
//! Spill protocol (what the experiments validate):
//! - set: write the value to `<aa>/<bb>/<name>.tmp` (outside any lock), then BEGIN IMMEDIATE, read
//!   the old filename, rename `.tmp` -> `.val`, upsert the row with a fresh `store_seq`, COMMIT,
//!   then unlink the old `.val`. The rename happens while the write lock is held, so a `.val` file
//!   that no row references, seen while holding the write lock, is always an orphan.
//! - get: read the row; if it names a file, open it with no transaction held. On ENOENT re-read the
//!   row: gone -> miss; filename or `store_seq` changed -> retry; unchanged -> true miss, and try
//!   `DELETE ... WHERE id = ? AND store_seq = ?` with a zero busy timeout.

use std::cell::Cell;
use std::fs;
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rusqlite::{Connection, ErrorCode, OptionalExtension, params};

pub const DB_FILENAME: &str = "diskcache-oxide.sqlite";
pub const FORMAT_VERSION: i64 = 1;
/// Optimistic (lock-free) attempts at a spilled read before falling back to the locked read.
pub const OPTIMISTIC_ATTEMPTS: u32 = 8;

const SCHEMA: &str = "
CREATE TABLE cache (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    mode INTEGER NOT NULL,
    value BLOB,
    filename TEXT,
    size INTEGER NOT NULL,
    store_time REAL NOT NULL,
    expire_time REAL,
    store_seq INTEGER NOT NULL
);
CREATE INDEX cache_store_seq ON cache (store_seq);
CREATE INDEX cache_filename ON cache (filename) WHERE filename IS NOT NULL;
CREATE TABLE settings (name TEXT PRIMARY KEY, value) WITHOUT ROWID;
";

const UPSERT_SQL: &str = "
INSERT INTO cache (key, mode, value, filename, size, store_time, expire_time, store_seq)
VALUES (?1, 2, ?2, ?3, ?4, ?5, NULL, (SELECT COALESCE(MAX(store_seq), 0) + 1 FROM cache))
ON CONFLICT (key) DO UPDATE SET
    mode = excluded.mode, value = excluded.value, filename = excluded.filename, size = excluded.size,
    store_time = excluded.store_time, expire_time = excluded.expire_time, store_seq = excluded.store_seq
";

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;

/// How a connection waits for the write lock.
#[derive(Clone, Copy, Debug)]
pub enum WaitPolicy {
    /// `sqlite3_busy_timeout`: SQLite's own backoff (1, 2, 5, 10, 15, 20, 25, 25, 25, 50, 50, 100 ms...).
    Default(Duration),
    /// A custom busy handler that sleeps `poll` between attempts until `timeout` has elapsed.
    Poll { poll: Duration, timeout: Duration },
}

pub fn apply_wait_policy(conn: &mut Connection, policy: WaitPolicy) -> rusqlite::Result<()> {
    match policy {
        WaitPolicy::Default(timeout) => conn.busy_timeout(timeout),
        WaitPolicy::Poll { poll, timeout } => {
            let mut started = Instant::now();
            conn.busy_handler(Some(move |count: i32| {
                // `count` is 0 on the first call for each locking event, so the deadline resets per event.
                if count == 0 {
                    started = Instant::now();
                }
                if started.elapsed() >= timeout {
                    return false;
                }
                std::thread::sleep(poll);
                true
            }))
        }
    }
}

#[derive(Debug, Default, Clone)]
pub struct OpenReport {
    pub set_wal: bool,
    pub created_schema: bool,
    pub wrote_settings: bool,
}

impl OpenReport {
    pub fn wrote(&self) -> bool {
        self.set_wal || self.created_schema || self.wrote_settings
    }
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub enum OpenStyle {
    /// Check `journal_mode`, `user_version` and the settings first; write only on a mismatch (D-019).
    #[default]
    CheckFirst,
    /// Negative control: what a naive open does (unconditional pragma and settings writes).
    Unconditional,
}

#[derive(Debug)]
pub enum Got {
    Hit(Vec<u8>),
    /// The row was absent (or vanished while we looked).
    Miss,
    /// The row still points at the same missing file. `deleted` is whether the compare-and-delete removed it.
    TrueMiss { deleted: bool },
}

#[derive(Debug, Default, Clone, Copy)]
pub struct GetStats {
    pub retries: u32,
    pub locked_fallback: bool,
}

pub struct Store {
    pub dir: PathBuf,
    pub conn: Connection,
    pub threshold: usize,
    policy: WaitPolicy,
    /// Negative control for the race: diskcache-like reads (ENOENT is a miss, no re-check).
    pub naive_reads: bool,
    /// Test-only: sleep between reading the row and opening its file, to widen the race window.
    pub read_gap: Duration,
    tmp_counter: Cell<u64>,
}

impl Store {
    pub fn open(dir: &Path, threshold: usize, policy: WaitPolicy) -> Result<(Self, OpenReport)> {
        Self::open_with(dir, threshold, policy, OpenStyle::CheckFirst, &[("spill_threshold", threshold.to_string())])
    }

    pub fn open_with(
        dir: &Path,
        threshold: usize,
        policy: WaitPolicy,
        style: OpenStyle,
        settings: &[(&str, String)],
    ) -> Result<(Self, OpenReport)> {
        fs::create_dir_all(dir)?;
        let mut conn = Connection::open(dir.join(DB_FILENAME))?;
        apply_wait_policy(&mut conn, policy)?;
        let mut report = OpenReport::default();

        // 1. journal_mode: reading it never writes; WAL is persistent, so set it only when it isn't already.
        let mode: String = conn.query_row("PRAGMA journal_mode", [], |r| r.get(0))?;
        if style == OpenStyle::Unconditional || !mode.eq_ignore_ascii_case("wal") {
            let mode: String = conn.query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))?;
            if !mode.eq_ignore_ascii_case("wal") {
                return Err(format!("journal_mode is {mode}, not wal").into());
            }
            report.set_wal = style == OpenStyle::CheckFirst;
        }
        conn.execute_batch("PRAGMA synchronous = NORMAL")?; // per connection, never written to the file

        // 2. user_version: 0 means a brand-new file; create the schema under the write lock, re-checking.
        let version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        if version > FORMAT_VERSION {
            return Err(format!("FormatMismatch: database format {version} is newer than {FORMAT_VERSION}").into());
        }
        if version < FORMAT_VERSION {
            conn.execute_batch("BEGIN IMMEDIATE")?;
            let version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
            if version == 0 {
                conn.execute_batch(SCHEMA)?;
                conn.execute_batch(&format!("PRAGMA user_version = {FORMAT_VERSION}"))?;
                report.created_schema = true;
            }
            conn.execute_batch("COMMIT")?;
        } else if style == OpenStyle::Unconditional {
            conn.execute_batch(&format!("PRAGMA user_version = {FORMAT_VERSION}"))?;
        }

        // 3. Settings: compare first, write only what differs.
        let differs = |conn: &Connection| -> rusqlite::Result<bool> {
            let mut stmt = conn.prepare_cached("SELECT value FROM settings WHERE name = ?1")?;
            for (name, value) in settings {
                let stored: Option<String> = stmt.query_row([name], |r| r.get(0)).optional()?;
                if stored.as_deref() != Some(value.as_str()) {
                    return Ok(true);
                }
            }
            Ok(false)
        };
        if style == OpenStyle::Unconditional || differs(&conn)? {
            conn.execute_batch("BEGIN IMMEDIATE")?;
            for (name, value) in settings {
                conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?1, ?2)", params![name, value])?;
            }
            conn.execute_batch("COMMIT")?;
            report.wrote_settings = style == OpenStyle::CheckFirst;
        }

        let store =
            Self { dir: dir.to_path_buf(), conn, threshold, policy, naive_reads: false, read_gap: Duration::ZERO, tmp_counter: Cell::new(0) };
        Ok((store, report))
    }

    pub fn set_policy(&mut self, policy: WaitPolicy) -> rusqlite::Result<()> {
        self.policy = policy;
        apply_wait_policy(&mut self.conn, policy)
    }

    fn new_name(&self) -> String {
        let n = self.tmp_counter.get();
        self.tmp_counter.set(n + 1);
        let mut rng = Rng::new(nanos() ^ (u64::from(std::process::id()) << 32) ^ n.wrapping_mul(0x9E37_79B9));
        format!("{:016x}{:016x}", rng.next(), rng.next())
    }

    /// Relative path of a live value file: `aa/bb/<32 hex>.val`.
    fn rel_path(name: &str) -> String {
        format!("{}/{}/{name}.val", &name[0..2], &name[2..4])
    }

    pub fn set(&self, key: &str, value: &[u8]) -> Result<()> {
        let now = now();
        if value.len() < self.threshold {
            self.conn.execute_batch("BEGIN IMMEDIATE")?;
            let old = self.old_filename(key);
            let done = old.and_then(|old| {
                self.conn.prepare_cached(UPSERT_SQL)?.execute(params![
                    key,
                    value,
                    Option::<String>::None,
                    value.len() as i64,
                    now
                ])?;
                Ok(old)
            });
            return self.finish(done, None);
        }
        let name = self.new_name();
        let rel = Self::rel_path(&name);
        let final_path = self.dir.join(&rel);
        let tmp_path = final_path.with_extension("tmp");
        fs::create_dir_all(final_path.parent().expect("has parent"))?;
        {
            let mut f = fs::File::create_new(&tmp_path)?;
            f.write_all(value)?; // no fsync, same as diskcache
        }
        if let Err(e) = self.conn.execute_batch("BEGIN IMMEDIATE") {
            let _ = fs::remove_file(&tmp_path);
            return Err(e.into());
        }
        let done = self.old_filename(key).and_then(|old| {
            fs::rename(&tmp_path, &final_path).map_err(|e| rusqlite::Error::ToSqlConversionFailure(Box::new(e)))?;
            self.conn.prepare_cached(UPSERT_SQL)?.execute(params![
                key,
                Option::<&[u8]>::None,
                rel,
                value.len() as i64,
                now
            ])?;
            Ok(old)
        });
        self.finish(done, Some((&tmp_path, &final_path)))
    }

    fn old_filename(&self, key: &str) -> rusqlite::Result<Option<String>> {
        self.conn
            .prepare_cached("SELECT filename FROM cache WHERE key = ?1")?
            .query_row([key], |r| r.get::<_, Option<String>>(0))
            .optional()
            .map(Option::flatten)
    }

    /// COMMIT (or ROLLBACK and clean up our new file), then unlink the replaced file.
    fn finish(&self, done: rusqlite::Result<Option<String>>, ours: Option<(&Path, &Path)>) -> Result<()> {
        match done.and_then(|old| self.conn.execute_batch("COMMIT").map(|()| old)) {
            Ok(old) => {
                if let Some(old) = old {
                    remove_ignoring_missing(&self.dir.join(old));
                }
                Ok(())
            }
            Err(e) => {
                let _ = self.conn.execute_batch("ROLLBACK");
                if let Some((tmp, fin)) = ours {
                    remove_ignoring_missing(tmp);
                    remove_ignoring_missing(fin);
                }
                Err(e.into())
            }
        }
    }

    pub fn delete(&self, key: &str) -> Result<bool> {
        self.conn.execute_batch("BEGIN IMMEDIATE")?;
        let old = self.old_filename(key);
        let n = old.as_ref().ok().map(|_| self.conn.execute("DELETE FROM cache WHERE key = ?1", [key]));
        match (old, n) {
            (Ok(old), Some(Ok(n))) => {
                self.conn.execute_batch("COMMIT")?;
                if let Some(old) = old {
                    remove_ignoring_missing(&self.dir.join(old));
                }
                Ok(n > 0)
            }
            (Err(e), _) | (_, Some(Err(e))) => {
                let _ = self.conn.execute_batch("ROLLBACK");
                Err(e.into())
            }
            (Ok(_), None) => unreachable!(),
        }
    }

    fn read_row(&self, key: &str) -> rusqlite::Result<Option<(i64, Option<Vec<u8>>, Option<String>, i64)>> {
        self.conn
            .prepare_cached("SELECT id, value, filename, store_seq FROM cache WHERE key = ?1")?
            .query_row([key], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
            .optional()
    }

    pub fn get(&mut self, key: &str, stats: &mut GetStats) -> Result<Got> {
        let mut row = self.read_row(key)?;
        for _ in 0..OPTIMISTIC_ATTEMPTS {
            let Some((id, value, filename, seq)) = row else { return Ok(Got::Miss) };
            let Some(filename) = filename else { return Ok(Got::Hit(value.unwrap_or_default())) };
            if !self.read_gap.is_zero() {
                std::thread::sleep(self.read_gap);
            }
            match fs::read(self.dir.join(&filename)) {
                Ok(bytes) => return Ok(Got::Hit(bytes)),
                Err(e) if e.kind() == ErrorKind::NotFound => {}
                Err(e) => return Err(e.into()),
            }
            if self.naive_reads {
                return Ok(Got::Miss);
            }
            let again = self.read_row(key)?;
            match &again {
                None => return Ok(Got::Miss),
                Some((id2, _, f2, seq2)) if *id2 == id && f2.as_deref() == Some(filename.as_str()) && *seq2 == seq => {
                    return Ok(Got::TrueMiss { deleted: self.try_compare_and_delete(id, seq)? });
                }
                Some(_) => {
                    stats.retries += 1;
                    row = again;
                }
            }
        }
        // Overwrites keep beating us. Read the row and open the file while holding the write lock:
        // no writer can commit a replacement (and so unlink this file) until we release it, and the
        // open fd keeps the data readable after that. Bounded by the connection's busy timeout.
        stats.locked_fallback = true;
        self.conn.execute_batch("BEGIN IMMEDIATE")?;
        let opened = (|| -> Result<Option<(i64, i64, Option<fs::File>, Option<Vec<u8>>)>> {
            let Some((id, value, filename, seq)) = self.read_row(key)? else { return Ok(None) };
            let Some(filename) = filename else { return Ok(Some((id, seq, None, value))) };
            if !self.read_gap.is_zero() {
                std::thread::sleep(self.read_gap);
            }
            match fs::File::open(self.dir.join(&filename)) {
                Ok(f) => Ok(Some((id, seq, Some(f), None))),
                Err(e) if e.kind() == ErrorKind::NotFound => Ok(Some((id, seq, None, None))),
                Err(e) => Err(e.into()),
            }
        })();
        self.conn.execute_batch("COMMIT")?;
        match opened? {
            None => Ok(Got::Miss),
            Some((_, _, None, Some(inline))) => Ok(Got::Hit(inline)),
            Some((_, _, Some(mut f), _)) => {
                let mut buf = Vec::new();
                std::io::Read::read_to_end(&mut f, &mut buf)?;
                Ok(Got::Hit(buf))
            }
            Some((id, seq, None, None)) => Ok(Got::TrueMiss { deleted: self.try_compare_and_delete(id, seq)? }),
        }
    }

    /// The D-013 opportunistic cleanup: never waits for the write lock, never deletes a newer version.
    pub fn try_compare_and_delete(&mut self, id: i64, seq: i64) -> Result<bool> {
        self.conn.busy_timeout(Duration::ZERO)?;
        let r = self.conn.execute("DELETE FROM cache WHERE id = ?1 AND store_seq = ?2", params![id, seq]);
        // busy_timeout(0) cleared the connection's normal wait policy; put it back.
        apply_wait_policy(&mut self.conn, self.policy)?;
        match r {
            Ok(n) => Ok(n > 0),
            Err(e) if is_busy(&e) => Ok(false),
            Err(e) => Err(e.into()),
        }
    }

    pub fn count(&self) -> rusqlite::Result<i64> {
        self.conn.query_row("SELECT COUNT(*) FROM cache", [], |r| r.get(0))
    }

    /// Chunked `clear()` bounded by a `store_seq` snapshot (D-010, D-011). Returns (rows removed, chunks).
    pub fn clear(&self, chunk: usize, pause: Duration, mut after_chunk: impl FnMut(usize)) -> Result<(usize, usize)> {
        let snapshot: i64 = self.conn.query_row("SELECT COALESCE(MAX(store_seq), 0) FROM cache", [], |r| r.get(0))?;
        let (mut removed, mut chunks) = (0usize, 0usize);
        loop {
            self.conn.execute_batch("BEGIN IMMEDIATE")?;
            let batch: rusqlite::Result<Vec<(i64, Option<String>)>> = (|| {
                let mut stmt = self.conn.prepare_cached(
                    "SELECT id, filename FROM cache WHERE store_seq <= ?1 ORDER BY store_seq LIMIT ?2",
                )?;
                let rows = stmt.query_map(params![snapshot, chunk as i64], |r| Ok((r.get(0)?, r.get(1)?)))?;
                let rows = rows.collect::<rusqlite::Result<Vec<_>>>()?;
                let mut del = self.conn.prepare_cached("DELETE FROM cache WHERE id = ?1")?;
                for (id, _) in &rows {
                    del.execute([id])?;
                }
                Ok(rows)
            })();
            let batch = match batch.and_then(|b| self.conn.execute_batch("COMMIT").map(|()| b)) {
                Ok(b) => b,
                Err(e) => {
                    let _ = self.conn.execute_batch("ROLLBACK");
                    return Err(e.into());
                }
            };
            if batch.is_empty() {
                return Ok((removed, chunks));
            }
            for (_, f) in &batch {
                if let Some(f) = f {
                    remove_ignoring_missing(&self.dir.join(f));
                }
            }
            removed += batch.len();
            chunks += 1;
            after_chunk(chunks);
            if !pause.is_zero() {
                std::thread::sleep(pause);
            }
        }
    }

    /// The design for a later `check()`'s orphan pass, run here only to count. Lists `.val` and `.tmp`
    /// files with no lock held, then confirms each `.val` is unreferenced while holding the write lock.
    /// Returns (orphan .val files, .tmp files).
    pub fn find_orphans(&self) -> Result<(Vec<PathBuf>, Vec<PathBuf>)> {
        let (mut vals, mut tmps) = (Vec::new(), Vec::new());
        for a in read_dirs(&self.dir)? {
            for b in read_dirs(&a)? {
                for f in fs::read_dir(&b)? {
                    let p = f?.path();
                    match p.extension().and_then(|e| e.to_str()) {
                        Some("val") => vals.push(p),
                        Some("tmp") => tmps.push(p),
                        _ => {}
                    }
                }
            }
        }
        self.conn.execute_batch("BEGIN IMMEDIATE")?;
        let mut orphans = Vec::new();
        {
            let mut stmt = self.conn.prepare_cached("SELECT 1 FROM cache WHERE filename = ?1")?;
            for p in vals {
                let rel = p.strip_prefix(&self.dir)?.to_string_lossy().into_owned();
                if !stmt.exists([&rel])? && p.exists() {
                    orphans.push(p);
                }
            }
        }
        self.conn.execute_batch("COMMIT")?;
        Ok((orphans, tmps))
    }

    pub fn spill_file_count(&self) -> Result<usize> {
        let mut n = 0;
        for a in read_dirs(&self.dir)? {
            for b in read_dirs(&a)? {
                n += fs::read_dir(&b)?.filter(|e| e.as_ref().is_ok_and(|e| e.path().extension().is_some_and(|x| x == "val"))).count();
            }
        }
        Ok(n)
    }
}

fn read_dirs(p: &Path) -> Result<Vec<PathBuf>> {
    let mut out = Vec::new();
    for e in fs::read_dir(p)? {
        let e = e?;
        if e.file_type()?.is_dir() {
            out.push(e.path());
        }
    }
    Ok(out)
}

pub fn is_busy(e: &rusqlite::Error) -> bool {
    matches!(e.sqlite_error_code(), Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked))
}

fn remove_ignoring_missing(p: &Path) {
    match fs::remove_file(p) {
        Ok(()) => {}
        Err(e) if e.kind() == ErrorKind::NotFound => {}
        Err(e) => eprintln!("warning: could not remove {}: {e}", p.display()),
    }
}

pub fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

pub fn nanos() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_nanos() as u64)
}

/// xorshift64*: good enough for test data.
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        Self(seed | 1)
    }
    pub fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    pub fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
    pub fn fill(&mut self, buf: &mut [u8]) {
        for chunk in buf.chunks_mut(8) {
            let v = self.next().to_le_bytes();
            chunk.copy_from_slice(&v[..chunk.len()]);
        }
    }
}

pub fn fnv64(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in data {
        h ^= u64::from(*b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

/// A self-verifying blob: `key|n|` + random bytes (at least `min_size` in total) + FNV-64 of the rest.
pub fn make_blob(key: &str, n: u64, rng: &mut Rng, min_size: usize, spread: usize) -> Vec<u8> {
    let mut body = format!("{key}|{n}|").into_bytes();
    let len = min_size + rng.below(spread as u64 + 1) as usize;
    let start = body.len();
    body.resize(start.max(len - 8), 0);
    rng.fill(&mut body[start..]);
    let h = fnv64(&body);
    body.extend_from_slice(&h.to_le_bytes());
    body
}

pub fn blob_ok(key: &str, blob: &[u8], min_size: usize) -> bool {
    if blob.len() < min_size || blob.len() < 8 {
        return false;
    }
    let (body, digest) = blob.split_at(blob.len() - 8);
    body.starts_with(format!("{key}|").as_bytes()) && fnv64(body).to_le_bytes() == digest
}

/// Simple `--name value` argument lookup for the spike binaries.
pub struct Args(Vec<String>);

impl Args {
    pub fn from_env() -> Self {
        Self(std::env::args().skip(1).collect())
    }
    pub fn first(&self) -> &str {
        self.0.first().map_or("", String::as_str)
    }
    pub fn get(&self, name: &str) -> Option<&str> {
        self.0.iter().position(|a| a == name).and_then(|i| self.0.get(i + 1)).map(String::as_str)
    }
    pub fn num<T: std::str::FromStr>(&self, name: &str, default: T) -> T {
        self.get(name).and_then(|v| v.parse().ok()).unwrap_or(default)
    }
    pub fn flag(&self, name: &str) -> bool {
        self.0.iter().any(|a| a == name)
    }
    pub fn raw(&self) -> &[String] {
        &self.0
    }
}

/// Waits (polling) until `path` exists.
pub fn wait_for(path: &Path, limit: Duration) -> bool {
    let start = Instant::now();
    while !path.exists() {
        if start.elapsed() > limit {
            return false;
        }
        std::thread::sleep(Duration::from_micros(200));
    }
    true
}

pub fn touch(path: &Path) {
    let _ = fs::write(path, b"");
}

pub fn fresh_dir(path: &Path) {
    let _ = fs::remove_dir_all(path);
    fs::create_dir_all(path).expect("create run dir");
}
