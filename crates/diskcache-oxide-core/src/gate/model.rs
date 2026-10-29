//! Model-checked interleavings of the fork gate (`cfg(shuttle)` only). Each execution gets a fresh
//! leaked gate (`begin_fork` needs `&'static`); the shuttle thread-local gives each modelled thread a
//! fresh `ThreadState`. No blocker is installed, so waits run directly.

use std::sync::Arc;
use std::sync::atomic::Ordering;

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
                        let outer = gate.enter_shared();
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
                    let outer = gate.enter_shared();
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
                let g = gate.enter_shared();
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
                let a = gate.enter_shared().into_held();
                let b = gate.enter_shared().into_held();
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
                let g = gate.enter_shared();
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
                    let deadline = crate::sync::now() + std::time::Duration::from_secs(10);
                    let token = lock.lock_until(deadline).expect("write lock timed out");
                    let permit = slots.acquire_until(1, deadline).expect("slot timed out");
                    let g = gate.enter_shared();
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
