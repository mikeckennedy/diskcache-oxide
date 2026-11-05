//! `expire()`, `clear()`, `cull()` and key iteration (Phase 2 step 6; 04-design.md sections 6 and 9).
//!
//! Maintenance works in chunks of [`CHUNK`] rows, each its own write transaction, bounded by a
//! `store_seq` snapshot taken when the call starts. So a call always finishes (D-011): rows stored
//! after it started are never part of it, and writes made meanwhile survive. Between chunks it
//! unlinks the chunk's value files, pauses briefly with the interpreter released, and yields to
//! any same-process writer waiting on the write lock, so writers get in during a long `clear()`.
//!
//! Each call comes in two forms. The plain one (`clear(timeout)`, a `Cache` call) gives every
//! wait the full `timeout`. The `_until` one (`clear_until(snapshot, deadline)`, a `FanoutCache` call
//! over every shard) gives every wait only what's left before one `deadline` the caller took for
//! the whole call, and reports how much it removed when it stops early ([`Interrupted`]). The
//! deadline bounds waiting, not work: a chunk already running finishes, and past the deadline each
//! chunk still gets one attempt with no wait, so an idle store is still emptied at a short timeout.
//! `clear` and `expire` are bounded by their snapshot as well, so they always finish. `cull`'s
//! eviction loop isn't: it runs until the store is under its `size_limit`, so past the deadline a
//! writer in another process that keeps the store over its limit can keep `cull_until` going (as it
//! can keep `cull` going).
//!
//! Iteration is keyset-paged: each page is one short read that returns its connection, so no
//! cursor or read transaction is held across user code (D-017), and deleting keys while iterating
//! is safe. It isn't a snapshot (concurrent writes may or may not be seen, as with diskcache) and
//! it includes expired-but-unswept rows.

use std::time::{Duration, Instant};

use rusqlite::Connection;

use crate::gate;
use crate::rows::{Removed, RowWriter};
use crate::schema::KEY_TYPE_STR;
use crate::store::{Changed, LockPriority, Store};
use crate::{Error, Result};

/// Rows per maintenance transaction.
pub const CHUNK: i64 = 100;

/// Keys per iteration page.
pub const PAGE: i64 = 512;

/// The pause after a maintenance chunk while a same-process writer waits, with the interpreter
/// released.
const PAUSE: Duration = Duration::from_micros(100);

/// The longest a chunk loop yields to a waiting same-process writer before carrying on.
const MAX_YIELD: Duration = Duration::from_millis(2);

/// The order a key iteration walks in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Order {
    /// Key order (BINARY over UTF-8, which is code-point order, like Python's `sorted()`).
    Key { reverse: bool },
    /// Insertion order (`id`), which `__iter__` follows. An overwrite keeps its place.
    Inserted,
}

/// Where an iteration stopped: the last key returned (key order) or the last `id` (insertion order).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum Cursor {
    #[default]
    Start,
    Key(String),
    Id(i64),
}

/// The smallest string greater than every string that starts with `prefix`, or `None` when there's
/// no such bound (an empty prefix, or one made only of U+10FFFF): increment the last code point,
/// skipping the surrogate range, and carry past U+10FFFF by dropping it.
#[must_use]
pub fn prefix_upper(prefix: &str) -> Option<String> {
    let mut chars: Vec<char> = prefix.chars().collect();
    while let Some(last) = chars.pop() {
        let next = match last {
            '\u{D7FF}' => Some('\u{E000}'), // Skip the surrogate range, which `char` can't hold.
            char::MAX => None,              // U+10FFFF: carry into the previous character.
            c => char::from_u32(u32::from(c) + 1),
        };
        if let Some(next) = next {
            chars.push(next);
            return Some(chars.into_iter().collect());
        }
    }
    None
}

/// How long each wait in a maintenance call may take.
#[derive(Debug, Clone, Copy)]
enum Budget {
    /// The full `timeout` for every wait: a `Cache` call.
    Each(Duration),
    /// Whatever is left before the deadline, and nothing once it has passed: one call over every
    /// shard of a `FanoutCache`.
    Until(Instant),
}

impl Budget {
    /// What the next wait may take.
    fn left(self) -> Duration {
        match self {
            Self::Each(timeout) => timeout,
            Self::Until(deadline) => deadline.saturating_duration_since(Instant::now()),
        }
    }
}

/// The point a maintenance call starts from (D-011): every row stored before it has a smaller
/// `store_seq`, so a call bounded by it always finishes and never removes a later write. Taken by
/// [`Store::snapshot_until`], and only for the store that took it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[must_use]
pub struct Snapshot {
    seq: i64,
    /// The address of the store that took it, to refuse a snapshot passed to another shard's store,
    /// whose later writes it could delete. Both are alive for the call, so the address can't have
    /// been reused.
    store: usize,
}

