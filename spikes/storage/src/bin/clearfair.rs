//! Experiment 4: does a writer get in during a chunked `clear()` (D-010, D-011)?
//!
//! `clearfair run --dir D --rows 50000 --chunk 100 --pause-us 0 --writer poll|default --trials 20`
//! For each trial: populate, start a writer process (this binary, `writer` role) that waits for the
//! clear to start, sleeps a random fraction of the expected clear time, then does one `set` with a
//! 1.0 s timeout. The clear runs in this process with a long timeout.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use rusqlite::params;
use storage_spike::{Args, Rng, Store, WaitPolicy, fresh_dir, now, touch, wait_for};

const THRESHOLD: usize = 32 * 1024;

fn main() {
    let args = Args::from_env();
    let dir = PathBuf::from(args.get("--dir").expect("--dir"));
    match args.first() {
        "run" => run(&args, &dir),
        "writer" => writer(&args, &dir),
        other => panic!("unknown role {other:?}"),
    }
}

fn policy(name: &str, timeout: Duration, poll_us: u64) -> WaitPolicy {
    match name {
        "default" => WaitPolicy::Default(timeout),
        "poll" => WaitPolicy::Poll { poll: Duration::from_micros(poll_us), timeout },
        other => panic!("unknown policy {other}"),
    }
}

fn populate(store: &Store, rows: usize) {
    store.conn.execute_batch("BEGIN IMMEDIATE").unwrap();
    {
        let mut stmt = store
            .conn
            .prepare(
                "INSERT INTO cache (key, mode, value, filename, size, store_time, expire_time, store_seq)
                 VALUES (?1, 2, ?2, NULL, 100, ?3, NULL, ?4)",
            )
            .unwrap();
        let value = vec![b'x'; 100];
        let t = now();
        for i in 0..rows {
            stmt.execute(params![format!("h:{}:10.0.{}.{}:{i}", i % 24, i % 256, i % 199), value, t, i as i64 + 1])
                .unwrap();
        }
    }
    store.conn.execute_batch("COMMIT").unwrap();
    store.conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)").unwrap();
}

fn run(args: &Args, dir: &Path) {
    let rows: usize = args.num("--rows", 50_000);
    let chunk: usize = args.num("--chunk", 100);
    let pause = Duration::from_micros(args.num("--pause-us", 0));
    let trials: usize = args.num("--trials", 20);
    let writer_policy = args.get("--writer").unwrap_or("poll").to_string();
    let poll_us: u64 = args.num("--poll-us", 1000);
    let clear_policy = policy(args.get("--clearer").unwrap_or("default"), Duration::from_secs(60), poll_us);
    // Writer starts at a random point within this window after the clear's first chunk.
    let window_ms: u64 = args.num("--window-ms", 200);
    let exe = std::env::current_exe().unwrap();
    let mut rng = Rng::new(99);

    let (mut in_during, mut after_clear, mut timeouts, mut survived) = (0, 0, 0, 0);
    let mut waits = Vec::new();
    let mut clear_times = Vec::new();
    for trial in 0..trials {
        fresh_dir(dir);
        let (store, _) = Store::open(dir, THRESHOLD, clear_policy).unwrap();
        populate(&store, rows);
        if let Some(n) = args.get("--autockpt") {
            store.conn.execute_batch(&format!("PRAGMA wal_autocheckpoint = {n}")).unwrap();
        }
        let delay_us = rng.below(window_ms * 1000);
        let child = Command::new(&exe)
            .args(["writer", "--dir", &dir.display().to_string(), "--writer", &writer_policy])
            .args(["--poll-us", &poll_us.to_string(), "--delay-us", &delay_us.to_string()])
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        assert!(wait_for(&dir.join("ready"), Duration::from_secs(10)));
        let started = Instant::now();
        let started_wall = now();
        let (removed, chunks) = store.clear(chunk, pause, |n| if n == 1 { touch(&dir.join("clear-started")) }).unwrap();
        let clear_end = now();
        let clear_s = started.elapsed().as_secs_f64();
        clear_times.push(clear_s);
        let out = child.wait_with_output().unwrap();
        let line = String::from_utf8(out.stdout).unwrap();
        // "ok <t_start> <t_commit>" or "timeout <t_start> <t_end>"
        let parts: Vec<&str> = line.split_whitespace().collect();
        let (t0, t1): (f64, f64) = (parts[1].parse().unwrap(), parts[2].parse().unwrap());
        let wait_ms = (t1 - t0) * 1000.0;
        let note;
        if parts[0] == "ok" {
            waits.push(wait_ms);
            if t1 < clear_end {
                in_during += 1;
                note = "in during clear";
            } else {
                after_clear += 1;
                note = if t0 >= clear_end { "started after clear (window too long)" } else { "waited out the clear" };
            }
        } else {
            timeouts += 1;
            note = "TIMEOUT";
        }
        let late: bool = store.conn.query_row("SELECT EXISTS(SELECT 1 FROM cache WHERE key = 'late-write')", [], |r| r.get(0)).unwrap();
        let left = store.count().unwrap();
        survived += usize::from(late && left == 1);
        if args.flag("--verbose") {
            println!(
                "  trial {trial}: removed={removed} chunks={chunks} clear={clear_s:.3}s writer start +{:.0}ms wait={wait_ms:.1}ms {note} late_survived={late} rows_left={left}",
                (t0 - started_wall) * 1000.0
            );
        }
    }
    waits.sort_by(f64::total_cmp);
    clear_times.sort_by(f64::total_cmp);
    let pct = |v: &[f64], p: f64| if v.is_empty() { f64::NAN } else { v[((v.len() - 1) as f64 * p).round() as usize] };
    println!(
        "rows={rows} chunk={chunk} autockpt={} pause_us={} writer={writer_policy}{} clearer={} | in_during={in_during}/{trials} \
         after={after_clear} timeouts={timeouts} late_write_survived={survived}/{trials} | writer wait ms p50={:.1} max={:.1} \
         | clear s p50={:.3} max={:.3}",
        args.get("--autockpt").unwrap_or("1000"),
        pause.as_micros(),
        if writer_policy == "poll" { format!("({poll_us}us)") } else { String::new() },
        args.get("--clearer").unwrap_or("default"),
        pct(&waits, 0.5),
        pct(&waits, 1.0),
        pct(&clear_times, 0.5),
        pct(&clear_times, 1.0),
    );
}

fn writer(args: &Args, dir: &Path) {
    let poll_us: u64 = args.num("--poll-us", 1000);
    let p = policy(args.get("--writer").unwrap_or("poll"), Duration::from_secs(1), poll_us);
    let delay = Duration::from_micros(args.num("--delay-us", 0));
    let (store, _) = Store::open(dir, THRESHOLD, p).unwrap();
    touch(&dir.join("ready"));
    assert!(wait_for(&dir.join("clear-started"), Duration::from_secs(30)));
    std::thread::sleep(delay);
    let t0 = now();
    match store.set("late-write", b"written during the clear") {
        Ok(()) => println!("ok {t0} {}", now()),
        Err(_) => println!("timeout {t0} {}", now()),
    }
}
