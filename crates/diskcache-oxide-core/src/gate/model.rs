//! Model-checked interleavings of the fork gate (`cfg(shuttle)` only). Each execution gets a fresh
//! leaked gate (`begin_fork` needs `&'static`); the shuttle thread-local gives each modelled thread a
//! fresh `ThreadState`. No blocker is installed, so waits run directly.

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Instant;

use shuttle::sync::atomic::{AtomicBool, AtomicUsize};
use shuttle::sync::mpsc;
use shuttle::thread;

use super::*;
use crate::model::{Reach, check};

crate::sync::thread_local! {
    static LOCAL: ThreadState = const { ThreadState::new() };
}

std::thread_local! {
    // One gate per OS thread (each execution runs on one), reset at the start of each execution.
    static GATE_SLOT: &'static Gate = Box::leak(Box::new(Gate::new(&LOCAL)));
}

fn fresh_gate() -> &'static Gate {
    let gate = GATE_SLOT.with(|g| *g);
    gate.reset_for_model();
    gate
}

/// What the model knows independently of the gate's own counters.
struct Witness {
    /// Counts forks that had to wait for another thread's running level (not vacuous).
    waited: &'static Reach,
    /// Threads running "SQLite work" at depth >= 1, counted after entry and before exit, so it never
    /// exceeds the real holders: non-zero while a fork is exclusive is a violation.
    running: AtomicUsize,
    /// Forks between `begin_fork` returning and `end_parent`.
    exclusive: AtomicUsize,
}

impl Witness {
    fn new(waited: &'static Reach) -> Arc<Self> {
        Arc::new(Self { waited, running: AtomicUsize::new(0), exclusive: AtomicUsize::new(0) })
    }

    fn start_work(&self) {
        self.running.fetch_add(1, Ordering::SeqCst);
        assert_eq!(self.exclusive.load(Ordering::SeqCst), 0, "a level was entered while a fork was exclusive");
    }
    fn end_work(&self) {
        self.running.fetch_sub(1, Ordering::SeqCst);
    }
    /// `own` = this forker's own running count (it may fork inside its own levels).
    fn fork(&self, gate: &'static Gate, own: usize) {
        if self.running.load(Ordering::SeqCst) > own {
            self.waited.hit();
        }
        let guard = gate.begin_fork();
        assert_eq!(self.exclusive.fetch_add(1, Ordering::SeqCst), 0, "two forks were exclusive at once");
        assert_eq!(self.running.load(Ordering::SeqCst), own, "begin_fork returned while another thread was working");
        thread::yield_now(); // os.fork()
        self.exclusive.fetch_sub(1, Ordering::SeqCst);
        guard.end_parent();
    }
}

fn assert_idle(gate: &Gate) {
    assert_eq!(gate.holders(), 0, "a level leaked");
    assert_eq!(gate.parked(), 0, "parked didn't balance");
    assert!(!gate.pending.load(Ordering::SeqCst), "the claim was left set");
}

/// Model 4: nested entrants and one forker. The fork waits for running levels, blocks fresh entries,
/// and never blocks a nested one.
#[test]
fn model_fork_excludes_entrants_and_nested_entry_passes() {
    static WAITED: Reach = Reach::new();
    check(|| {
        let gate = fresh_gate();
        let w = Witness::new(&WAITED);
        let entrants: Vec<_> = (0..2)
            .map(|i| {
                let w = Arc::clone(&w);
                thread::spawn(move || {
                    for _ in 0..2 {
                        let outer = gate.enter_shared_blocking();
                        w.start_work();
                        let inner = gate.try_enter_shared().expect("a nested entry was refused");
                        // Thread 1 keeps its inner level as a held share (a held transaction).
                        let held = (i == 1).then(|| inner.into_held());
                        thread::yield_now();
                        assert!(gate.try_enter_shared().is_some(), "a reentrant entry was refused");
                        w.end_work();
                        drop(held);
                        drop(outer);
                    }
                })
            })
            .collect();
        let forker = {
            let w = Arc::clone(&w);
            thread::spawn(move || w.fork(gate, 0))
        };
        for t in entrants {
            t.join().unwrap();
        }
        forker.join().unwrap();
        assert_idle(gate);
    });
    WAITED.assert_reached("a fork that waited for a running level");
}