/// A maintenance call under a deadline that stopped early: it removed `removed` rows, each chunk
/// committed, before `error`.
#[derive(Debug)]
pub struct Interrupted {
    pub removed: u64,
    pub error: Error,
}

impl Store {
    /// Removes every row that expired before `now`, in chunks. Returns how many it removed.
    pub fn expire(&self, timeout: Duration, now: f64) -> Result<u64> {
        let snapshot = self.seq_snapshot(timeout)?;
        self.expire_from(snapshot, now, Budget::Each(timeout)).map_err(|stopped| stopped.error)
    }

    /// Removes every row stored before the call started, in chunks. Returns how many it removed.
    pub fn clear(&self, timeout: Duration) -> Result<u64> {
        let snapshot = self.seq_snapshot(timeout)?;
        self.clear_from(snapshot, Budget::Each(timeout)).map_err(|stopped| stopped.error)
    }

    /// Removes expired rows, then evicts least-recently-stored rows until the cache is under its
    /// `size_limit`. Returns how many it removed in total.
    pub fn cull(&self, timeout: Duration, now: f64) -> Result<u64> {
        let snapshot = self.seq_snapshot(timeout)?;
        self.cull_from(snapshot, now, Budget::Each(timeout)).map_err(|stopped| stopped.error)
    }

    /// The starting point for [`Store::clear_until`], [`Store::expire_until`] and
    /// [`Store::cull_until`] on this store: one short read, waiting no later than `deadline` (one
    /// attempt with no wait once it has passed). A `FanoutCache` takes every shard's before it removes
    /// anything, so a call removes only what every shard held when it started.
    pub fn snapshot_until(&self, deadline: Instant) -> Result<Snapshot> {
        let seq = self.seq_snapshot(Budget::Until(deadline).left())?;
        Ok(Snapshot { seq, store: self.address() })
    }

    /// [`Store::expire`] from `snapshot`, every wait ending by `deadline` (see the module docs).
    pub fn expire_until(&self, snapshot: Snapshot, now: f64, deadline: Instant) -> Result<u64, Interrupted> {
        self.expire_from(self.seq_of(snapshot)?, now, Budget::Until(deadline))
    }

    /// [`Store::clear`] from `snapshot`, every wait ending by `deadline` (see the module docs).
    pub fn clear_until(&self, snapshot: Snapshot, deadline: Instant) -> Result<u64, Interrupted> {
        self.clear_from(self.seq_of(snapshot)?, Budget::Until(deadline))
    }

    /// [`Store::cull`] from `snapshot` (its expire step), every wait ending by `deadline` (see the
    /// module docs).
    pub fn cull_until(&self, snapshot: Snapshot, now: f64, deadline: Instant) -> Result<u64, Interrupted> {
        self.cull_from(self.seq_of(snapshot)?, now, Budget::Until(deadline))
    }

    fn address(&self) -> usize {
        std::ptr::from_ref(self).addr()
    }

    /// `snapshot`'s `store_seq`, or an error, before anything is removed, if another store took it.
    fn seq_of(&self, snapshot: Snapshot) -> Result<i64, Interrupted> {
        if snapshot.store == self.address() {
            Ok(snapshot.seq)
        } else {
            let reason = "a maintenance snapshot was passed to a store other than the one that took it";
            Err(Interrupted { removed: 0, error: Error::InvalidSetting(reason.to_string()) })
        }
    }

