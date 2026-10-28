//! The process-wide fork gate (D-012) and the process generation.
//!
//! Every SQLite call runs while the calling thread holds the gate in *shared* mode, including
//! opening and closing connections. The before-fork hook takes it *exclusively*: it marks a fork
//! pending, which holds off new shared entries, and waits for the ones in flight to finish. A forked
//! child therefore never inherits a half-finished transaction, or SQLite lock state, from a thread
//! that doesn't exist in the child.
//!
//! # Rules
//!
//! - Shared entry is reentrant per thread: a thread that already holds the gate enters again even
//!   while a fork is pending, so nested operations (an operation inside a held transaction) can't
//!   deadlock behind the fork. Levels kept as a [`HeldShare`] count as the entering thread's until
//!   the share is dropped, on whichever thread that happens.
//! - The gate is the outermost lock. Nothing that blocks may be waited on while it is held shared,
//!   except SQLite's own busy wait; a pool that runs out of connections must release the gate
//!   before it waits for one.
//! - [`Gate::begin_fork`] waits until the only holders left are the calling thread's own levels, so
//!   forking inside your own transaction doesn't deadlock. That transaction is poisoned in the
//!   child (see [`generation`]).
//! - Blocking waits go through the [`Blocker`] installed with [`set_blocker`], so the Python layer
//!   can release the interpreter while a thread waits. The fast path never calls it.
//!
//! # Across `fork()`
//!
//! The gate is atomics and thread-locals only: there is no mutex or condition variable, so a child
//! can't inherit one locked by a thread that no longer exists, or one whose waiter list still names
//! parent threads. The forking thread claims the fork by compare-and-swapping the pending flag, so
//! concurrent forks take turns; the child inherits the flag set, and the after-fork hook clears it
//! in both processes. Every wait is a bounded sleep-poll on those atomics, so nothing depends on a
//! wakeup that a fork could lose, and threads leaving the gate never notify anyone.

use std::cell::{Cell, OnceCell, RefCell};
use std::marker::PhantomData;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, OnceLock};
use std::thread::LocalKey;
use std::time::Duration;

/// How often [`Gate::begin_fork`] re-checks the claim and the holder count. Holders never notify on
/// exit, which keeps the exit path to one atomic decrement.
const FORK_POLL: Duration = Duration::from_millis(1);

/// How often a thread blocked behind a pending fork re-checks it. Nothing wakes it early, so this is
/// also the most a thread can lag behind the end of a fork.
const WAITER_POLL: Duration = Duration::from_millis(1);

/// Runs a blocking wait. The Python layer installs one that releases the interpreter around it.
pub type Blocker = fn(&(dyn Fn() + Sync));

static BLOCKER: OnceLock<Blocker> = OnceLock::new();

/// Installs the function every blocking gate wait runs through. Only the first call has an effect;
/// returns whether this call installed it.
pub fn set_blocker(blocker: Blocker) -> bool {
    BLOCKER.set(blocker).is_ok()
}

/// One thread's view of one gate.
#[derive(Debug)]
pub struct ThreadState {
    /// How many shared levels this thread holds through [`SharedGuard`]s.
    depth: Cell<usize>,
    /// How many levels this thread entered that are now kept as [`HeldShare`]s. Shared with each
    /// share, so one dropped on another thread still takes its level off this thread. Created on the
    /// first [`SharedGuard::into_held`], and read only while a fork is pending.
    held: OnceCell<Arc<AtomicUsize>>,
    /// True on the forking thread from [`Gate::begin_fork`] to the after-fork hook, so code that
    /// runs in between (another library's before-fork hook) can still use the cache.
    forking: Cell<bool>,
}

impl ThreadState {
    #[must_use]
    pub const fn new() -> Self {
        Self { depth: Cell::new(0), held: OnceCell::new(), forking: Cell::new(false) }
    }

    fn held_levels(&self) -> usize {
        self.held.get().map_or(0, |held| held.load(Ordering::SeqCst))
    }
}

impl Default for ThreadState {
    fn default() -> Self {
        Self::new()
    }
}

thread_local! {
    static GLOBAL_THREAD: ThreadState = const { ThreadState::new() };
}

/// The process-wide gate every store uses.
pub static GATE: Gate = Gate::new(&GLOBAL_THREAD);

