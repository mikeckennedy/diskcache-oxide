//! Model-checked interleavings of the write lock and the slot counter (`cfg(shuttle)` only).

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;

use shuttle::sync::atomic::{AtomicBool, AtomicUsize};
use shuttle::thread;

use super::*;
use crate::model::{Reach, check};
use crate::sync::now;

fn far() -> Instant {
    now() + Duration::from_secs(10)
}

/// Counts threads inside a critical section; entering when it isn't empty is a violation.
fn enter(inside: &AtomicUsize, max: usize, what: &str) {
    let before = inside.fetch_add(1, Ordering::SeqCst);
    assert!(before < max, "{what}: {} inside, the limit is {max}", before + 1);
}

fn assert_idle(lock: &WriteLock) {
    assert_eq!(lock.state(), 0, "the lock was left held");
    assert_eq!(lock.waiters(), 0, "a waiter count leaked");
    assert_eq!(lock.starving_now(), 0, "a starving count leaked");
}

/// Model 1: mutual exclusion and no leaked counts, with one waiter that gives up.
#[test]
fn model_write_lock_excludes_and_counts_balance() {
    check(|| {
        let lock = Arc::new(WriteLock::new());
        let inside = Arc::new(AtomicUsize::new(0));
        let threads: Vec<_> = (0..3)
            .map(|i| {
                let (lock, inside) = (Arc::clone(&lock), Arc::clone(&inside));
                thread::spawn(move || {
                    for _ in 0..2 {
                        // Thread 2 has a deadline short enough to pass while it waits.
                        let deadline = if i == 2 { now() + POLL * 2 } else { far() };
                        let Some(token) = lock.lock_until(deadline) else {
                            assert_eq!(i, 2, "a far deadline passed");
                            continue;
                        };
                        enter(&inside, 1, "write lock");
                        thread::yield_now();
                        inside.fetch_sub(1, Ordering::SeqCst);
                        drop(token);
                    }
                })
            })
            .collect();
        for t in threads {
            t.join().unwrap();
        }
        assert_idle(&lock);
    });
}

/// Model 1b: a thread retaking the lock in a tight loop can't starve a waiter. Counted, not timed:
/// logical time only passes while someone sleeps, so a holder the scheduler leaves idle looks slow.
/// Once the waiter is starving, `try_lock` may succeed at most once more (a call that read
/// `starving` just before it rose), and then the waiter gets the lock.
#[test]
fn model_a_hammering_thread_cannot_starve_a_waiter() {
    static STARVED: Reach = Reach::new();
    check(|| {
        let lock = Arc::new(WriteLock::new());
        let done = Arc::new(AtomicBool::new(false));
        let barged = Arc::new(AtomicUsize::new(0));
        let hammer = {
            let (lock, done, barged) = (Arc::clone(&lock), Arc::clone(&done), Arc::clone(&barged));
            thread::spawn(move || {
                while !done.load(Ordering::SeqCst) {
                    if lock.starving_now() != 0 {
                        STARVED.hit();
                    }
                    if let Some(token) = lock.try_lock() {
                        if lock.starving_now() != 0 {
                            barged.fetch_add(1, Ordering::SeqCst);
                        }
                        thread::yield_now(); // one SQLite write
                        drop(token);
                    }
                    thread::yield_now();
                }
            })
        };
        let token = lock.lock_until(far()).expect("the waiter timed out");
        drop(token);
        done.store(true, Ordering::SeqCst);
        hammer.join().unwrap();
        let barged = barged.load(Ordering::SeqCst);
        assert!(barged <= 1, "try_lock got in {barged} times past a starving waiter");
        assert_idle(&lock);
    });
    STARVED.assert_reached("the waiter starving while the hammer runs");
}

/// Model 2: a nested writer racing a starving waiter never makes two holders.
#[test]
fn model_nested_writer_and_starving_waiter_never_both_hold() {
    check(|| {
        let lock = Arc::new(WriteLock::new());
        let inside = Arc::new(AtomicUsize::new(0));
        let first = lock.try_lock().expect("a fresh lock is free");
        let waiter = {
            let (lock, inside) = (Arc::clone(&lock), Arc::clone(&inside));
            thread::spawn(move || {
                let token = lock.lock_until_with_priority(far()).expect("the priority waiter timed out");
                enter(&inside, 1, "priority waiter");
                thread::yield_now();
                inside.fetch_sub(1, Ordering::SeqCst);
                drop(token);
            })
        };
        let nested = {
            let (lock, inside) = (Arc::clone(&lock), Arc::clone(&inside));
            thread::spawn(move || {
                for _ in 0..3 {
                    if let Some(token) = lock.try_lock_nested() {
                        enter(&inside, 1, "nested writer");
                        thread::yield_now();
                        inside.fetch_sub(1, Ordering::SeqCst);
                        drop(token);
                    }
                    thread::yield_now();
                }
            })
        };
        thread::yield_now();
        drop(first);
        waiter.join().unwrap();
        nested.join().unwrap();
        assert_idle(&lock);
    });
}