    fn expire_from(&self, snapshot: i64, now: f64, budget: Budget) -> Result<u64, Interrupted> {
        self.chunked(budget, |conn, timeout| {
            conn.prepare_cached(
                "DELETE FROM cache WHERE id IN (
                     SELECT id FROM cache
                     WHERE expire_time IS NOT NULL AND expire_time < ?1 AND store_seq < ?2
                     ORDER BY expire_time LIMIT ?3)
                 RETURNING size, filename",
            )
            .and_then(|mut stmt| collect_removed(stmt.query_map((now, snapshot, CHUNK), row_removed)?))
            .map_err(|e| Error::from_sqlite(e, timeout))
        })
    }

    fn clear_from(&self, snapshot: i64, budget: Budget) -> Result<u64, Interrupted> {
        self.chunked(budget, |conn, timeout| {
            conn.prepare_cached(
                "DELETE FROM cache WHERE id IN (
                     SELECT id FROM cache WHERE store_seq < ?1 ORDER BY store_seq LIMIT ?2)
                 RETURNING size, filename",
            )
            .and_then(|mut stmt| collect_removed(stmt.query_map((snapshot, CHUNK), row_removed)?))
            .map_err(|e| Error::from_sqlite(e, timeout))
        })
    }

    fn cull_from(&self, snapshot: i64, now: f64, budget: Budget) -> Result<u64, Interrupted> {
        let mut removed = self.expire_from(snapshot, now, budget)?;
        loop {
            let timeout = budget.left();
            let (over, batch) = self
                .write_files(timeout, LockPriority::Normal, |writer, conn| {
                    let (size_limit, size): (i64, i64) = conn
                        .prepare_cached("SELECT size_limit, size FROM totals WHERE only = 1")
                        .and_then(|mut stmt| stmt.query_row([], |r| Ok((r.get(0)?, r.get(1)?))))
                        .map_err(|e| Error::from_sqlite(e, timeout))?;
                    if crate::ops::volume_in(conn, size, timeout)? < size_limit {
                        return Ok(Changed::removing((false, 0), []));
                    }
                    let batch = writer.delete_oldest(CHUNK)?;
                    Ok(Changed::removing((true, count(batch.len())), batch))
                })
                .map_err(|error| Interrupted { removed, error })?;
            let done = !over || batch == 0;
            removed += batch;
            if done {
                return Ok(removed);
            }
        }
    }

    /// The next page of keys after `cursor`, at most [`PAGE`]. An empty page means the end.
    pub fn keys_page(
        &self,
        timeout: Duration,
        order: Order,
        prefix: Option<&str>,
        cursor: &Cursor,
    ) -> Result<(Vec<String>, Cursor)> {
        self.keys_page_of(PAGE, timeout, order, prefix, cursor)
    }

    /// [`Store::keys_page`] with pages of at most `size` keys (at least 1), for a `FanoutCache`
    /// merge, which keeps one page per shard buffered at once.
    pub fn keys_page_of(
        &self,
        size: i64,
        timeout: Duration,
        order: Order,
        prefix: Option<&str>,
        cursor: &Cursor,
    ) -> Result<(Vec<String>, Cursor)> {
        let size = size.max(1);
        let upper = prefix.and_then(prefix_upper);
        let lower = prefix.unwrap_or("");
        self.read(timeout, |conn| page(conn, timeout, order, lower, upper.as_deref(), cursor, size))
    }

    /// The next `store_seq` to be handed out: every row stored before now has a smaller one.
    pub(crate) fn seq_snapshot(&self, timeout: Duration) -> Result<i64> {
        self.read(timeout, |conn| {
            conn.prepare_cached(
                "SELECT MAX(COALESCE((SELECT MAX(store_seq) FROM cache), 0) + 1, next_seq) FROM totals WHERE only = 1",
            )
            .and_then(|mut stmt| stmt.query_row([], |r| r.get(0)))
            .map_err(|e| Error::from_sqlite(e, timeout))
        })
    }

    /// Runs `delete_chunk` in its own write transaction until it removes nothing, fixing up the
    /// totals for each chunk, and stepping aside between chunks while a same-process writer waits.
    /// Returns the total removed.
    ///
    /// Each chunk waits what `budget` has left when it starts; `delete_chunk` gets that too, for
    /// the number a timeout reports. Stopped by an error, it says how many the committed chunks
    /// removed.
    fn chunked(
        &self,
        budget: Budget,
        mut delete_chunk: impl FnMut(&Connection, Duration) -> Result<Vec<(i64, Option<String>)>>,
    ) -> Result<u64, Interrupted> {
        let mut total = 0;
        let mut yielded = false;
        loop {
            let timeout = budget.left();
            let chunk = |writer: &RowWriter<'_>, conn: &Connection| -> Result<Changed<u64>> {
                writer.raise_seq_floor()?;
                let rows = delete_chunk(conn, timeout)?;
                let size: i64 = rows.iter().map(|(size, _)| size).sum();
                writer.adjust_totals_after_delete(i64::try_from(rows.len()).unwrap_or(i64::MAX), size)?;
                let removed = count(rows.len());
                Ok(Changed::removing(removed, rows.into_iter().map(|(_, filename)| Removed { filename })))
            };
            // After stepping aside between chunks, take the lock back with priority.
            let priority = if yielded { LockPriority::AfterYield } else { LockPriority::Normal };
            let batch =
                self.write_files(timeout, priority, chunk).map_err(|error| Interrupted { removed: total, error })?;
            if batch == 0 {
                return Ok(total);
            }
            total += batch;
            yielded = self.pause_between_chunks();
        }
    }

    /// The breather between maintenance chunks, only while a same-process writer waits on the write
    /// lock: a short pause, then a yield to it (up to [`MAX_YIELD`]), with the interpreter released.
    ///
    /// With nobody waiting it does nothing (Michael, 2026-10-29). Measured on `clear()` of 100k
    /// rows: an unconditional pause made it 2.2x slower idle and 8x slower beside a writer in
    /// another process, whose worst wait it only trimmed from about 4 to 2.3 ms. That writer now
    /// gets in only in the short gap between one chunk's `COMMIT` and the next `BEGIN`, through
    /// SQLite's busy wait: usually, not by guarantee, so a slow disk could stretch its tail.
    /// Without the yield, though, a same-process writer waited out the whole `clear()`
    /// (127-140 ms). plans/06 has the table. Returns whether it stepped aside, so the next chunk
    /// takes the lock back with priority only then.
    fn pause_between_chunks(&self) -> bool {
        // Inside a held transaction this thread keeps the write lock, so a waiter can't get in
        // however long it pauses, and inside any other outer operation the next chunk doesn't wait
        // for the lock anyway. Either way a pause would only hold up a pending fork.
        if self.write_waiters() == 0 || gate::GATE.own_depth() > 0 {
            return false;
        }
        gate::run_blocking(&|| {
            std::thread::sleep(PAUSE);
            let start = Instant::now();
            while self.write_waiters() > 0 && start.elapsed() < MAX_YIELD {
                std::thread::sleep(Duration::from_micros(50));
            }
        });
        true
    }
}

