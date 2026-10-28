//! Times the phases of a spilled set (write tmp, BEGIN..rename..upsert..COMMIT, unlink old), to explain
//! where SIGKILLs land in Experiment 2.
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use storage_spike::{Args, Rng, Store, WaitPolicy, fresh_dir, make_blob};

fn main() {
    let args = Args::from_env();
    let dir = PathBuf::from(args.get("--dir").expect("--dir"));
    let threshold: usize = args.num("--threshold", 32 * 1024);
    fresh_dir(&dir);
    let (store, _) = Store::open(&dir, threshold, WaitPolicy::Default(Duration::from_secs(5))).unwrap();
    let mut rng = Rng::new(5);
    let n = 2000;
    let (mut total, mut write, mut unlink) = (0.0, 0.0, 0.0);
    // whole set() timing
    for i in 0..n {
        let v = make_blob("k", i, &mut rng, 2 * threshold, threshold);
        let t = Instant::now();
        store.set(&format!("k{}", i % 6), &v).unwrap();
        total += t.elapsed().as_secs_f64();
    }
    // file write alone and unlink alone, same sizes
    fs::create_dir_all(dir.join("probe")).unwrap();
    for i in 0..n {
        let v = make_blob("k", i, &mut rng, 2 * threshold, threshold);
        let p = dir.join("probe").join(format!("{i}.tmp"));
        let t = Instant::now();
        fs::File::create_new(&p).unwrap().write_all(&v).unwrap();
        write += t.elapsed().as_secs_f64();
        let t = Instant::now();
        fs::remove_file(&p).unwrap();
        unlink += t.elapsed().as_secs_f64();
    }
    let us = |x: f64| x / n as f64 * 1e6;
    println!(
        "threshold={threshold}: set total {:.0} us | tmp write {:.0} us | unlink {:.0} us | remainder (lock, rename, upsert, commit, blob gen excluded) {:.0} us",
        us(total), us(write), us(unlink), us(total - write - unlink)
    );
}