/// Model 5: two forkers that each hold their own levels (D-020) take turns and don't deadlock,
/// with a plain entrant alongside.
#[test]
fn model_two_forkers_holding_levels_take_turns() {
    static WAITED: Reach = Reach::new();
    check(|| {
        let gate = fresh_gate();
        let w = Witness::new(&WAITED);
        let forkers: Vec<_> = (0..2)
            .map(|_| {
                let w = Arc::clone(&w);
                thread::spawn(move || {
                    let outer = gate.enter_shared_blocking();
                    let held = gate.try_enter_shared().expect("nested").into_held();
                    // Parked or forking, this thread runs Python, not SQLite: not `running`.
                    w.fork(gate, 0);
                    drop(held);
                    drop(outer);
                })
            })
            .collect();
        let entrant = {
            let w = Arc::clone(&w);
            thread::spawn(move || {
                let g = gate.enter_shared_blocking();
                w.start_work();
                thread::yield_now();
                w.end_work();
                drop(g);
            })
        };
        for t in forkers {
            t.join().unwrap();
        }
        entrant.join().unwrap();
        assert_idle(gate);
    });
    WAITED.assert_reached("a fork that waited for a running level");
}

/// Model 6: a held share dropped on another thread at any point while its owner parks behind a
/// second forker's claim. The documented transient may make `parked` too low (the fork waits
/// longer), never too high (the fork would pass a running level), and everything balances.
#[test]
fn model_a_share_dropped_elsewhere_while_its_owner_parks() {
    static DROPPED_PARKED: Reach = Reach::new();
    static WAITED: Reach = Reach::new();
    check(|| {
        let gate = fresh_gate();
        let w = Witness::new(&WAITED);
        let (tx, rx) = mpsc::channel::<HeldShare<'static>>();
        let owner = {
            let w = Arc::clone(&w);
            thread::spawn(move || {
                let a = gate.enter_shared_blocking().into_held();
                let b = gate.enter_shared_blocking().into_held();
                tx.send(a).unwrap();
                w.fork(gate, 0);
                drop(b);
            })
        };
        let dropper = thread::spawn(move || {
            let share = rx.recv().unwrap();
            if gate.parked() != 0 {
                DROPPED_PARKED.hit();
            }
            drop(share);
        });
        let other = {
            let w = Arc::clone(&w);
            thread::spawn(move || w.fork(gate, 0))
        };
        let worker = {
            let w = Arc::clone(&w);
            thread::spawn(move || {
                let g = gate.enter_shared_blocking();
                w.start_work();
                thread::yield_now();
                w.end_work();
                drop(g);
            })
        };
        for t in [owner, dropper, other, worker] {
            t.join().unwrap();
        }
        assert_idle(gate);
    });
    DROPPED_PARKED.assert_reached("a share dropped while its owner was parked");
    WAITED.assert_reached("a fork that waited for a running level");
}

/// Model 8: a share ended on a thread other than its owner while a fork waits for it (a held
/// transaction finished by a GC finalizer). The ender enters on the share's level, so it never waits
/// for the fork, and the fork still never runs alongside the ender's work.
#[test]
fn model_a_share_ended_on_another_thread_while_a_fork_waits() {
    static ENTERED_PENDING: Reach = Reach::new();
    static WAITED: Reach = Reach::new();
    check(|| {
        let gate = fresh_gate();
        let w = Witness::new(&WAITED);
        let (tx, rx) = mpsc::channel::<HeldShare<'static>>();
        let owner = thread::spawn(move || tx.send(gate.enter_shared_blocking().into_held()).unwrap());
        let ender = {
            let w = Arc::clone(&w);
            thread::spawn(move || {
                let share = rx.recv().unwrap();
                if gate.pending.load(Ordering::SeqCst) {
                    ENTERED_PENDING.hit();
                }
                let g = share.enter();
                w.start_work();
                thread::yield_now();
                w.end_work();
                drop(g);
                drop(share);
            })
        };
        let forker = {
            let w = Arc::clone(&w);
            thread::spawn(move || w.fork(gate, 0))
        };
        for t in [owner, ender, forker] {
            t.join().unwrap();
        }
        assert_idle(gate);
    });
    ENTERED_PENDING.assert_reached("a share entered while a fork was pending");
    WAITED.assert_reached("a fork that waited for a running level");
}

