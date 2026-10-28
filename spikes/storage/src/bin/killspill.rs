//! Experiment 2: SIGKILL mid-spill (Test I).
//!
//! `killspill run --dir D --rounds 100 --threshold 32768` repeatedly spawns a writer (this binary,
//! `writer` role) that overwrites 6 spilled keys forever, SIGKILLs it at a random moment, then checks
//! the cache and counts orphans.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use rusqlite::Connection;
use storage_spike::{Args, DB_FILENAME, GetStats, Got, Rng, Store, WaitPolicy, blob_ok, fresh_dir, make_blob, touch, wait_for};

const KEYS: [&str; 6] = ["blob-0", "blob-1", "blob-2", "blob-3", "blob-4", "blob-5"];
const POLICY: WaitPolicy = WaitPolicy::Default(Duration::from_secs(10));

fn main() {
    let args = Args::from_env();
    let dir = PathBuf::from(args.get("--dir").expect("--dir"));
    let threshold: usize = args.num("--threshold", 32 * 1024);
    match args.first() {
        "run" => run(&args, &dir, threshold),
        "writer" => writer(&args, &dir, threshold),
        other => panic!("unknown role {other:?}"),
    }
}

fn run(args: &Args, dir: &Path, threshold: usize) {
    let rounds: u64 = args.num("--rounds", 100);
    let max_delay_ms: u64 = args.num("--max-delay-ms", 50);
    let deletes = args.flag("--deletes");
    fresh_dir(dir);
    drop(Store::open(dir, threshold, POLICY).unwrap());
    let exe = std::env::current_exe().unwrap();
    let mut rng = Rng::new(42);
    for n in 0..rounds {
        let started = dir.join(format!("started-{n}"));
        let mut cmd = Command::new(&exe);
        cmd.args(["writer", "--dir", &dir.display().to_string(), "--threshold", &threshold.to_string()]);
        cmd.args(["--seed", &n.to_string()]);
        if deletes {
            cmd.arg("--deletes");
        }
        let mut child = cmd.spawn().unwrap();
        assert!(wait_for(&started, Duration::from_secs(10)), "writer never got going");
        std::thread::sleep(Duration::from_micros(rng.below(max_delay_ms * 1000 + 1)));
        child.kill().unwrap(); // SIGKILL on Unix
        let status = child.wait().unwrap();
        assert!(!status.success());
        let _ = std::fs::remove_file(&started);
    }

    // Checks, in the order Test I makes them.
    let integrity: String = {
        let c = Connection::open(dir.join(DB_FILENAME)).unwrap();
        c.query_row("PRAGMA integrity_check", [], |r| r.get(0)).unwrap()
    };
    let (mut store, report) = Store::open(dir, threshold, POLICY).unwrap();
    let min_size = 2 * threshold;
    let (mut hits, mut misses, mut true_misses, mut corrupt) = (0, 0, 0, 0);
    for key in KEYS {
        let mut st = GetStats::default();
        match store.get(key, &mut st).unwrap() {
            Got::Hit(b) if blob_ok(key, &b, min_size) => hits += 1,
            Got::Hit(_) => corrupt += 1,
            Got::Miss => misses += 1,
            Got::TrueMiss { .. } => true_misses += 1,
        }
    }
    let after = vec![b'a'; min_size];
    store.set("after", &after).unwrap();
    let mut st = GetStats::default();
    let after_ok = matches!(store.get("after", &mut st).unwrap(), Got::Hit(b) if b == after);
    let rows = store.count().unwrap();
    let files = store.spill_file_count().unwrap();
    let (orphans, tmps) = store.find_orphans().unwrap();
    println!(
        "rounds={rounds} threshold={threshold} deletes={deletes} | integrity={integrity} open_wrote={} | keys hits={hits} \
         misses={misses} true_misses={true_misses} corrupt={corrupt} | after_ok={after_ok} rows={rows} val_files={files} \
         orphan_val={} orphan_tmp={}",
        report.wrote(),
        orphans.len(),
        tmps.len()
    );
}

fn writer(args: &Args, dir: &Path, threshold: usize) {
    let seed: u64 = args.num("--seed", 0);
    let deletes = args.flag("--deletes");
    let (store, _) = Store::open(dir, threshold, POLICY).unwrap();
    let mut rng = Rng::new(seed + 1000);
    let mut n = 0u64;
    loop {
        let key = KEYS[rng.below(KEYS.len() as u64) as usize];
        if deletes && rng.below(4) == 0 {
            store.delete(key).unwrap();
        } else {
            store.set(key, &make_blob(key, n, &mut rng, 2 * threshold, threshold)).unwrap();
        }
        n += 1;
        if n == 3 {
            touch(&dir.join(format!("started-{seed}")));
        }
    }
}