/// Bumped in the forked child. Anything tagged with an older generation was inherited from the
/// parent and must be dropped without being closed (`mem::forget`), never used.
static GENERATION: AtomicU64 = AtomicU64::new(0);

/// The current process generation (0 in a process that was never forked from a cache user).
#[must_use]
pub fn generation() -> u64 {
    GENERATION.load(Ordering::Acquire)
}

/// A reentrant shared/exclusive gate whose exclusive side survives `fork()`. See the module docs.
#[derive(Debug)]
pub struct Gate {
    /// Shared levels held, counted across all threads (a thread at depth 2 counts twice).
    holders: AtomicUsize,
    /// A fork is claimed: set by [`Gate::begin_fork`], cleared when its [`ForkGuard`] ends. Also
    /// what makes concurrent forks take turns.
    pending: AtomicBool,
    local: &'static LocalKey<ThreadState>,
}

impl Gate {
    /// A gate whose per-thread state lives in `local`. Each gate needs its own thread-local.
    #[must_use]
    pub const fn new(local: &'static LocalKey<ThreadState>) -> Self {
        Self { holders: AtomicUsize::new(0), pending: AtomicBool::new(false), local }
    }

    /// Enters in shared mode without waiting. Returns `None` only if a fork is pending and this
    /// thread holds no level yet.
    pub fn try_enter_shared(&self) -> Option<SharedGuard<'_>> {
        self.local.with(|t| {
            let depth = t.depth.get();
            // Increment, then check: paired with begin_fork's store-then-load, either we see the
            // pending flag or begin_fork sees our level. The held levels are read last, so only
            // while a fork is pending.
            self.holders.fetch_add(1, Ordering::SeqCst);
            if depth == 0 && !t.forking.get() && self.pending.load(Ordering::SeqCst) && t.held_levels() == 0 {
                self.holders.fetch_sub(1, Ordering::SeqCst);
                return None;
            }
            t.depth.set(depth + 1);
            Some(SharedGuard { gate: self, _not_send: PhantomData })
        })
    }

    /// Enters in shared mode, waiting on this thread (attached to nothing) while a fork is pending.
    pub fn enter_shared_blocking(&self) -> SharedGuard<'_> {
        loop {
            if let Some(guard) = self.try_enter_shared() {
                return guard;
            }
            self.wait_while_pending();
        }
    }

    /// Enters in shared mode, running any wait through the installed [`Blocker`].
    pub fn enter_shared(&self) -> SharedGuard<'_> {
        loop {
            if let Some(guard) = self.try_enter_shared() {
                return guard;
            }
            match BLOCKER.get() {
                Some(blocker) => blocker(&|| self.wait_while_pending()),
                None => self.wait_while_pending(),
            }
        }
    }

    /// Sleeps until no fork is pending. Call it with the interpreter released, so the forking
    /// thread (and a free-threaded stop-the-world) isn't held up by the waiter.
    pub fn wait_while_pending(&self) {
        while self.pending.load(Ordering::SeqCst) {
            std::thread::sleep(WAITER_POLL);
        }
    }

    /// The shared levels the calling thread holds, including those kept as [`HeldShare`]s.
    #[must_use]
    pub fn own_depth(&self) -> usize {
        self.local.with(|t| t.depth.get() + t.held_levels())
    }

    /// Takes the gate exclusively: claims the fork (waiting for another thread's to end), blocks
    /// new shared entries and waits, with no timeout, until the calling thread's own levels are the
    /// only ones left. Call it with the interpreter released.
    ///
    /// The returned guard keeps the claim until it is ended with [`ForkGuard::end_parent`] or
    /// [`ForkGuard::end_child`] (dropping it counts as the parent).
    pub fn begin_fork(&'static self) -> ForkGuard {
        // SeqCst on success pairs with try_enter_shared's increment-then-check.
        while self.pending.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() {
            std::thread::sleep(FORK_POLL);
        }
        let own = self.own_depth();
        while self.holders.load(Ordering::SeqCst) > own {
            std::thread::sleep(FORK_POLL);
        }
        self.local.with(|t| t.forking.set(true));
        ForkGuard { gate: self, active: true, _not_send: PhantomData }
    }
}

/// One shared level of a [`Gate`], held by the current thread. Not `Send`: the level belongs to the
/// thread that entered.
#[derive(Debug)]
#[must_use = "the gate is released as soon as the guard is dropped"]
pub struct SharedGuard<'g> {
    gate: &'g Gate,
    _not_send: PhantomData<*const ()>,
}

