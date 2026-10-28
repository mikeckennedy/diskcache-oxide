//! Experiment 3: opening with unchanged settings never writes (D-019).
//!
//! `noopen run --dir D` populates a cache, checkpoints the WAL, then has a child process (this binary,
//! `cycle` role) open the cache and read (get, contains, len, iterkeys, volume) while a peer
//! connection watches `PRAGMA data_version` and the main file's hash and mtime are compared.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime};

use rusqlite::Connection;
use storage_spike::{Args, DB_FILENAME, GetStats, OpenStyle, Rng, Store, WaitPolicy, fnv64, fresh_dir, make_blob};

const POLICY: WaitPolicy = WaitPolicy::Default(Duration::from_secs(5));
const THRESHOLD: usize = 32 * 1024;

fn main() {
    let args = Args::from_env();
    let dir = PathBuf::from(args.get("--dir").expect("--dir"));
    match args.first() {
        "run" => run(&dir),
        "cycle" => cycle(&dir, args.get("--style").unwrap_or("check"), args.get("--setting").unwrap_or("a")),
        other => panic!("unknown role {other:?}"),
    }
}

fn settings(v: &str) -> Vec<(&'static str, String)> {
    vec![("spill_threshold", THRESHOLD.to_string()), ("size_limit", "1073741824".into()), ("eviction", v.into())]
}

fn file_state(dir: &Path) -> (u64, SystemTime, u64) {
    let p = dir.join(DB_FILENAME);
    let bytes = std::fs::read(&p).unwrap();
    let meta = std::fs::metadata(&p).unwrap();
    (fnv64(&bytes), meta.modified().unwrap(), meta.len())
}

fn run(dir: &Path) {
    fresh_dir(dir);
    {
        let (store, _) = Store::open_with(dir, THRESHOLD, POLICY, OpenStyle::CheckFirst, &settings("a")).unwrap();
        let mut rng = Rng::new(3);
        for i in 0..1000 {
            let key = format!("k{i}");
            if i % 50 == 0 {
                store.set(&key, &make_blob(&key, 0, &mut rng, 2 * THRESHOLD, 0)).unwrap();
            } else {
                store.set(&key, format!("value-{i}").as_bytes()).unwrap();
            }
        }
    }
    let exe = std::env::current_exe().unwrap();
    let cycle = |style: &str, setting: &str| -> String {
        let out = Command::new(&exe)
            .args(["cycle", "--dir", &dir.display().to_string(), "--style", style, "--setting", setting])
            .output()
            .unwrap();
        assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
        String::from_utf8(out.stdout).unwrap().trim().to_string()
    };

    // Each case: (label, style, setting value). The peer is open the whole time.
    let cases = [
        ("check-first, same settings", "check", "a"),
        ("check-first, same settings (again)", "check", "a"),
        ("unconditional writes (control)", "unconditional", "a"),
        ("check-first, changed setting", "check", "b"),
        ("check-first, after the change", "check", "b"),
    ];
    for (label, style, setting) in cases {
        checkpoint(dir);
        let peer = Connection::open(dir.join(DB_FILENAME)).unwrap();
        let dv0: i64 = peer.query_row("PRAGMA data_version", [], |r| r.get(0)).unwrap();
        let before = file_state(dir);
        std::thread::sleep(Duration::from_millis(20)); // so an mtime change can't hide in timestamp granularity
        let child = cycle(style, setting);
        let dv1: i64 = peer.query_row("PRAGMA data_version", [], |r| r.get(0)).unwrap();
        let wal = std::fs::metadata(dir.join(format!("{DB_FILENAME}-wal"))).map(|m| m.len()).unwrap_or(0);
        let during = file_state(dir);
        drop(peer); // last connection closes: SQLite checkpoints whatever the WAL holds
        let after = file_state(dir);
        println!(
            "{label:38} | child: {child} | peer data_version {dv0}->{dv1} | wal_bytes={wal} | main file with peer open: \
             hash_same={} mtime_same={} | after last close: hash_same={} mtime_same={}",
            before.0 == during.0,
            before.1 == during.1,
            before.0 == after.0,
            before.1 == after.1,
        );
    }

    // No peer at all: the child's connection is the only one, so its close runs the final checkpoint.
    checkpoint(dir);
    let before = file_state(dir);
    std::thread::sleep(Duration::from_millis(20));
    let child = cycle("check", "b");
    let after = file_state(dir);
    let wal_exists = dir.join(format!("{DB_FILENAME}-wal")).exists();
    println!(
        "{:38} | child: {child} | main file: hash_same={} mtime_same={} | wal file left behind={wal_exists}",
        "check-first, no peer connection",
        before.0 == after.0,
        before.1 == after.1
    );
}

fn checkpoint(dir: &Path) {
    let c = Connection::open(dir.join(DB_FILENAME)).unwrap();
    let (busy, log, done): (i64, i64, i64) =
        c.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).unwrap();
    assert_eq!((busy, log, done), (0, 0, 0), "checkpoint incomplete");
}

/// Open + the read-only operations from the D-019 test, then close.
fn cycle(dir: &Path, style: &str, setting: &str) {
    let style = if style == "unconditional" { OpenStyle::Unconditional } else { OpenStyle::CheckFirst };
    let (mut store, report) = Store::open_with(dir, THRESHOLD, POLICY, style, &settings(setting)).unwrap();
    let mut st = GetStats::default();
    let mut hits = 0;
    for i in [0, 1, 50, 999, 5000] {
        hits += usize::from(matches!(store.get(&format!("k{i}"), &mut st).unwrap(), storage_spike::Got::Hit(_)));
    }
    let contains: bool = store.conn.query_row("SELECT EXISTS(SELECT 1 FROM cache WHERE key = 'k7')", [], |r| r.get(0)).unwrap();
    let len = store.count().unwrap();
    let keys: Vec<String> = {
        let mut s = store.conn.prepare("SELECT key FROM cache ORDER BY rowid").unwrap();
        s.query_map([], |r| r.get(0)).unwrap().collect::<rusqlite::Result<_>>().unwrap()
    };
    let volume: i64 = store.conn.query_row("SELECT COALESCE(SUM(size), 0) FROM cache", [], |r| r.get(0)).unwrap();
    print!(
        "open wrote={} (wal={} schema={} settings={}) hits={hits} in={contains} len={len} keys={} volume={volume}",
        report.wrote(),
        report.set_wal,
        report.created_schema,
        report.wrote_settings,
        keys.len()
    );
}
