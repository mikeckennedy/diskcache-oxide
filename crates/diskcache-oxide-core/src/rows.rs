//! `RowWriter`: the single choke point for every change to the `cache` table (04-design.md section 3).
//!
//! Every insert, overwrite and delete goes through here, inside the caller's write transaction,
//! so the `totals` row (`count`, `size`, `next_seq`) always agrees with the table. Nothing else in
//! the crate issues `INSERT`, `UPDATE` or `DELETE` against `cache`.
//!
//! `store_seq` never repeats (D-011): a new one is `max(MAX(store_seq) + 1, totals.next_seq)`.
//! `MAX(store_seq)` alone would hand out a number again after the newest row is deleted, and a
//! running `clear()` could then delete a write made after it started. So every delete first raises
//! `totals.next_seq` (the floor) past the current maximum. Overwrites never touch `totals` unless
//! the count or size changes, which keeps a plain `set` to the same WAL pages as diskcache's.

use std::time::Duration;

use rusqlite::types::{ToSqlOutput, Value};
use rusqlite::{Connection, OptionalExtension, ToSql};

use crate::schema::KEY_TYPE_STR;
use crate::{Error, Result};

/// What a row stores in its `value` column.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Stored<'a> {
    /// Inline bytes: raw str (UTF-8), raw bytes, or a serialized value.
    Blob(&'a [u8]),
    /// A native SQLite integer (mode 6).
    Int(i64),
    /// A native SQLite float (mode 7).
    Real(f64),
    /// The value lives in this value file (relative path); the column is NULL.
    File(&'a str),
}

impl ToSql for Stored<'_> {
    fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
        Ok(match *self {
            Stored::Blob(bytes) => ToSqlOutput::from(bytes),
            Stored::Int(n) => ToSqlOutput::from(n),
            Stored::Real(x) => ToSqlOutput::from(x),
            Stored::File(_) => ToSqlOutput::Owned(Value::Null),
        })
    }
}

/// A row to store under `key`.
#[derive(Debug, Clone, Copy)]
pub struct NewRow<'a> {
    pub key: &'a str,
    pub mode: i64,
    pub value: Stored<'a>,
    /// Bytes counted toward `volume()` beyond the database pages: the value file's length for a
    /// spilled value, and 0 for an inline one (its bytes are already in `page_count × page_size`).
    pub size: i64,
    pub store_time: f64,
    pub expire_time: Option<f64>,
}

/// The previous version of a row that a write replaced or deleted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Removed {
    /// Its value file, which the caller unlinks after the transaction commits.
    pub filename: Option<String>,
}

/// Writes rows inside one write transaction, keeping `totals` exact.
#[derive(Debug)]
pub struct RowWriter<'t> {
    /// A connection inside a write transaction: a `Transaction` (it derefs to `Connection`) or a
    /// held transaction's pinned connection.
    tx: &'t Connection,
    timeout: Duration,
}

impl<'t> RowWriter<'t> {
    #[must_use]
    pub fn new(tx: &'t Connection, timeout: Duration) -> Self {
        Self { tx, timeout }
    }

    /// Inserts or overwrites `row`. An overwrite keeps the row's `id` (insertion order, which
    /// `__iter__` follows) but gets a new `store_seq` (store order, which eviction follows).
    /// Returns the replaced version, if there was one.
    pub fn upsert(&self, row: &NewRow<'_>) -> Result<(i64, Option<Removed>)> {
        let old = self.old_row(row.key)?;
        self.upsert_after(row, old)
    }

    /// The stored version of `key`, if any: its `size` and value file.
    pub fn old_row(&self, key: &str) -> Result<Option<(i64, Option<String>)>> {
        self.tx
            .prepare_cached("SELECT size, filename FROM cache WHERE key_type = ?1 AND key = ?2")
            .and_then(|mut stmt| stmt.query_row((KEY_TYPE_STR, key), |r| Ok((r.get(0)?, r.get(1)?))).optional())
            .map_err(|e| self.sqlite(e))
    }