/// Model 9: as model 8, but the share's owner is the forker, whose fork counts the share as its own
/// and so doesn't wait for it. The ender must then wait for the fork, never work beside it.
#[test]
fn model_a_share_ended_elsewhere_while_its_owner_forks() {
    static ENTERED_PENDING: Reach = Reach::new();
    static NEVER_WAITED: Reach = Reach::new();
    check(|| {
        let gate = fresh_gate();
        let w = Witness::new(&NEVER_WAITED);
        let (tx, rx) = mpsc::channel::<HeldShare<'static>>();
        let owner = {
            let w = Arc::clone(&w);
            thread::spawn(move || {
                tx.send(gate.enter_shared_blocking().into_held()).unwrap();
                w.fork(gate, 0);
            })
        };
        let ender = {
            let w = Arc::clone(&w);
            thread::spawn(move || {
                let share = rx.recv().unwrap();
                if gate.pending.load(Ordering::SeqCst) {
                    ENTERED_PENDING.hit();
                }
                let g = share.enter();
                w.start_work();
                thread::yield_now();
                w.end_work();
                drop(g);
                drop(share);
            })
        };
        for t in [owner, ender] {
            t.join().unwrap();
        }
        assert_idle(gate);
    });
    ENTERED_PENDING.assert_reached("a share entered while its owner's fork was pending");
}

/// Model 10: the share's owner forks too, and parks behind another forker, whose fork doesn't wait
/// for parked levels. The ender must not work beside either fork.
#[test]
fn model_a_share_ended_elsewhere_while_its_owner_parks() {
    static ENTERED_PARKED: Reach = Reach::new();
    static WAITED: Reach = Reach::new();
    check(|| {
        let gate = fresh_gate();
        let w = Witness::new(&WAITED);
        let (tx, rx) = mpsc::channel::<HeldShare<'static>>();
        let owner = {
            let w = Arc::clone(&w);
            thread::spawn(move || {
                tx.send(gate.enter_shared_blocking().into_held()).unwrap();
                w.fork(gate, 0);
            })
        };
        let ender = {
            let w = Arc::clone(&w);
            thread::spawn(move || {
                let share = rx.recv().unwrap();
                if gate.parked() != 0 {
                    ENTERED_PARKED.hit();
                }
                let g = share.enter();
                w.start_work();
                thread::yield_now();
                w.end_work();
                drop(g);
                drop(share);
            })
        };
        let other = {
            let w = Arc::clone(&w);
            thread::spawn(move || w.fork(gate, 0))
        };
        for t in [owner, ender, other] {
            t.join().unwrap();
        }
        assert_idle(gate);
    });
    ENTERED_PARKED.assert_reached("a share entered while its owner was parked");
}

/// Model 7: the composition, as a store write runs it. Write lock (never waited for inside the
/// gate) then gate, a nested write at depth 1 via `try_lock_nested`, a slot taken before the gate,
/// and a forker. Checks the lock-order rules hold up together: no deadlock, write exclusion,
/// fork exclusion.
#[test]
fn model_writes_slots_and_a_fork_compose() {
    static WAITED: Reach = Reach::new();
    use crate::genlock::{Slots, WriteLock};
    check(|| {
        let gate = fresh_gate();
        let lock = Arc::new(WriteLock::new());
        let slots = Arc::new(Slots::new());
        let w = Witness::new(&WAITED);
        let writing = Arc::new(AtomicBool::new(false));
        let writers: Vec<_> = (0..2)
            .map(|_| {
                let (lock, slots, w, writing) =
                    (Arc::clone(&lock), Arc::clone(&slots), Arc::clone(&w), Arc::clone(&writing));
                thread::spawn(move || {
                    let deadline = now() + std::time::Duration::from_secs(10);
                    let token = lock.lock_until(deadline).expect("write lock timed out");
                    let permit = slots.acquire_until(1, deadline).expect("slot timed out");
                    let g = gate.enter_shared_blocking();
                    w.start_work();
                    assert!(!writing.swap(true, Ordering::SeqCst), "two writers at once");
                    // A nested write inside: the lock is ours, so it must not get it again.
                    assert!(lock.try_lock_nested().is_none(), "a nested write took a held lock");
                    writing.store(false, Ordering::SeqCst);
                    w.end_work();
                    drop(g);
                    drop(permit);
                    drop(token);
                })
            })
            .collect();
        let forker = {
            let w = Arc::clone(&w);
            thread::spawn(move || w.fork(gate, 0))
        };
        for t in writers {
            t.join().unwrap();
        }
        forker.join().unwrap();
        assert_idle(gate);
        assert_eq!(lock.waiters(), 0);
        assert_eq!(slots.in_use(), 0);
    });
    WAITED.assert_reached("a fork that waited for a running level");
}

/// A deadline a few waiter polls away, on the logical clock.
fn soon(polls: u32) -> Instant {
    now() + WAITER_POLL * polls
}