/// A row count as the `u64` the maintenance calls return.
fn count(rows: usize) -> u64 {
    u64::try_from(rows).unwrap_or(u64::MAX)
}

fn row_removed(row: &rusqlite::Row<'_>) -> rusqlite::Result<(i64, Option<String>)> {
    Ok((row.get(0)?, row.get(1)?))
}

fn collect_removed(
    rows: impl Iterator<Item = rusqlite::Result<(i64, Option<String>)>>,
) -> rusqlite::Result<Vec<(i64, Option<String>)>> {
    rows.collect()
}

/// The statement for one page in key order. Every one binds `?1` the key type, `?2` the lower bound,
/// `?3` the upper bound and `?4` the page size. There's one per shape of the range, and no statement
/// has an optional condition: `?3 IS NULL OR key < ?3` can't be used to seek or to stop, so each page
/// would scan from the prefix's start, or, past the prefix's end, to the end of the table. That made a
/// walk quadratic. The soak's end-of-run check of 4.5M rows took about 5 minutes.
fn key_page_sql(reverse: bool, after_cursor: bool, has_upper: bool) -> &'static str {
    match (reverse, after_cursor, has_upper) {
        (false, false, false) => "SELECT key FROM cache WHERE key_type = ?1 AND key >= ?2 ORDER BY key LIMIT ?4",
        (false, false, true) => {
            "SELECT key FROM cache WHERE key_type = ?1 AND key >= ?2 AND key < ?3 ORDER BY key LIMIT ?4"
        }
        (false, true, false) => "SELECT key FROM cache WHERE key_type = ?1 AND key > ?2 ORDER BY key LIMIT ?4",
        (false, true, true) => {
            "SELECT key FROM cache WHERE key_type = ?1 AND key > ?2 AND key < ?3 ORDER BY key LIMIT ?4"
        }
        // A reverse page's top is the cursor or the prefix's end, whichever applies, so only whether
        // there is one matters.
        (true, false, false) => "SELECT key FROM cache WHERE key_type = ?1 AND key >= ?2 ORDER BY key DESC LIMIT ?4",
        (true, _, _) => {
            "SELECT key FROM cache WHERE key_type = ?1 AND key >= ?2 AND key < ?3 ORDER BY key DESC LIMIT ?4"
        }
    }
}