    /// [`RowWriter::upsert`] when the caller already read the stored version (`old`) in this
    /// transaction, saving a statement on the hot path.
    pub fn upsert_after(&self, row: &NewRow<'_>, old: Option<(i64, Option<String>)>) -> Result<(i64, Option<Removed>)> {
        let filename = match row.value {
            Stored::File(name) => Some(name),
            _ => None,
        };
        let seq: i64 = self
            .tx
            .prepare_cached(
                "INSERT INTO cache (key_type, key, mode, value, filename, size, store_time, expire_time, store_seq)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8,
                         (SELECT MAX(COALESCE((SELECT MAX(store_seq) FROM cache), 0) + 1, next_seq) FROM totals WHERE only = 1))
                 ON CONFLICT (key_type, key) DO UPDATE SET
                     mode = excluded.mode, value = excluded.value, filename = excluded.filename,
                     size = excluded.size, store_time = excluded.store_time,
                     expire_time = excluded.expire_time, store_seq = excluded.store_seq
                 RETURNING store_seq",
            )
            .and_then(|mut stmt| {
                stmt.query_row(
                    (KEY_TYPE_STR, row.key, row.mode, row.value, filename, row.size, row.store_time, row.expire_time),
                    |r| r.get::<_, i64>(0),
                )
            })
            .map_err(|e| self.sqlite(e))?;
        // `totals` only changes when the count or size does: an inline overwrite writes no extra page.
        let (count, size, replaced) = match old {
            Some((old_size, old_filename)) => (0, row.size - old_size, Some(Removed { filename: old_filename })),
            None => (1, row.size, None),
        };
        self.adjust_totals(count, size)?;
        Ok((seq, replaced))
    }

    /// Deletes `key`. Returns the removed version, or `None` if the key wasn't stored.
    pub fn delete_key(&self, key: &str) -> Result<Option<Removed>> {
        self.raise_seq_floor()?;
        let removed = self
            .tx
            .prepare_cached("DELETE FROM cache WHERE key_type = ?1 AND key = ?2 RETURNING size, filename")
            .and_then(|mut stmt| stmt.query_row((KEY_TYPE_STR, key), |r| Ok((r.get(0)?, r.get(1)?))).optional())
            .map_err(|e| self.sqlite(e))?;
        self.finish_delete(removed)
    }

    /// Deletes exactly one stored version, `(id, store_seq)`, and nothing newer. The D-013
    /// compare-and-delete: a reader that found a value file missing removes that version only, so
    /// it can never delete a row a writer has just replaced.
    pub fn delete_version(&self, id: i64, store_seq: i64) -> Result<Option<Removed>> {
        self.raise_seq_floor()?;
        let removed = self
            .tx
            .prepare_cached("DELETE FROM cache WHERE id = ?1 AND store_seq = ?2 RETURNING size, filename")
            .and_then(|mut stmt| stmt.query_row((id, store_seq), |r| Ok((r.get(0)?, r.get(1)?))).optional())
            .map_err(|e| self.sqlite(e))?;
        self.finish_delete(removed)
    }

    /// Deletes up to `limit` rows that expired *before* `now`, earliest expiry first.
    ///
    /// Strictly before: a row expiring exactly at `now` already reads as a miss (`expire_time > now`
    /// is false) but isn't swept yet. That's what fits diskcache's pinned behavior: `set(expire=0)`
    /// leaves a row that `len` still counts, even though culling runs after the insert.
    pub fn delete_expired(&self, now: f64, limit: i64) -> Result<Vec<Removed>> {
        self.delete_batch(
            "DELETE FROM cache WHERE id IN (
                 SELECT id FROM cache WHERE expire_time IS NOT NULL AND expire_time < ?1
                 ORDER BY expire_time LIMIT ?2)
             RETURNING size, filename",
            (now, limit),
        )
    }

    /// Deletes up to `limit` rows in store order (least recently stored first), the eviction policy.
    pub fn delete_oldest(&self, limit: i64) -> Result<Vec<Removed>> {
        self.delete_batch(
            "DELETE FROM cache WHERE id IN (SELECT id FROM cache ORDER BY store_seq LIMIT ?1) RETURNING size, filename",
            [limit],
        )
    }

    /// Sets the expiry of an unexpired `key` without counting as a store (`store_seq` is unchanged),
    /// as `touch` does. Returns whether the key was present and unexpired.
    pub fn set_expire_time(&self, key: &str, now: f64, expire_time: Option<f64>) -> Result<bool> {
        self.tx
            .prepare_cached(
                "UPDATE cache SET expire_time = ?3
                 WHERE key_type = ?1 AND key = ?2 AND (expire_time IS NULL OR expire_time > ?4)",
            )
            .and_then(|mut stmt| stmt.execute((KEY_TYPE_STR, key, expire_time, now)))
            .map(|changed| changed > 0)
            .map_err(|e| self.sqlite(e))
    }

    /// Fixes up `totals` after the caller deleted `count` rows of `size` bytes itself in this
    /// transaction (maintenance chunks, which delete with their own bounded statements).
    pub fn adjust_totals_after_delete(&self, count: i64, size: i64) -> Result<()> {
        self.adjust_totals(-count, -size)
    }

    /// Raises `totals.next_seq` past every `store_seq` in the table. Called before any delete, so a
    /// deleted row's number is never handed out again. Touches the `totals` page the delete's count
    /// change writes anyway.
    pub fn raise_seq_floor(&self) -> Result<()> {
        self.tx
            .prepare_cached(
                "UPDATE totals SET next_seq = MAX(next_seq, COALESCE((SELECT MAX(store_seq) FROM cache), 0) + 1)
                 WHERE only = 1",
            )
            .and_then(|mut stmt| stmt.execute([]))
            .map(|_| ())
            .map_err(|e| self.sqlite(e))
    }

    fn delete_batch(&self, sql: &str, params: impl rusqlite::Params) -> Result<Vec<Removed>> {
        self.raise_seq_floor()?;
        let mut size = 0;
        let removed = self
            .tx
            .prepare_cached(sql)
            .and_then(|mut stmt| {
                stmt.query_map(params, |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?)))?
                    .collect::<rusqlite::Result<Vec<_>>>()
            })
            .map_err(|e| self.sqlite(e))?
            .into_iter()
            .map(|(row_size, filename)| {
                size += row_size;
                Removed { filename }
            })
            .collect::<Vec<_>>();
        let count = i64::try_from(removed.len()).unwrap_or(i64::MAX);
        self.adjust_totals(-count, -size)?;
        Ok(removed)
    }

    fn finish_delete(&self, removed: Option<(i64, Option<String>)>) -> Result<Option<Removed>> {
        match removed {
            Some((size, filename)) => {
                self.adjust_totals(-1, -size)?;
                Ok(Some(Removed { filename }))
            }
            None => Ok(None),
        }
    }

    fn adjust_totals(&self, count: i64, size: i64) -> Result<()> {
        if count == 0 && size == 0 {
            return Ok(());
        }
        self.tx
            .prepare_cached("UPDATE totals SET count = count + ?1, size = size + ?2 WHERE only = 1")
            .and_then(|mut stmt| stmt.execute((count, size)))
            .map(|_| ())
            .map_err(|e| self.sqlite(e))
    }

    fn sqlite(&self, error: rusqlite::Error) -> Error {
        Error::from_sqlite(error, self.timeout)
    }
}