/// Model 11: the cycle. A holder waits on a channel for an entrant, and a forker waits for the
/// holder. It must finish: with an unbounded entry, `begin_fork`'s poll keeps the logical clock
/// moving until the `LIVELOCK` assert trips.
#[test]
fn model_a_bounded_entry_breaks_the_cycle() {
    static TIMED_OUT_PENDING: Reach = Reach::new();
    check(|| {
        let gate = fresh_gate();
        let (held_tx, held_rx) = mpsc::channel::<()>();
        let (result_tx, result_rx) = mpsc::channel::<()>();
        let holder = thread::spawn(move || {
            let guard = gate.enter_shared_blocking();
            held_tx.send(()).unwrap();
            result_rx.recv().unwrap();
            drop(guard);
        });
        let forker = thread::spawn(move || {
            held_rx.recv().unwrap();
            gate.begin_fork().end_parent();
        });
        let entrant = thread::spawn(move || {
            let deadline = soon(3);
            let entered = gate.enter_shared_until(deadline);
            if entered.is_none() {
                assert!(now() >= deadline, "gave up before its deadline");
                if gate.pending.load(Ordering::SeqCst) {
                    TIMED_OUT_PENDING.hit();
                }
            }
            drop(entered);
            result_tx.send(()).unwrap();
        });
        for t in [holder, forker, entrant] {
            t.join().unwrap();
        }
        assert_idle(gate);
    });
    TIMED_OUT_PENDING.assert_reached("an entry that timed out with a fork pending");
}

/// Model 12: the race at the deadline, with no stuck holder. An entry either gets in after waiting
/// or times out, never runs beside an exclusive fork, and leaks nothing.
#[test]
fn model_the_race_at_the_deadline() {
    static WAITED: Reach = Reach::new();
    static ENTERED_AFTER_WAITING: Reach = Reach::new();
    static TIMED_OUT: Reach = Reach::new();
    check(|| {
        let gate = fresh_gate();
        let w = Witness::new(&WAITED);
        let forker = {
            let w = Arc::clone(&w);
            thread::spawn(move || w.fork(gate, 0))
        };
        let entrant = {
            let w = Arc::clone(&w);
            thread::spawn(move || {
                let was_pending = gate.pending.load(Ordering::SeqCst);
                let deadline = soon(2);
                if let Some(guard) = gate.enter_shared_until(deadline) {
                    if was_pending {
                        ENTERED_AFTER_WAITING.hit();
                    }
                    w.start_work();
                    thread::yield_now();
                    w.end_work();
                    drop(guard);
                } else {
                    assert!(now() >= deadline, "gave up before its deadline");
                    TIMED_OUT.hit();
                }
            })
        };
        forker.join().unwrap();
        entrant.join().unwrap();
        assert_idle(gate);
    });
    ENTERED_AFTER_WAITING.assert_reached("an entry that got in after waiting");
    TIMED_OUT.assert_reached("an entry that timed out");
}

/// Model 13: a past deadline never refuses a thread with levels of its own, a held share, or the
/// forking thread itself.
#[test]
fn model_a_thread_with_a_level_never_times_out() {
    check(|| {
        let gate = fresh_gate();
        let (held_tx, held_rx) = mpsc::channel::<()>();
        let holder = thread::spawn(move || {
            let outer = gate.enter_shared_blocking();
            let share = gate.try_enter_shared().expect("nested").into_held();
            held_tx.send(()).unwrap();
            assert!(gate.enter_shared_until(now()).is_some(), "depth > 0 timed out");
            drop(outer);
            assert!(gate.enter_shared_until(now()).is_some(), "a held share's owner timed out");
            drop(share);
        });
        let forker = thread::spawn(move || {
            held_rx.recv().unwrap();
            let guard = gate.begin_fork();
            assert!(gate.enter_shared_until(now()).is_some(), "the forking thread timed out");
            guard.end_parent();
        });
        holder.join().unwrap();
        forker.join().unwrap();
        assert_idle(gate);
        assert_eq!(gate.stats().fork_timeouts, 0);
    });
}

/// Model 14: two forkers holding levels, plus a bounded entrant. Parked balances, and nothing
/// deadlocks.
#[test]
fn model_two_forkers_holding_levels_and_a_bounded_entrant() {
    static WAITED: Reach = Reach::new();
    static TIMED_OUT: Reach = Reach::new();
    check(|| {
        let gate = fresh_gate();
        let w = Witness::new(&WAITED);
        let forkers: Vec<_> = (0..2)
            .map(|_| {
                let w = Arc::clone(&w);
                thread::spawn(move || {
                    let outer = gate.enter_shared_blocking();
                    let held = gate.try_enter_shared().expect("nested").into_held();
                    w.fork(gate, 0);
                    drop(held);
                    drop(outer);
                })
            })
            .collect();
        let entrant = {
            let w = Arc::clone(&w);
            thread::spawn(move || {
                if let Some(guard) = gate.enter_shared_until(soon(2)) {
                    w.start_work();
                    thread::yield_now();
                    w.end_work();
                    drop(guard);
                } else {
                    TIMED_OUT.hit();
                }
            })
        };
        for t in forkers {
            t.join().unwrap();
        }
        entrant.join().unwrap();
        assert_idle(gate);
    });
    WAITED.assert_reached("a fork that waited for a running level");
    TIMED_OUT.assert_reached("an entry that timed out");
}