/// Model 3: never more than `cap` slots in use; every waiter gets one; the count returns to 0.
#[test]
fn model_slots_never_exceed_the_cap() {
    check(|| {
        let slots = Arc::new(Slots::new());
        let inside = Arc::new(AtomicUsize::new(0));
        let threads: Vec<_> = (0..4)
            .map(|_| {
                let (slots, inside) = (Arc::clone(&slots), Arc::clone(&inside));
                thread::spawn(move || {
                    let permit = slots.acquire_until(2, far()).expect("a slot waiter timed out");
                    enter(&inside, 2, "slots");
                    thread::yield_now();
                    inside.fetch_sub(1, Ordering::SeqCst);
                    drop(permit);
                })
            })
            .collect();
        for t in threads {
            t.join().unwrap();
        }
        assert_eq!(slots.in_use(), 0);
    });
}

/// Model 4: a holder releases while a waiter is mid-spin and a third thread barges in. The cap is
/// never exceeded, a far-deadline waiter gets in once the bargers stop, and the count returns to 0.
/// Shuttle checks bounded schedules, so this shows the waiter isn't shut out by a bounded barger,
/// not liveness against an unbounded one.
#[test]
fn model_slot_wait_survives_a_barger_and_a_release_mid_spin() {
    use tests_support::{Phase, current_phase, phase};
    static RELEASED_MID_SPIN: Reach = Reach::new();
    static BARGED: Reach = Reach::new();
    check(|| {
        phase(Phase::Attached);
        let slots = Arc::new(Slots::new());
        let inside = Arc::new(AtomicUsize::new(0));
        let held = slots.try_acquire(1).expect("a fresh counter has room");
        let waiter = {
            let (slots, inside) = (Arc::clone(&slots), Arc::clone(&inside));
            thread::spawn(move || {
                let permit = slots.acquire_until(1, far()).expect("a far-deadline waiter timed out");
                enter(&inside, 1, "waiter");
                thread::yield_now();
                inside.fetch_sub(1, Ordering::SeqCst);
                drop(permit);
            })
        };
        let barger = {
            let (slots, inside) = (Arc::clone(&slots), Arc::clone(&inside));
            thread::spawn(move || {
                for _ in 0..3 {
                    if let Some(permit) = slots.try_acquire(1) {
                        if current_phase() != Phase::Attached {
                            BARGED.hit();
                        }
                        enter(&inside, 1, "barger");
                        thread::yield_now();
                        inside.fetch_sub(1, Ordering::SeqCst);
                        drop(permit);
                    }
                    thread::yield_now();
                }
            })
        };
        thread::yield_now();
        if current_phase() == Phase::Spin {
            RELEASED_MID_SPIN.hit();
        }
        drop(held);
        waiter.join().unwrap();
        barger.join().unwrap();
        assert_eq!(slots.in_use(), 0, "a slot leaked");
    });
    RELEASED_MID_SPIN.assert_reached("the holder releasing while the waiter spins");
    BARGED.assert_reached("a barger taking the slot while the waiter waits detached");
}

/// Model 5: the deadline passes during the spin phase (another thread's sleep moves the
/// clock). The wait returns `None`, and nothing leaks: only the holder's slot is in use.
#[test]
fn model_slot_wait_times_out_during_the_spin() {
    use tests_support::{Phase, current_phase, phase};
    static EXPIRED_MID_SPIN: Reach = Reach::new();
    check(|| {
        phase(Phase::Attached);
        let slots = Arc::new(Slots::new());
        let held = slots.try_acquire(1).expect("a fresh counter has room");
        let deadline = now() + POLL;
        let waiter = {
            let slots = Arc::clone(&slots);
            thread::spawn(move || {
                assert!(slots.acquire_until(1, deadline).is_none(), "got a slot that was held throughout");
            })
        };
        let clock = thread::spawn(move || {
            for _ in 0..2 {
                crate::sync::thread::sleep(POLL);
                if now() >= deadline && current_phase() == Phase::Spin {
                    EXPIRED_MID_SPIN.hit();
                }
            }
        });
        waiter.join().unwrap();
        clock.join().unwrap();
        assert_eq!(slots.in_use(), 1, "the timed-out wait left a count behind");
        drop(held);
        assert_eq!(slots.in_use(), 0);
    });
    EXPIRED_MID_SPIN.assert_reached("the deadline passing while the waiter spins");
}
