//! Experiment 1: the spilled-value overwrite race (D-016) and compare-and-delete (D-013).
//!
//! `race run --dir D --threshold 32768 --ops 3000 --readers 4 [--deletes] [--naive]`
//! spawns 1 writer and N reader processes (this same binary, in the `writer`/`reader` roles).

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use storage_spike::{Args, GetStats, Got, Rng, Store, WaitPolicy, blob_ok, fresh_dir, make_blob, touch, wait_for};

const KEY: &str = "hot";
const POLICY: WaitPolicy = WaitPolicy::Default(Duration::from_secs(60));

fn main() {
    let args = Args::from_env();
    let dir = PathBuf::from(args.get("--dir").expect("--dir"));
    let threshold: usize = args.num("--threshold", 32 * 1024);
    let min_size = 2 * threshold;
    match args.first() {
        "run" => run(&args, &dir, threshold, min_size),
        "writer" => writer(&args, &dir, threshold, min_size),
        "reader" => reader(&args, &dir, threshold, min_size),
        other => panic!("unknown role {other:?}"),
    }
}

fn run(args: &Args, dir: &Path, threshold: usize, min_size: usize) {
    let readers: usize = args.num("--readers", 4);
    let ops: u64 = args.num("--ops", 3000);
    fresh_dir(dir);
    let (store, _) = Store::open(dir, threshold, POLICY).unwrap();
    store.set(KEY, &make_blob(KEY, 0, &mut Rng::new(7), min_size, threshold)).unwrap();

    let exe = std::env::current_exe().unwrap();
    let mut passthrough: Vec<String> = vec!["--dir".into(), dir.display().to_string(), "--threshold".into(), threshold.to_string()];
    for f in ["--deletes", "--naive", "--naive-delete"] {
        if args.flag(f) {
            passthrough.push(f.into());
        }
    }
    passthrough.extend(["--gap-us".into(), args.num::<u64>("--gap-us", 0).to_string()]);
    let spawn = |role: &str, extra: &[String]| {
        Command::new(&exe).arg(role).args(&passthrough).args(extra).stdout(Stdio::piped()).spawn().unwrap()
    };
    let started = Instant::now();
    let writer = spawn("writer", &["--ops".into(), ops.to_string()]);
    let reader_procs: Vec<_> = (0..readers).map(|i| spawn("reader", &["--seed".into(), i.to_string()])).collect();
    touch(&dir.join("go"));
    let w = writer.wait_with_output().unwrap();
    assert!(w.status.success(), "writer failed");
    let mut totals = [0u64; 9];
    for r in reader_procs {
        let out = r.wait_with_output().unwrap();
        assert!(out.status.success(), "reader failed: {}", String::from_utf8_lossy(&out.stdout));
        let line = String::from_utf8(out.stdout).unwrap();
        let nums: Vec<u64> = line.split_whitespace().skip(1).map(|n| n.parse().unwrap()).collect();
        for (i, n) in nums.iter().enumerate() {
            if i == 6 {
                totals[i] = totals[i].max(*n);
            } else {
                totals[i] += n;
            }
        }
    }
    let elapsed = started.elapsed();
    let [hits, misses, true_misses, cas_deleted, torn, retries, max_retry, errors, locked] = totals;

    let (mut store, _) = Store::open(dir, threshold, POLICY).unwrap();
    let mut st = GetStats::default();
    let final_ok = matches!(store.get(KEY, &mut st).unwrap(), Got::Hit(b) if blob_ok(KEY, &b, min_size));
    let rows = store.count().unwrap();
    let files = store.spill_file_count().unwrap();
    let (orphans, tmps) = store.find_orphans().unwrap();
    let w_out = String::from_utf8(w.stdout).unwrap();
    println!(
        "threshold={threshold} readers={readers} ops={ops} deletes={} naive={} gap_us={} | {} | reads hits={hits} misses={misses} \
         true_misses={true_misses} cas_deleted={cas_deleted} torn={torn} retries={retries} max_retry={max_retry} \
         errors={errors} locked_fallbacks={locked} | final_ok={final_ok} rows={rows} files={files} orphan_val={} tmp={} | {:.1}s",
        args.flag("--deletes"),
        args.flag("--naive"),
        args.num::<u64>("--gap-us", 0),
        w_out.trim(),
        orphans.len(),
        tmps.len(),
        elapsed.as_secs_f64()
    );
}

fn writer(args: &Args, dir: &Path, threshold: usize, min_size: usize) {
    let ops: u64 = args.num("--ops", 3000);
    let deletes = args.flag("--deletes");
    let (store, _) = Store::open(dir, threshold, POLICY).unwrap();
    let mut rng = Rng::new(1);
    assert!(wait_for(&dir.join("go"), Duration::from_secs(30)));
    let (mut sets, mut dels) = (0u64, 0u64);
    for n in 1..=ops {
        // The last op is always a set, so the key must be present at the end.
        if deletes && n < ops && rng.below(5) == 0 {
            store.delete(KEY).unwrap();
            dels += 1;
        } else {
            store.set(KEY, &make_blob(KEY, n, &mut rng, min_size, threshold)).unwrap();
            sets += 1;
        }
    }
    touch(&dir.join("done"));
    println!("writer sets={sets} deletes={dels}");
}

fn reader(args: &Args, dir: &Path, threshold: usize, min_size: usize) {
    let (mut store, _) = Store::open(dir, threshold, POLICY).unwrap();
    store.naive_reads = args.flag("--naive");
    store.read_gap = Duration::from_micros(args.num("--gap-us", 0));
    assert!(wait_for(&dir.join("go"), Duration::from_secs(30)));
    let done = dir.join("done");
    // hits, misses, true_misses, cas_deleted, torn, retries, max_retry, errors
    let mut c = [0u64; 9];
    loop {
        let last = done.exists();
        let mut st = GetStats::default();
        match store.get(KEY, &mut st) {
            Ok(Got::Hit(b)) if blob_ok(KEY, &b, min_size) => c[0] += 1,
            Ok(Got::Hit(_)) => c[4] += 1,
            Ok(Got::Miss) => {
                c[1] += 1;
                if store.naive_reads && args.flag("--naive-delete") {
                    // Negative control: diskcache-style cleanup by key, which can hit a live row.
                    store.conn.busy_timeout(Duration::ZERO).unwrap();
                    let n = store.conn.execute("DELETE FROM cache WHERE key = ?1", [KEY]).unwrap_or(0);
                    store.conn.busy_timeout(Duration::from_secs(60)).unwrap();
                    c[3] += n as u64;
                }
            }
            Ok(Got::TrueMiss { deleted }) => {
                c[2] += 1;
                c[3] += u64::from(deleted);
            }
            Err(e) => {
                eprintln!("reader error: {e}");
                c[7] += 1;
            }
        }
        c[5] += u64::from(st.retries);
        c[6] = c[6].max(u64::from(st.retries));
        c[8] += u64::from(st.locked_fallback);
        if last {
            break;
        }
    }
    println!("RESULT {}", c.map(|n| n.to_string()).join(" "));
}