/// Model 15: model 7 with a bounded entrant that also times out holding a slot and the write lock,
/// which must come back.
#[test]
fn model_a_bounded_entry_times_out_holding_a_slot_and_the_write_lock() {
    static WAITED: Reach = Reach::new();
    static TIMED_OUT: Reach = Reach::new();
    use crate::genlock::{Slots, WriteLock};
    check(|| {
        let gate = fresh_gate();
        let lock = Arc::new(WriteLock::new());
        let slots = Arc::new(Slots::new());
        let w = Witness::new(&WAITED);
        let writing = Arc::new(AtomicBool::new(false));
        let writers: Vec<_> = (0..2)
            .map(|i| {
                let (lock, slots, w, writing) =
                    (Arc::clone(&lock), Arc::clone(&slots), Arc::clone(&w), Arc::clone(&writing));
                thread::spawn(move || {
                    let deadline = now() + std::time::Duration::from_secs(10);
                    let token = lock.lock_until(deadline).expect("write lock timed out");
                    let permit = slots.acquire_until(1, deadline).expect("slot timed out");
                    // One writer has a short deadline for the gate.
                    let gate_deadline = if i == 0 { soon(2) } else { deadline };
                    if let Some(g) = gate.enter_shared_until(gate_deadline) {
                        w.start_work();
                        assert!(!writing.swap(true, Ordering::SeqCst), "two writers at once");
                        writing.store(false, Ordering::SeqCst);
                        w.end_work();
                        drop(g);
                    } else {
                        TIMED_OUT.hit();
                    }
                    drop(permit);
                    drop(token);
                })
            })
            .collect();
        let forker = {
            let w = Arc::clone(&w);
            thread::spawn(move || w.fork(gate, 0))
        };
        for t in writers {
            t.join().unwrap();
        }
        forker.join().unwrap();
        assert_idle(gate);
        assert_eq!(lock.waiters(), 0);
        assert_eq!(slots.in_use(), 0);
    });
    TIMED_OUT.assert_reached("a writer that timed out at the gate");
}

/// Model 16: the deferred drain. A deferrer and two forkers, each running the drain at its end, and
/// the count must be 0 at the end. A single store-then-load pair passes on the wrong order (clearing
/// the flag before the try), so one forker isn't enough: that order strands the work when fork 1's
/// drain clears the flag, its try fails because fork 2 has claimed, and fork 2's end sees no flag.
#[test]
fn model_a_deferred_release_is_always_drained() {
    static CLAIM_BEAT_THE_DRAIN: Reach = Reach::new();
    check(|| {
        let gate = fresh_gate();
        let count = Arc::new(AtomicUsize::new(0));
        let drain = |gate: &'static Gate, count: &AtomicUsize| {
            let was_set = gate.has_deferred();
            let mut ran = false;
            gate.drain_deferred(|_| {
                ran = true;
                count.swap(0, Ordering::SeqCst);
            });
            if was_set && !ran && gate.pending.load(Ordering::SeqCst) {
                CLAIM_BEAT_THE_DRAIN.hit();
            }
        };
        let forkers: Vec<_> = (0..2)
            .map(|_| {
                let count = Arc::clone(&count);
                thread::spawn(move || {
                    gate.begin_fork().end_parent();
                    drain(gate, &count);
                })
            })
            .collect();
        let deferrer = {
            let count = Arc::clone(&count);
            thread::spawn(move || {
                // As `Store::defer_release`: only behind a pending fork.
                if gate.try_enter_shared().is_none() {
                    gate.defer(
                        || {
                            count.fetch_add(1, Ordering::SeqCst);
                        },
                        || drain(gate, &count),
                    );
                }
            })
        };
        for t in forkers {
            t.join().unwrap();
        }
        deferrer.join().unwrap();
        assert_eq!(count.load(Ordering::SeqCst), 0, "a deferred release was stranded");
        assert_idle(gate);
    });
    CLAIM_BEAT_THE_DRAIN.assert_reached("a drain whose try failed because the second fork had claimed");
}