fn page(
    conn: &Connection,
    timeout: Duration,
    order: Order,
    lower: &str,
    upper: Option<&str>,
    cursor: &Cursor,
    size: i64,
) -> Result<(Vec<String>, Cursor)> {
    let sqlite = |e| Error::from_sqlite(e, timeout);
    match order {
        Order::Inserted => {
            let after = match cursor {
                Cursor::Id(id) => *id,
                _ => 0,
            };
            let rows: Vec<(i64, String)> = conn
                .prepare_cached("SELECT id, key FROM cache WHERE key_type = ?1 AND id > ?2 ORDER BY id LIMIT ?3")
                .and_then(|mut stmt| {
                    stmt.query_map((KEY_TYPE_STR, after, size), |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
                })
                .map_err(sqlite)?;
            let next = rows.last().map_or_else(|| cursor.clone(), |(id, _)| Cursor::Id(*id));
            Ok((rows.into_iter().map(|(_, key)| key).collect(), next))
        }
        Order::Key { reverse } => {
            let after = match cursor {
                Cursor::Key(key) => Some(key.as_str()),
                _ => None,
            };
            // Forward, the cursor replaces the lower bound. In reverse, it replaces the upper one: it
            // came from this prefix, so it's under the prefix's end.
            let (low, high) = match (reverse, after) {
                (false, after) => (after.unwrap_or(lower), upper),
                (true, after) => (lower, after.or(upper)),
            };
            let keys: Vec<String> = conn
                .prepare_cached(key_page_sql(reverse, after.is_some(), upper.is_some()))
                .and_then(|mut stmt| stmt.query_map((KEY_TYPE_STR, low, high, size), |r| r.get(0))?.collect())
                .map_err(sqlite)?;
            let next = keys.last().map_or_else(|| cursor.clone(), |k| Cursor::Key(k.clone()));
            Ok((keys, next))
        }
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::sync::Arc;

    use super::*;
    use crate::open::Requested;
    use crate::ops::Value;
    use crate::schema::MODE_STR;
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
        })
        .unwrap()
    }

    fn put(s: &Store, key: &str, expire: Option<f64>) {
        s.set(T, key, Value::Bytes { mode: MODE_STR, data: b"v" }, expire, NOW).unwrap();
    }

    fn all(s: &Store, order: Order, prefix: Option<&str>) -> Vec<String> {
        let mut cursor = Cursor::Start;
        let mut out = Vec::new();
        loop {
            let (keys, next) = s.keys_page(T, order, prefix, &cursor).unwrap();
            if keys.is_empty() {
                return out;
            }
            out.extend(keys);
            cursor = next;
        }
    }

    /// Every shape `page` can ask for, as (reverse, after a cursor, has an upper bound, what the
    /// index search must include).
    const KEY_PAGE_SHAPES: [(bool, bool, bool, &str); 8] = [
        (false, false, false, "key_type=? AND key>?"),
        (false, false, true, "key_type=? AND key>? AND key<?"),
        (false, true, false, "key_type=? AND key>?"),
        (false, true, true, "key_type=? AND key>? AND key<?"),
        (true, false, false, "key_type=? AND key>?"),
        (true, false, true, "key_type=? AND key>? AND key<?"),
        (true, true, false, "key_type=? AND key>? AND key<?"),
        (true, true, true, "key_type=? AND key>? AND key<?"),
    ];

    #[test]
    fn every_key_page_statement_seeks_on_both_of_its_bounds() {
        let tmp = TempDir::new("maint-page-plan");
        let s = store(tmp.path());
        s.read(T, |conn| {
            for (reverse, after, upper, search) in KEY_PAGE_SHAPES {
                let plan = format!("EXPLAIN QUERY PLAN {}", key_page_sql(reverse, after, upper));
                let detail: String = conn
                    .prepare(&plan)
                    .and_then(|mut stmt| {
                        let params = (KEY_TYPE_STR, "a", "b", PAGE);
                        stmt.query_row(params, |r| r.get(3))
                    })
                    .unwrap();
                assert!(detail.contains(&format!("cache_key ({search})")), "{plan}: {detail}");
                assert!(detail.starts_with("SEARCH"), "{plan}: {detail}");
            }
            Ok(())
        })
        .unwrap();
    }

    /// The VM steps for one page, which unlike a time can't flake. With `rows` keys in the store, a
    /// page that scanned from the start of the range would take thousands.
    fn page_steps(conn: &Connection, sql: &str, low: &str, high: Option<&str>) -> (Vec<String>, i32) {
        let mut stmt = conn.prepare(sql).unwrap();
        let keys: Vec<String> = stmt
            .query_map((KEY_TYPE_STR, low, high, PAGE), |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        (keys, stmt.get_status(rusqlite::StatementStatus::VmStep))
    }

    #[test]
    fn a_key_page_costs_the_same_at_every_depth_in_both_directions() {
        const ROWS: usize = 6_000;
        let tmp = TempDir::new("maint-page-cost");
        let s = store(tmp.path());
        // A prefix in the middle of the table, with many keys on each side of it, so that one that
        // scans past either end of its range costs as much as one that scans from its start.
        for group in ["a", "m", "z"] {
            for i in 0..ROWS {
                put(&s, &format!("{group}:{i:06}"), None);
            }
        }
        let budget = i32::try_from(PAGE).unwrap() * 12 + 100;
        s.read(T, |conn| {
            for (prefix, upper, expect) in [("", None, 3 * ROWS), ("m:", Some("m;"), ROWS)] {
                for reverse in [false, true] {
                    let mut after: Option<String> = None;
                    let (mut seen, mut pages, mut worst) = (0, 0, 0);
                    loop {
                        let (low, high) = if reverse {
                            (prefix, after.as_deref().or(upper))
                        } else {
                            (after.as_deref().unwrap_or(prefix), upper)
                        };
                        let sql = key_page_sql(reverse, after.is_some(), upper.is_some());
                        let (keys, steps) = page_steps(conn, sql, low, high);
                        worst = worst.max(steps);
                        assert!(steps <= budget, "prefix {prefix:?} reverse {reverse}, page {pages}: {steps} steps");
                        let Some(last) = keys.last() else { break };
                        after = Some(last.clone());
                        seen += keys.len();
                        pages += 1;
                    }
                    assert_eq!(seen, expect, "prefix {prefix:?} reverse {reverse}, worst page {worst} steps");
                }
            }
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn key_pages_walk_exactly_the_keys_a_sort_gives() {
        let tmp = TempDir::new("maint-page-model");
        let s = store(tmp.path());
        let mut keys: Vec<String> = vec![
            String::new(),
            "a".to_string(),
            "a\u{0}".to_string(),
            "a\u{D7FF}".to_string(),
            "a\u{E000}".to_string(),
            "a\u{10FFFF}".to_string(),
            "a\u{10FFFF}x".to_string(),
            "b".to_string(),
            "\u{10FFFF}".to_string(),
            "\u{10FFFF}\u{10FFFF}".to_string(),
        ];
        // More than two pages under one prefix, so the cursor and the prefix's end apply together.
        keys.extend((0..PAGE * 2 + 7).map(|i| format!("ab{i:05}")));
        keys.extend((0..PAGE + 3).map(|i| format!("ac{i:05}")));
        for key in &keys {
            put(&s, key, None);
        }
        keys.sort();
        for prefix in
            [None, Some(""), Some("a"), Some("ab"), Some("ac"), Some("a\u{10FFFF}"), Some("\u{10FFFF}"), Some("q")]
        {
            let mut want: Vec<String> = keys.iter().filter(|k| k.starts_with(prefix.unwrap_or(""))).cloned().collect();
            assert_eq!(all(&s, Order::Key { reverse: false }, prefix), want, "prefix {prefix:?}");
            want.reverse();
            assert_eq!(all(&s, Order::Key { reverse: true }, prefix), want, "prefix {prefix:?} reverse");
        }
    }

    #[test]
    fn small_pages_walk_the_same_keys_as_full_ones() {
        let tmp = TempDir::new("maint-page-small");
        let s = store(tmp.path());
        for i in 0..300 {
            put(&s, &format!("k{i:03}"), None);
        }
        for (order, prefix) in
            [(Order::Key { reverse: false }, None), (Order::Key { reverse: true }, Some("k1")), (Order::Inserted, None)]
        {
            let mut cursor = Cursor::Start;
            let mut small = Vec::new();
            loop {
                let (keys, next) = s.keys_page_of(7, T, order, prefix, &cursor).unwrap();
                assert!(keys.len() <= 7);
                if keys.is_empty() {
                    break;
                }
                small.extend(keys);
                cursor = next;
            }
            assert_eq!(small, all(&s, order, prefix), "{order:?} {prefix:?}");
        }
    }

    #[test]
    fn a_key_walk_sees_changes_ahead_of_its_cursor_and_not_behind_it() {
        let tmp = TempDir::new("maint-page-changes");
        let s = store(tmp.path());
        for i in 0..PAGE * 2 {
            put(&s, &format!("k{i:05}"), None);
        }
        for (reverse, ahead, behind, gone) in [(false, "k90000", "k", "k01000"), (true, "k", "k90000", "k00000")] {
            let order = Order::Key { reverse };
            let (first, cursor) = s.keys_page(T, order, Some("k"), &Cursor::Start).unwrap();
            assert_eq!(i64::try_from(first.len()), Ok(PAGE));
            put(&s, ahead, None);
            put(&s, behind, None);
            s.delete(T, gone, NOW).unwrap();
            let mut rest = Vec::new();
            let mut cursor = cursor;
            loop {
                let (keys, next) = s.keys_page(T, order, Some("k"), &cursor).unwrap();
                if keys.is_empty() {
                    break;
                }
                rest.extend(keys);
                cursor = next;
            }
            assert!(rest.iter().any(|k| k == ahead), "reverse {reverse}: a key ahead of the cursor is seen");
            assert!(rest.iter().all(|k| k != behind), "reverse {reverse}: a key behind the cursor isn't");
            assert!(rest.iter().all(|k| k != gone), "reverse {reverse}: a deleted key isn't");
            s.delete(T, ahead, NOW).unwrap();
            s.delete(T, behind, NOW).unwrap();
            put(&s, gone, None);
        }
    }

    #[test]
    fn prefix_upper_covers_the_edge_cases() {
        assert_eq!(prefix_upper("ab").as_deref(), Some("ac"));
        assert_eq!(prefix_upper("a\u{D7FF}").as_deref(), Some("a\u{E000}"));
        assert_eq!(prefix_upper("a\u{10FFFF}").as_deref(), Some("b"));
        assert_eq!(prefix_upper("\u{10FFFF}\u{10FFFF}"), None);
        assert_eq!(prefix_upper(""), None);
    }

    #[test]
    fn expire_removes_every_expired_row_even_with_one_shared_expire_time() {
        let tmp = TempDir::new("maint-expire");
        let s = store(tmp.path());
        for i in 0..250 {
            put(&s, &format!("e{i}"), Some(10.0)); // All share NOW + 10 (D-010).
        }
        put(&s, "live", None);
        assert_eq!(s.expire(T, NOW + 11.0).unwrap(), 250);
        assert_eq!(s.len(T).unwrap(), 1);
    }

    #[test]
    fn clear_returns_the_count_and_spares_writes_made_after_it_started() {
        let tmp = TempDir::new("maint-clear");
        let s = store(tmp.path());
        for i in 0..350 {
            put(&s, &format!("k{i}"), if i % 2 == 0 { Some(1.0) } else { None });
        }
        let snapshot = s.seq_snapshot(T).unwrap();
        put(&s, "after", None); // Stored after the snapshot.
        assert_eq!(snapshot, 351, "350 stores took seqs 1..=350");
        assert_eq!(s.clear(T).unwrap(), 351, "clear's own snapshot includes the later write here");
        put(&s, "late", None);
        assert_eq!(s.len(T).unwrap(), 1);
    }

    #[test]
    fn key_order_and_prefix_scan() {
        let tmp = TempDir::new("maint-keys");
        let s = store(tmp.path());
        for key in ["b", "a", "aa", "ab", "a\u{10FFFF}", "ac", "c", "10", "2", ""] {
            put(&s, key, None);
        }
        let mut sorted = vec!["", "10", "2", "a", "aa", "ab", "ac", "a\u{10FFFF}", "b", "c"];
        assert_eq!(all(&s, Order::Key { reverse: false }, None), sorted);
        sorted.reverse();
        assert_eq!(all(&s, Order::Key { reverse: true }, None), sorted);
        assert_eq!(all(&s, Order::Key { reverse: false }, Some("a")), ["a", "aa", "ab", "ac", "a\u{10FFFF}"]);
        assert_eq!(all(&s, Order::Key { reverse: true }, Some("a")), ["a\u{10FFFF}", "ac", "ab", "aa", "a"]);
        assert_eq!(all(&s, Order::Key { reverse: false }, Some("zz")), Vec::<String>::new());
    }

    #[test]
    fn insertion_order_keeps_an_overwrite_in_place_and_pages_through_many_keys() {
        let tmp = TempDir::new("maint-iter");
        let s = store(tmp.path());
        let keys: Vec<String> = (0..1_200).map(|i| format!("k{i:04}")).collect();
        for key in &keys {
            put(&s, key, None);
        }
        put(&s, "k0000", None); // Overwrite: keeps its insertion position.
        assert_eq!(all(&s, Order::Inserted, None), keys);
    }

    #[test]
    fn past_its_deadline_a_maintenance_call_still_empties_an_idle_store() {
        // Past the deadline nothing waits, not even for a pending fork: keep the other tests' forks
        // on the global gate out of the way, or one of these no-wait attempts can meet one.
        let _forks = crate::testutil::one_global_fork_at_a_time();
        let tmp = TempDir::new("maint-until-idle");
        let s = store(tmp.path());
        for i in 0..350 {
            put(&s, &format!("k{i}"), if i < 120 { Some(1.0) } else { None });
        }
        let past = Instant::now();
        let snapshot = s.snapshot_until(past).unwrap();
        assert_eq!(s.expire_until(snapshot, NOW + 2.0, past).unwrap(), 120, "every chunk, each with no wait");
        assert_eq!(s.cull_until(snapshot, NOW + 2.0, past).unwrap(), 0, "under its size_limit");
        assert_eq!(s.clear_until(snapshot, past).unwrap(), 230);
        assert_eq!(s.len(T).unwrap(), 0);
        assert!(s.checkpoint_until(past).unwrap(), "one round with no wait resets an idle WAL");
    }

    #[test]
    fn a_snapshot_taken_before_a_write_spares_it() {
        let tmp = TempDir::new("maint-until-snapshot");
        let s = store(tmp.path());
        for i in 0..250 {
            put(&s, &format!("k{i}"), Some(1.0));
        }
        let snapshot = s.snapshot_until(Instant::now() + T).unwrap();
        put(&s, "after", Some(1.0)); // Expired too, but stored after the snapshot.
        put(&s, "k0", None); // An overwrite after it is a new version: spared as well.
        let deadline = Instant::now() + T;
        assert_eq!(s.expire_until(snapshot, NOW + 2.0, deadline).unwrap(), 249);
        assert_eq!(s.clear_until(snapshot, deadline).unwrap(), 0);
        assert_eq!(all(&s, Order::Key { reverse: false }, None), ["after", "k0"]);
    }

    #[test]
    fn a_snapshot_from_another_store_is_refused_before_anything_is_removed() {
        let (one, two) = (TempDir::new("maint-until-wrong-a"), TempDir::new("maint-until-wrong-b"));
        let (a, b) = (store(one.path()), store(two.path()));
        put(&a, "a", Some(1.0));
        put(&b, "b", Some(1.0));
        let deadline = Instant::now() + T;
        let from_a = a.snapshot_until(deadline).unwrap();
        for stopped in [
            b.clear_until(from_a, deadline).unwrap_err(),
            b.expire_until(from_a, NOW + 2.0, deadline).unwrap_err(),
            b.cull_until(from_a, NOW + 2.0, deadline).unwrap_err(),
        ] {
            assert_eq!(stopped.removed, 0);
            assert!(matches!(stopped.error, Error::InvalidSetting(_)), "{:?}", stopped.error);
        }
        assert_eq!(b.len(T).unwrap(), 1);
    }

    #[test]
    fn behind_another_threads_write_lock_the_deadline_bounds_the_wait_and_nothing_is_removed() {
        let tmp = TempDir::new("maint-until-held");
        let s = store(tmp.path());
        for i in 0..50 {
            put(&s, &format!("k{i}"), None);
        }
        let snapshot = s.snapshot_until(Instant::now() + T).unwrap();
        let held = s.begin_hold(T).unwrap();
        std::thread::scope(|scope| {
            let worker = scope.spawn(|| {
                let start = Instant::now();
                let stopped = s.clear_until(snapshot, start + Duration::from_millis(100)).unwrap_err();
                (start.elapsed(), stopped)
            });
            let (took, stopped) = worker.join().unwrap();
            assert!(matches!(stopped.error, Error::Timeout { .. }), "{:?}", stopped.error);
            assert_eq!(stopped.removed, 0);
            assert!(took >= Duration::from_millis(100), "gave up after {took:?}, before its deadline");
            assert!(took < Duration::from_secs(2), "waited {took:?}, past its deadline");
        });
        held.finish(false).unwrap();
        assert_eq!(s.len(T).unwrap(), 50);
    }

    #[test]
    fn a_chunked_call_stopped_by_an_error_reports_what_its_committed_chunks_removed() {
        let tmp = TempDir::new("maint-until-partial");
        let s = store(tmp.path());
        for i in 0..350 {
            put(&s, &format!("k{i}"), None);
        }
        let mut calls = 0;
        let stopped = s
            .chunked(Budget::Each(T), |conn, timeout| {
                calls += 1;
                if calls == 3 {
                    return Err(Error::Closed); // Not a lock timeout, so the write isn't retried.
                }
                conn.prepare_cached("DELETE FROM cache WHERE id IN (SELECT id FROM cache ORDER BY id LIMIT ?1) RETURNING size, filename")
                    .and_then(|mut stmt| collect_removed(stmt.query_map([CHUNK], row_removed)?))
                    .map_err(|e| Error::from_sqlite(e, timeout))
            })
            .unwrap_err();
        assert_eq!(stopped.removed, 200, "two chunks committed before the third failed");
        assert!(matches!(stopped.error, Error::Closed), "{:?}", stopped.error);
        assert_eq!(s.len(T).unwrap(), 150, "and the failed chunk removed nothing");
    }

    #[test]
    fn cull_evicts_until_under_the_limit() {
        let tmp = TempDir::new("maint-cull");
        let s = Store::open(&OpenOptions {
            directory: tmp.path().to_path_buf(),
            timeout: T,
            requested: Requested { size_limit: Some(1 << 20), cull_limit: Some(0) },
            max_connections: DEFAULT_MAX_CONNECTIONS,
            cache_pages: DEFAULT_CACHE_PAGES,
            wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
            auto_checkpoint: false,
            role: crate::open::Role::Plain,
            default_size_limit: crate::schema::DEFAULT_SIZE_LIMIT,
        })
        .unwrap();
        let blob = vec![1u8; 30_000];
        for i in 0..80 {
            s.set(T, &format!("k{i:02}"), Value::Bytes { mode: 2, data: &blob }, None, NOW).unwrap();
        }
        assert!(s.volume(T).unwrap() > 1 << 20, "cull_limit=0 means set never culls");
        assert!(s.cull(T, NOW).unwrap() > 0);
        assert!(s.volume(T).unwrap() < 1 << 20);
    }
}
