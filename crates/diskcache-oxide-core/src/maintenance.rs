//! `expire()`, `clear()`, `cull()` and key iteration (Phase 2 step 6; 04-design.md sections 6 and 9).
//!
//! Maintenance works in chunks of [`CHUNK`] rows, each its own write transaction, bounded by a
//! `store_seq` snapshot taken when the call starts. So a call always finishes (D-011): rows stored
//! after it started are never part of it, and writes made meanwhile survive. Between chunks it
//! unlinks the chunk's value files, pauses briefly with the interpreter released, and yields to
//! any same-process writer waiting on the write lock, so writers get in during a long `clear()`.
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

impl Store {
    /// Removes every row that expired before `now`, in chunks. Returns how many it removed.
    pub fn expire(&self, timeout: Duration, now: f64) -> Result<u64> {
        let snapshot = self.seq_snapshot(timeout)?;
        self.chunked(timeout, |conn| {
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

    /// Removes every row stored before the call started, in chunks. Returns how many it removed.
    pub fn clear(&self, timeout: Duration) -> Result<u64> {
        let snapshot = self.seq_snapshot(timeout)?;
        self.chunked(timeout, |conn| {
            conn.prepare_cached(
                "DELETE FROM cache WHERE id IN (
                     SELECT id FROM cache WHERE store_seq < ?1 ORDER BY store_seq LIMIT ?2)
                 RETURNING size, filename",
            )
            .and_then(|mut stmt| collect_removed(stmt.query_map((snapshot, CHUNK), row_removed)?))
            .map_err(|e| Error::from_sqlite(e, timeout))
        })
    }

    /// Removes expired rows, then evicts least-recently-stored rows until the cache is under its
    /// `size_limit`. Returns how many it removed in total.
    pub fn cull(&self, timeout: Duration, now: f64) -> Result<u64> {
        let mut removed = self.expire(timeout, now)?;
        loop {
            let (over, batch) = self.write_files(timeout, LockPriority::Normal, |writer, conn| {
                let (size_limit, size): (i64, i64) = conn
                    .prepare_cached("SELECT size_limit, size FROM totals WHERE only = 1")
                    .and_then(|mut stmt| stmt.query_row([], |r| Ok((r.get(0)?, r.get(1)?))))
                    .map_err(|e| Error::from_sqlite(e, timeout))?;
                if crate::ops::volume_in(conn, size, timeout)? < size_limit {
                    return Ok(Changed::removing((false, 0), []));
                }
                let batch = writer.delete_oldest(CHUNK)?;
                Ok(Changed::removing((true, count(batch.len())), batch))
            })?;
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
        let upper = prefix.and_then(prefix_upper);
        let lower = prefix.unwrap_or("");
        self.read(timeout, |conn| page(conn, timeout, order, lower, upper.as_deref(), cursor))
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
    fn chunked(
        &self,
        timeout: Duration,
        mut delete_chunk: impl FnMut(&Connection) -> Result<Vec<(i64, Option<String>)>>,
    ) -> Result<u64> {
        let mut total = 0;
        let mut yielded = false;
        loop {
            let chunk = |writer: &RowWriter<'_>, conn: &Connection| -> Result<Changed<u64>> {
                writer.raise_seq_floor()?;
                let rows = delete_chunk(conn)?;
                let size: i64 = rows.iter().map(|(size, _)| size).sum();
                writer.adjust_totals_after_delete(i64::try_from(rows.len()).unwrap_or(i64::MAX), size)?;
                let removed = count(rows.len());
                Ok(Changed::removing(removed, rows.into_iter().map(|(_, filename)| Removed { filename })))
            };
            // After stepping aside between chunks, take the lock back with priority.
            let priority = if yielded { LockPriority::AfterYield } else { LockPriority::Normal };
            let batch = self.write_files(timeout, priority, chunk)?;
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
                    stmt.query_map((KEY_TYPE_STR, after, PAGE), |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
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
                .and_then(|mut stmt| stmt.query_map((KEY_TYPE_STR, low, high, PAGE), |r| r.get(0))?.collect())
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