impl<'g> SharedGuard<'g> {
    /// Turns the guard into a `Send` token for a level that must outlive the current Rust scope
    /// (a transaction held open across Python calls). The level still counts as the entering
    /// thread's own until the token is dropped, on any thread.
    #[must_use = "dropping the token releases the level at once"]
    pub fn into_held(self) -> HeldShare<'g> {
        let held = self.gate.local.with(|t| {
            let held = Arc::clone(t.held.get_or_init(|| Arc::new(AtomicUsize::new(0))));
            // Move the level from the depth to the held count; only this thread reads either.
            held.fetch_add(1, Ordering::SeqCst);
            t.depth.set(t.depth.get().saturating_sub(1));
            held
        });
        let share = HeldShare { gate: self.gate, held };
        std::mem::forget(self);
        share
    }
}

impl Drop for SharedGuard<'_> {
    fn drop(&mut self) {
        // try_with: a guard dropped while this thread's locals are being destroyed still releases.
        let _ = self.gate.local.try_with(|t| t.depth.set(t.depth.get().saturating_sub(1)));
        self.gate.holders.fetch_sub(1, Ordering::SeqCst);
    }
}

/// A shared level kept across Rust scopes, see [`SharedGuard::into_held`]. It may be dropped on any
/// thread (on free-threaded Python the GC can finalize its owner anywhere): either way the level
/// leaves both the gate's count and the entering thread's own levels.
#[derive(Debug)]
pub struct HeldShare<'g> {
    gate: &'g Gate,
    /// The entering thread's held count.
    held: Arc<AtomicUsize>,
}

impl Drop for HeldShare<'_> {
    fn drop(&mut self) {
        // Off the owner first: if a fork is pending, the owner then stops counting as reentrant no
        // later than begin_fork stops counting the level.
        self.held.fetch_sub(1, Ordering::SeqCst);
        self.gate.holders.fetch_sub(1, Ordering::SeqCst);
    }
}

/// The exclusive side of a [`Gate`], held from [`Gate::begin_fork`] to the after-fork hook. Not
/// `Send`: it ends the forking thread's thread-local `forking` state.
#[derive(Debug)]
#[must_use = "dropping the guard ends the fork as the parent"]
pub struct ForkGuard {
    gate: &'static Gate,
    active: bool,
    _not_send: PhantomData<*const ()>,
}

impl ForkGuard {
    /// Ends the fork in the parent: lets blocked threads in (they notice within [`WAITER_POLL`]).
    pub fn end_parent(mut self) {
        self.finish(false);
    }

    /// Ends the fork in the child. Only the forking thread exists there, so the holder count becomes
    /// that thread's own levels.
    pub fn end_child(mut self) {
        self.finish(true);
    }

    fn finish(&mut self, child: bool) {
        if !std::mem::replace(&mut self.active, false) {
            return;
        }
        let gate = self.gate;
        if child {
            gate.holders.store(gate.own_depth(), Ordering::SeqCst);
        }
        gate.local.with(|t| t.forking.set(false));
        gate.pending.store(false, Ordering::SeqCst);
    }
}

impl Drop for ForkGuard {
    fn drop(&mut self) {
        self.finish(false);
    }
}

thread_local! {
    /// The forking thread's guard on [`GATE`], kept between the before- and after-fork hooks.
    static HELD_FORK: RefCell<Option<ForkGuard>> = const { RefCell::new(None) };
}

/// The before-fork hook: takes [`GATE`] exclusively, then checks that no connection pool is
/// mid-update. Call it with the interpreter released.
pub fn begin_fork() {
    // A guard left here by a fork whose after-hook never ran still holds the claim, so
    // GATE.begin_fork would wait for it forever: end it first. CPython runs the parent's after-hooks
    // even when fork() fails, so this needs a hook that was skipped outright; we are then still the
    // process that took the claim, so ending it as the parent is the only sound reading.
    if let Some(stale) = HELD_FORK.with(|held| held.borrow_mut().take()) {
        stale.end_parent();
    }
    let guard = GATE.begin_fork();
    crate::pool::quiesce_all();
    HELD_FORK.with(|held| *held.borrow_mut() = Some(guard));
}