#[cfg(test)]
mod tests {
    use rusqlite::{Connection, TransactionBehavior};

    use super::*;
    use crate::open::{self, Requested};
    use crate::testutil::TempDir;

    const T: Duration = Duration::from_secs(5);

    fn new_db(tmp: &TempDir) -> Connection {
        open::ensure_directory(tmp.path()).unwrap();
        let mut conn = open::connect(tmp.path(), T).unwrap();
        open::prepare(&mut conn, Requested::default(), T).unwrap();
        conn
    }

    fn write<R>(conn: &mut Connection, f: impl FnOnce(&RowWriter<'_>) -> R) -> R {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate).unwrap();
        let result = f(&RowWriter::new(&tx, T));
        tx.commit().unwrap();
        result
    }

    fn row<'a>(key: &'a str, value: &'a [u8]) -> NewRow<'a> {
        NewRow {
            key,
            mode: 2,
            value: Stored::Blob(value),
            size: i64::try_from(value.len()).unwrap(),
            store_time: 1.0,
            expire_time: None,
        }
    }

    fn totals(conn: &Connection) -> (i64, i64) {
        conn.query_row("SELECT count, size FROM totals", [], |r| Ok((r.get(0)?, r.get(1)?))).unwrap()
    }

    fn actual(conn: &Connection) -> (i64, i64) {
        conn.query_row("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cache", [], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
    }

    #[test]
    fn overwrite_keeps_id_and_takes_a_new_seq() {
        let tmp = TempDir::new("rows-overwrite");
        let mut conn = new_db(&tmp);
        let (first, replaced) = write(&mut conn, |w| w.upsert(&row("a", b"one")).unwrap());
        assert_eq!(replaced, None);
        let id: i64 = conn.query_row("SELECT id FROM cache WHERE key = 'a'", [], |r| r.get(0)).unwrap();
        let (second, replaced) = write(&mut conn, |w| w.upsert(&row("a", b"three")).unwrap());
        assert_eq!(replaced, Some(Removed { filename: None }));
        assert!(second > first);
        let (id_after, seq): (i64, i64) = conn
            .query_row("SELECT id, store_seq FROM cache WHERE key = 'a'", [], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap();
        assert_eq!((id_after, seq), (id, second));
        assert_eq!(totals(&conn), (1, 5));
    }

    #[test]
    fn seq_and_id_never_repeat_after_the_newest_row_is_deleted() {
        let tmp = TempDir::new("rows-monotonic");
        let mut conn = new_db(&tmp);
        write(&mut conn, |w| w.upsert(&row("a", b"1")).unwrap());
        let (b_seq, _) = write(&mut conn, |w| w.upsert(&row("b", b"2")).unwrap());
        let b_id: i64 = conn.query_row("SELECT id FROM cache WHERE key = 'b'", [], |r| r.get(0)).unwrap();
        write(&mut conn, |w| w.delete_key("b").unwrap());
        let (c_seq, _) = write(&mut conn, |w| w.upsert(&row("c", b"3")).unwrap());
        let c_id: i64 = conn.query_row("SELECT id FROM cache WHERE key = 'c'", [], |r| r.get(0)).unwrap();
        assert!(c_seq > b_seq, "store_seq reused: {c_seq} <= {b_seq}");
        assert!(c_id > b_id, "id reused: {c_id} <= {b_id}");
    }

    #[test]
    fn delete_version_only_removes_that_exact_version() {
        let tmp = TempDir::new("rows-cad");
        let mut conn = new_db(&tmp);
        let (old_seq, _) = write(&mut conn, |w| w.upsert(&row("k", b"old")).unwrap());
        let id: i64 = conn.query_row("SELECT id FROM cache WHERE key = 'k'", [], |r| r.get(0)).unwrap();
        write(&mut conn, |w| w.upsert(&row("k", b"new")).unwrap());
        // A reader still holding the old version must not delete the new one.
        assert_eq!(write(&mut conn, |w| w.delete_version(id, old_seq).unwrap()), None);
        assert_eq!(totals(&conn), (1, 3));
        let new_seq: i64 = conn.query_row("SELECT store_seq FROM cache WHERE key = 'k'", [], |r| r.get(0)).unwrap();
        assert!(write(&mut conn, |w| w.delete_version(id, new_seq).unwrap()).is_some());
        assert_eq!(totals(&conn), (0, 0));
    }

    #[test]
    fn file_rows_store_null_value_and_report_their_file_on_removal() {
        let tmp = TempDir::new("rows-file");
        let mut conn = new_db(&tmp);
        let file_row = NewRow { value: Stored::File("aa/bb/x.val"), size: 100_000, ..row("big", b"") };
        write(&mut conn, |w| w.upsert(&file_row).unwrap());
        let is_null: bool =
            conn.query_row("SELECT value IS NULL FROM cache WHERE key = 'big'", [], |r| r.get(0)).unwrap();
        assert!(is_null);
        let (_, replaced) = write(&mut conn, |w| w.upsert(&row("big", b"small now")).unwrap());
        assert_eq!(replaced, Some(Removed { filename: Some("aa/bb/x.val".into()) }));
        assert_eq!(totals(&conn), (1, 9));
    }

    #[test]
    fn totals_match_the_table_after_random_operations() {
        // A small deterministic generator, so the test needs no extra crate and reproduces exactly.
        let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
        let mut next = move |bound: u64| {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state % bound
        };
        let tmp = TempDir::new("rows-property");
        let mut conn = new_db(&tmp);
        let keys: Vec<String> = (0..40).map(|i| format!("k{i}")).collect();
        let payload = vec![7u8; 4_096];
        for _ in 0..3_000 {
            let key = &keys[usize::try_from(next(40)).unwrap()];
            let len = usize::try_from(next(4_096)).unwrap();
            match next(10) {
                0..=5 => write(&mut conn, |w| w.upsert(&row(key, &payload[..len])).map(|_| ()).unwrap()),
                6 => {
                    let file_row = NewRow { value: Stored::File("aa/bb/f.val"), size: 70_000, ..row(key, b"") };
                    write(&mut conn, |w| w.upsert(&file_row).map(|_| ()).unwrap());
                }
                7 | 8 => write(&mut conn, |w| w.delete_key(key).map(|_| ()).unwrap()),
                _ => {
                    let version: Option<(i64, i64)> = conn
                        .query_row("SELECT id, store_seq FROM cache WHERE key = ?1", [key], |r| {
                            Ok((r.get(0)?, r.get(1)?))
                        })
                        .optional()
                        .unwrap();
                    if let Some((id, seq)) = version {
                        // Half the time the version is stale and must not match.
                        let seq = if next(2) == 0 { seq } else { seq - 1 };
                        write(&mut conn, |w| w.delete_version(id, seq).map(|_| ()).unwrap());
                    }
                }
            }
        }
        assert_eq!(totals(&conn), actual(&conn));
    }
}
