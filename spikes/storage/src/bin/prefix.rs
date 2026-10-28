//! Experiment 5: an index-backed, keyset-paged prefix scan.
//!
//! `prefix run --dir D --sizes 100000,400000` populates caches with the bench/baseline.py key shape
//! (`{h|d}:{i % 24}:{ip}:{i}`, 25% under `d:`), prints EXPLAIN QUERY PLAN for the page query, and times:
//! the `d:` prefix scan (25% of keys), a narrow `d:4:` scan (1/24 of keys), and a full keyset walk + filter.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use rusqlite::{Connection, params};
use storage_spike::{Args, Rng, Store, WaitPolicy, fresh_dir, now};

const FIRST_PAGE: &str = "SELECT key FROM cache WHERE key >= ?1 AND key < ?2 ORDER BY key LIMIT ?3";
const NEXT_PAGE: &str = "SELECT key FROM cache WHERE key > ?1 AND key < ?2 ORDER BY key LIMIT ?3";
const FIRST_PAGE_OPEN: &str = "SELECT key FROM cache WHERE key >= ?1 ORDER BY key LIMIT ?2";
const NEXT_PAGE_OPEN: &str = "SELECT key FROM cache WHERE key > ?1 ORDER BY key LIMIT ?2";

fn main() {
    let args = Args::from_env();
    let dir = PathBuf::from(args.get("--dir").expect("--dir"));
    let sizes: Vec<usize> =
        args.get("--sizes").unwrap_or("100000,400000").split(',').map(|s| s.parse().unwrap()).collect();
    let page: i64 = args.num("--page", 100);
    for n in sizes {
        let d = dir.join(format!("n{n}"));
        if !d.join(storage_spike::DB_FILENAME).exists() || args.flag("--rebuild") {
            fresh_dir(&d);
            populate(&d, n);
        }
        let (store, _) = Store::open(&d, 32 * 1024, WaitPolicy::Default(Duration::from_secs(5))).unwrap();
        if n == 100_000 || args.flag("--plans") {
            explain(&store.conn, FIRST_PAGE, params!["d:", "d;", page]);
            explain(&store.conn, NEXT_PAGE, params!["d:0:", "d;", page]);
        }
        for p in [page, 1000] {
            let (hits, t) = best_of(5, || scan_prefix(&store.conn, "d:", p).len());
            let (narrow, tn) = best_of(5, || scan_prefix(&store.conn, "d:4:", p).len());
            let (all, ta) = best_of(3, || scan_all(&store.conn, p).into_iter().filter(|k| k.starts_with("d:")).count());
            println!(
                "n={n} page={p} | prefix 'd:' -> {hits} keys in {:.1} ms | prefix 'd:4:' -> {narrow} keys in {:.2} ms | \
                 full keyset walk + filter -> {all} keys in {:.1} ms",
                t * 1e3,
                tn * 1e3,
                ta * 1e3
            );
        }
    }
}

fn populate(dir: &Path, n: usize) {
    let (store, _) = Store::open(dir, 32 * 1024, WaitPolicy::Default(Duration::from_secs(5))).unwrap();
    let mut rng = Rng::new(7);
    store.conn.execute_batch("BEGIN IMMEDIATE").unwrap();
    {
        let mut stmt = store
            .conn
            .prepare(
                "INSERT INTO cache (key, mode, value, filename, size, store_time, expire_time, store_seq)
                 VALUES (?1, 2, ?2, NULL, 8, ?3, NULL, ?4)",
            )
            .unwrap();
        let t = now();
        for i in 0..n {
            let prefix = if i % 4 == 0 { 'd' } else { 'h' };
            let ip = format!("{}.{}.{}.{}", rng.below(256), rng.below(256), rng.below(256), i % 256);
            let key = format!("{prefix}:{}:{ip}:{i}", i % 24);
            stmt.execute(params![key, (i as i64).to_le_bytes().to_vec(), t, i as i64 + 1]).unwrap();
        }
    }
    store.conn.execute_batch("COMMIT").unwrap();
    store.conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)").unwrap();
}

fn explain(conn: &Connection, sql: &str, p: &[&dyn rusqlite::ToSql]) {
    let mut stmt = conn.prepare(&format!("EXPLAIN QUERY PLAN {sql}")).unwrap();
    let rows: Vec<String> = stmt.query_map(p, |r| r.get::<_, String>(3)).unwrap().map(Result::unwrap).collect();
    println!("EXPLAIN QUERY PLAN {sql}\n    -> {}", rows.join(" | "));
}

/// The smallest string greater than every string that starts with `prefix`, or None if there is none.
/// UTF-8 byte order equals code point order, so incrementing the last code point is exact.
pub fn prefix_upper(prefix: &str) -> Option<String> {
    let mut chars: Vec<char> = prefix.chars().collect();
    while let Some(last) = chars.pop() {
        let next = match last as u32 {
            0x10FFFF => continue,
            0xD7FF => Some('\u{E000}'),
            c => char::from_u32(c + 1),
        };
        if let Some(c) = next {
            chars.push(c);
            return Some(chars.into_iter().collect());
        }
    }
    None
}

/// Keyset paging: every page is its own statement (no read transaction is held between pages), and
/// the cursor is the last key returned.
fn scan_prefix(conn: &Connection, prefix: &str, page: i64) -> Vec<String> {
    let Some(upper) = prefix_upper(prefix) else { return scan_all(conn, page) };
    let mut out: Vec<String> = Vec::new();
    loop {
        let rows: Vec<String> = match out.last() {
            None => collect(conn, FIRST_PAGE, params![prefix, upper, page]),
            Some(last) => collect(conn, NEXT_PAGE, params![last, upper, page]),
        };
        let got = rows.len() as i64;
        out.extend(rows);
        if got < page {
            return out;
        }
    }
}

fn scan_all(conn: &Connection, page: i64) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    loop {
        let rows: Vec<String> = match out.last() {
            None => collect(conn, FIRST_PAGE_OPEN, params!["", page]),
            Some(last) => collect(conn, NEXT_PAGE_OPEN, params![last, page]),
        };
        let got = rows.len() as i64;
        out.extend(rows);
        if got < page {
            return out;
        }
    }
}

fn collect(conn: &Connection, sql: &str, p: &[&dyn rusqlite::ToSql]) -> Vec<String> {
    let mut stmt = conn.prepare_cached(sql).unwrap();
    stmt.query_map(p, |r| r.get::<_, String>(0)).unwrap().map(Result::unwrap).collect()
}

fn best_of<T>(n: usize, mut f: impl FnMut() -> T) -> (T, f64) {
    let mut best = f64::INFINITY;
    let mut last = None;
    for _ in 0..n {
        let t = Instant::now();
        let v = f();
        best = best.min(t.elapsed().as_secs_f64());
        last = Some(v);
    }
    (last.unwrap(), best)
}

#[cfg(test)]
mod tests {
    use super::prefix_upper;

    #[test]
    fn upper_bounds() {
        assert_eq!(prefix_upper("d:").as_deref(), Some("d;"));
        assert_eq!(prefix_upper("a\u{10FFFF}").as_deref(), Some("b"));
        assert_eq!(prefix_upper("\u{D7FF}").as_deref(), Some("\u{E000}"));
        assert_eq!(prefix_upper(""), None);
        assert_eq!(prefix_upper("\u{10FFFF}"), None);
    }
}