/// The after-fork hook in the parent.
pub fn end_fork_parent() {
    if let Some(guard) = HELD_FORK.with(|held| held.borrow_mut().take()) {
        guard.end_parent();
    }
}

/// The after-fork hook in the child: bumps the process generation, then reopens the gate.
pub fn end_fork_child() {
    GENERATION.fetch_add(1, Ordering::AcqRel);
    match HELD_FORK.with(|held| held.borrow_mut().take()) {
        Some(guard) => guard.end_child(),
        // The before-hook didn't run (it can't fail, but CPython ignores before-hook errors). Still
        // drop the parent's other threads from the count.
        None => GATE.holders.store(GATE.own_depth(), Ordering::SeqCst),
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::AtomicBool;
    use std::sync::mpsc;
    use std::thread;
    use std::time::Instant;

    use super::*;

    // Each test gets its own gate (and thread-local), so tests don't see each other's holders.
    macro_rules! test_gate {
        ($name:ident) => {{
            thread_local! { static LOCAL: ThreadState = const { ThreadState::new() }; }
            static $name: Gate = Gate::new(&LOCAL);
            &$name
        }};
    }

    const SETTLE: Duration = Duration::from_millis(100);

    #[test]
    fn shared_entry_is_reentrant_and_counted() {
        let gate = test_gate!(G);
        let a = gate.try_enter_shared().unwrap();
        let b = gate.try_enter_shared().unwrap();
        assert_eq!(gate.own_depth(), 2);
        assert_eq!(gate.holders.load(Ordering::SeqCst), 2);
        drop(b);
        drop(a);
        assert_eq!(gate.own_depth(), 0);
        assert_eq!(gate.holders.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn begin_fork_waits_for_holders() {
        let gate = test_gate!(G);
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel::<()>();
        let holder = thread::spawn(move || {
            let _guard = gate.try_enter_shared().unwrap();
            entered_tx.send(()).unwrap();
            release_rx.recv().unwrap();
        });
        entered_rx.recv().unwrap();

        let done = Arc::new(AtomicBool::new(false));
        let forker = {
            let done = Arc::clone(&done);
            thread::spawn(move || {
                let guard = gate.begin_fork();
                done.store(true, Ordering::SeqCst);
                guard.end_parent();
            })
        };
        thread::sleep(SETTLE);
        assert!(!done.load(Ordering::SeqCst), "begin_fork returned while another thread held the gate");
        release_tx.send(()).unwrap();
        holder.join().unwrap();
        forker.join().unwrap();
        assert!(done.load(Ordering::SeqCst));
    }

    #[test]
    fn a_pending_fork_blocks_new_entries_until_it_ends() {
        let gate = test_gate!(G);
        let fork = gate.begin_fork();
        let entered = Arc::new(AtomicBool::new(false));
        let entrant = {
            let entered = Arc::clone(&entered);
            thread::spawn(move || {
                assert!(gate.try_enter_shared().is_none(), "try_enter_shared succeeded during a fork");
                let _guard = gate.enter_shared_blocking();
                entered.store(true, Ordering::SeqCst);
            })
        };
        thread::sleep(SETTLE);
        assert!(!entered.load(Ordering::SeqCst), "a new shared entry got in during a fork");
        fork.end_parent();
        entrant.join().unwrap();
        assert!(entered.load(Ordering::SeqCst));
    }

    #[test]
    fn reentrant_entry_passes_a_pending_fork() {
        let gate = test_gate!(G);
        let (entered_tx, entered_rx) = mpsc::channel();
        let (go_tx, go_rx) = mpsc::channel::<()>();
        let holder = thread::spawn(move || {
            let outer = gate.try_enter_shared().unwrap();
            entered_tx.send(()).unwrap();
            go_rx.recv().unwrap();
            // A fork is now pending; a nested entry must not wait behind it.
            let inner = gate.try_enter_shared();
            assert!(inner.is_some(), "a reentrant entry was refused while a fork was pending");
            drop(inner);
            drop(outer);
        });
        entered_rx.recv().unwrap();
        let forker = thread::spawn(move || gate.begin_fork().end_parent());
        // Wait until the fork is pending before letting the holder nest.
        let deadline = Instant::now() + Duration::from_secs(5);
        while !gate.pending.load(Ordering::SeqCst) {
            assert!(Instant::now() < deadline, "the fork never became pending");
            thread::yield_now();
        }
        go_tx.send(()).unwrap();
        holder.join().unwrap();
        forker.join().unwrap();
    }

    #[test]
    fn begin_fork_inside_own_levels_does_not_wait_for_itself() {
        let gate = test_gate!(G);
        let outer = gate.try_enter_shared().unwrap();
        let held = gate.try_enter_shared().unwrap().into_held();
        let start = Instant::now();
        let fork = gate.begin_fork();
        assert!(start.elapsed() < Duration::from_secs(1));
        // The forking thread may still use the gate between the hooks.
        assert!(gate.try_enter_shared().is_some());
        fork.end_child();
        assert_eq!(gate.holders.load(Ordering::SeqCst), 2, "the child keeps exactly the forking thread's levels");
        drop(held);
        drop(outer);
        assert_eq!(gate.holders.load(Ordering::SeqCst), 0);
        assert_eq!(gate.own_depth(), 0);
    }

    #[test]
    fn end_child_drops_other_threads_levels() {
        let gate = test_gate!(G);
        // Simulate a level held by a parent thread that doesn't exist in the child: the fork can't
        // wait for it here, so count it in by hand after begin_fork.
        let fork = gate.begin_fork();
        gate.holders.fetch_add(3, Ordering::SeqCst);
        fork.end_child();
        assert_eq!(gate.holders.load(Ordering::SeqCst), 0);
        assert!(gate.try_enter_shared().is_some());
    }

    #[test]
    fn concurrent_forks_take_turns() {
        let gate = test_gate!(G);
        let inside = Arc::new(AtomicUsize::new(0));
        let threads: Vec<_> = (0..4)
            .map(|_| {
                let inside = Arc::clone(&inside);
                thread::spawn(move || {
                    for _ in 0..20 {
                        let fork = gate.begin_fork();
                        assert_eq!(inside.fetch_add(1, Ordering::SeqCst), 0, "two forks were exclusive at once");
                        inside.fetch_sub(1, Ordering::SeqCst);
                        fork.end_parent();
                    }
                })
            })
            .collect();
        for t in threads {
            t.join().unwrap();
        }
    }

    #[test]
    fn a_held_share_dropped_on_another_thread_releases_the_owners_level() {
        let gate = test_gate!(G);
        let (held_tx, held_rx) = mpsc::channel();
        let (pending_tx, pending_rx) = mpsc::channel::<()>();
        let (checked_tx, checked_rx) = mpsc::channel::<()>();
        // Thread A enters, keeps the level as a HeldShare and hands it off.
        let a = thread::spawn(move || {
            let held = gate.try_enter_shared().unwrap().into_held();
            assert_eq!(gate.own_depth(), 1, "a held level still counts as the entering thread's");
            held_tx.send(held).unwrap();
            pending_rx.recv().unwrap();
            assert_eq!(gate.own_depth(), 0, "the level dropped on another thread still counts as A's");
            assert!(gate.try_enter_shared().is_none(), "A still walks past a pending fork");
            checked_tx.send(()).unwrap();
        });
        // Thread B drops it.
        let held = held_rx.recv().unwrap();
        thread::spawn(move || drop(held)).join().unwrap();
        assert_eq!(gate.holders.load(Ordering::SeqCst), 0);
        // Thread C forks: it must not wait for the released level, and A must wait for it.
        let (forked_tx, forked_rx) = mpsc::channel();
        let c = thread::spawn(move || {
            let fork = gate.begin_fork();
            forked_tx.send(()).unwrap();
            checked_rx.recv().unwrap();
            fork.end_parent();
        });
        forked_rx.recv_timeout(Duration::from_secs(5)).expect("the fork never completed");
        pending_tx.send(()).unwrap();
        a.join().unwrap();
        c.join().unwrap();
        assert_eq!(gate.holders.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn a_leftover_fork_guard_is_ended_by_the_next_before_hook() {
        // Uses the global gate and HELD_FORK, on a fresh thread so no other test's levels count.
        thread::spawn(|| {
            begin_fork();
            // The after-hook "never ran"; the next before-hook must not wait for its claim.
            begin_fork();
            end_fork_parent();
            assert!(!GATE.pending.load(Ordering::SeqCst));
        })
        .join()
        .unwrap();
    }
}
