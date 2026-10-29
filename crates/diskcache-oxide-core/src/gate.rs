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
//! - The gate is the outermost lock. While holding a shared level, a thread waits for nothing another
//!   thread can hold: not the write lock, a connection slot, a connection or the gate. A pool that
//!   runs out of connections releases the gate before it waits for one. Two waits are allowed:
//!   SQLite's own busy wait inside an [`crate::lockwait::AttachedWindow`], and bounded local work run
//!   through [`run_blocking`] (file I/O, or a SQLite call whose only wait is that window: the
//!   spill-file read in `pop`, the forced WAL checkpoint), which keeps a pending fork waiting no
//!   longer than the work takes.
//! - Both rely on a second rule: **no thread waits on a gate holder while attached** to the
//!   interpreter. Every wait for the write lock, a slot or a pending fork goes through the blocker.
//!   Otherwise a holder that released the interpreter could deadlock re-taking it on a GIL build.
//! - [`Gate::begin_fork`] waits until the only holders left are the calling thread's own levels and
//!   those of threads parked in `begin_fork` behind it, so forking inside your own transaction
//!   doesn't deadlock, and neither do two threads that each hold their own and fork at once: a
//!   parked forker is running Python, not SQLite, so its levels are quiescent. Those transactions
//!   are poisoned in the child (see [`generation`] and [`crate::poison`]).
//! - Blocking waits go through the [`Blocker`] installed with [`set_blocker`], so the Python layer
//!   can release the interpreter while a thread waits. The fast path never calls it. The Python
//!   layer's blocker trusts a per-thread "already detached" flag, set while its `detach` helper
//!   runs long Rust-only work (`clear`, `expire`, `cull`, opening, `begin_hold`), and then waits
//!   without re-attaching. Work run through that helper must therefore never attach the
//!   interpreter (no Python callbacks), or its waits would run attached and break the rule above.
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
use std::sync::OnceLock;
use std::sync::atomic::Ordering;

use crate::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize};
use crate::sync::{Arc, LocalKey, thread};
use std::time::Duration;

/// How often [`Gate::begin_fork`] re-checks the claim and the holder count. Holders never notify on
/// exit, which keeps the exit path to one atomic decrement.
const FORK_POLL: Duration = Duration::from_millis(1);

/// How often a thread blocked behind a pending fork re-checks it. Nothing wakes it early, so this is
/// also the most a thread can lag behind the end of a fork.
const WAITER_POLL: Duration = Duration::from_millis(1);

/// Runs a blocking wait. The Python layer installs one that releases the interpreter around it. It
/// must run the closure exactly once, on the calling thread: callers keep thread-local state (an
/// open [`crate::lockwait::AttachedWindow`]) across it.
pub type Blocker = fn(&(dyn Fn() + Sync));

static BLOCKER: OnceLock<Blocker> = OnceLock::new();

/// Installs the function every blocking gate wait runs through. Only the first call has an effect;
/// returns whether this call installed it.
pub fn set_blocker(blocker: Blocker) -> bool {
    BLOCKER.set(blocker).is_ok()
}

/// Runs `work` once, on the calling thread, through the installed [`Blocker`], so the interpreter is
/// released while it runs. Usually called with no gate level held. A caller holding one may use it
/// only for the bounded work the rules above allow (the spill-file read in `pop`, the forced WAL
/// checkpoint).
pub fn run_blocking(wait: &(dyn Fn() + Sync)) {
    match BLOCKER.get() {
        Some(blocker) => blocker(wait),
        None => wait(),
    }
}

/// One thread's view of one gate.
#[derive(Debug)]
pub struct ThreadState {
    /// How many shared levels this thread holds through [`SharedGuard`]s.
    depth: Cell<usize>,
    /// The levels this thread entered that are now kept as [`HeldShare`]s. Shared with each share,
    /// so one dropped on another thread still takes its level off this thread. Created on the first
    /// [`SharedGuard::into_held`], and read only while a fork is pending.
    held: OnceCell<Arc<Owner>>,
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
        self.held.get().map_or(0, |owner| owner.levels.load(Ordering::SeqCst) & !PARKED)
    }
}

/// The part of a thread's state its [`HeldShare`]s keep, so they can be dropped on any thread.
#[derive(Debug)]
struct Owner {
    /// How many held levels the thread has, with [`PARKED`] set while it is parked in
    /// [`Gate::begin_fork`] (a share dropped then also takes its level off the parked half of
    /// [`Gate::counts`]).
    levels: AtomicUsize,
    /// The [`Gate::epoch`] this thread exists in. A share whose owner is from an older epoch was
    /// inherited across fork from a thread that doesn't exist here, and its level was never counted.
    epoch: AtomicU64,
}

/// Set in [`Owner::levels`] while the thread is parked in [`Gate::begin_fork`].
const PARKED: usize = 1 << (usize::BITS - 1);

impl Default for ThreadState {
    fn default() -> Self {
        Self::new()
    }
}

crate::sync::thread_local! {
    static GLOBAL_THREAD: ThreadState = const { ThreadState::new() };
}

/// The process-wide gate every store uses.
pub static GATE: Gate = Gate::with_early_child_entry(&GLOBAL_THREAD, enter_child_early);

/// Bumped in the forked child. Anything tagged with an older generation was inherited from the
/// parent and must be dropped without being closed (`mem::forget`), never used.
static GENERATION: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// The current process generation (0 in a process that was never forked from a cache user).
#[must_use]
pub fn generation() -> u64 {
    GENERATION.load(Ordering::Acquire)
}

/// A reentrant shared/exclusive gate whose exclusive side survives `fork()`. See the module docs.
#[derive(Debug)]
pub struct Gate {
    /// The two counts the forker compares, in one word so one load is a consistent snapshot. Low 32
    /// bits: shared levels held across all threads (depth 2 counts twice). High 32 bits, signed and
    /// wrapping: levels of threads parked in [`Gate::begin_fork`], waiting for another thread's
    /// claim. Those are quiescent, so the claiming thread doesn't wait for them. Signed because a
    /// share dropped while its owner parks can take its level off before the owner has added it;
    /// every transient error makes the parked count too low, which only makes the fork wait longer.
    counts: AtomicU64,
    /// A fork is claimed: set by [`Gate::begin_fork`], cleared when its [`ForkGuard`] ends. Also
    /// what makes concurrent forks take turns.
    pending: AtomicBool,
    /// Bumped in the forked child, see [`Owner::epoch`].
    epoch: AtomicU64,
    local: &'static LocalKey<ThreadState>,
    /// Run when the forking thread enters at depth 0 before its fork has ended (see
    /// [`enter_child_early`]).
    early_child_entry: Option<fn()>,
}

impl Gate {
    /// A gate whose per-thread state lives in `local`. Each gate needs its own thread-local.
    #[must_use]
    pub const fn new(local: &'static LocalKey<ThreadState>) -> Self {
        Self::with_early_child_entry_opt(local, None)
    }

    /// [`Gate::new`], running `early` when the forking thread enters at depth 0 between
    /// [`Gate::begin_fork`] and the end of its fork.
    const fn with_early_child_entry(local: &'static LocalKey<ThreadState>, early: fn()) -> Self {
        Self::with_early_child_entry_opt(local, Some(early))
    }

    const fn with_early_child_entry_opt(local: &'static LocalKey<ThreadState>, early: Option<fn()>) -> Self {
        Self {
            counts: AtomicU64::new(0),
            pending: AtomicBool::new(false),
            epoch: AtomicU64::new(0),
            local,
            early_child_entry: early,
        }
    }

    /// Enters in shared mode without waiting. Returns `None` only if a fork is pending and this
    /// thread holds no level yet.
    pub fn try_enter_shared(&self) -> Option<SharedGuard<'_>> {
        self.local.with(|t| {
            let depth = t.depth.get();
            if depth == 0
                && t.forking.get()
                && let Some(early) = self.early_child_entry
            {
                early();
            }
            // Increment, then check: paired with begin_fork's store-then-load, either we see the
            // pending flag or begin_fork sees our level. The held levels are read last, so only
            // while a fork is pending.
            self.counts.fetch_add(HOLDER, Ordering::SeqCst);
            if depth == 0 && !t.forking.get() && self.pending.load(Ordering::SeqCst) && t.held_levels() == 0 {
                self.counts.fetch_sub(HOLDER, Ordering::SeqCst);
                return None;
            }
            t.depth.set(depth + 1);
            Some(SharedGuard { gate: self, _not_send: PhantomData })
        })
    }

    /// Enters in shared mode, waiting on this thread (attached to nothing) while a fork is pending.
    /// Never call it from code attached to the interpreter: an attached wait on a gate holder breaks
    /// the second rule above. Only tests use it.
    #[cfg(test)]
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
            thread::sleep(WAITER_POLL);
        }
    }

    /// The shared levels the calling thread holds, including those kept as [`HeldShare`]s.
    #[must_use]
    pub fn own_depth(&self) -> usize {
        self.local.with(|t| t.depth.get() + t.held_levels())
    }

    /// Takes the gate exclusively: claims the fork (waiting for another thread's to end), blocks
    /// new shared entries and waits, with no timeout, until the only levels left are the calling
    /// thread's own and those of threads parked here waiting for the claim. Call it with the
    /// interpreter released.
    ///
    /// While it waits for the claim, the calling thread's levels count as parked. So two threads
    /// that each hold a transaction and fork at once take turns: the first forks, and the second
    /// forks once the first has ended its claim and every level that isn't the second's or parked
    /// is released (the first thread's transaction included).
    ///
    /// The returned guard keeps the claim until it is ended with [`ForkGuard::end_parent`] or
    /// [`ForkGuard::end_child`] (dropping it counts as the parent).
    pub fn begin_fork(&'static self) -> ForkGuard {
        if !self.try_claim() {
            self.park();
            while !self.try_claim() {
                thread::sleep(FORK_POLL);
            }
            self.unpark();
        }
        while !self.quiescent() {
            thread::sleep(FORK_POLL);
        }
        self.local.with(|t| t.forking.set(true));
        ForkGuard { gate: self, active: true, _not_send: PhantomData }
    }

    fn try_claim(&self) -> bool {
        // SeqCst on success pairs with try_enter_shared's increment-then-check.
        self.pending.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_ok()
    }

    /// Whether every level is the calling (claiming) thread's own or parked.
    fn quiescent(&self) -> bool {
        // One load, so holders and parked are a consistent snapshot (two loads let a nested entry
        // and a park land in between and look quiescent; found by the shuttle models).
        let (holders, parked) = split(self.counts.load(Ordering::SeqCst));
        // Own levels last, re-read every poll: a share of ours dropped on another thread meanwhile
        // comes off `held` before `holders`, so this can only see too few own levels, never too many.
        let own = signed(self.own_depth());
        holders.saturating_sub(parked) <= own
    }

    /// Counts the calling thread's levels as parked, while it waits for another thread's claim.
    fn park(&self) {
        self.local.with(|t| {
            // Flag first: a share dropped from here on takes its level off `parked` too, and every
            // one dropped before is already missing from `held`. One dropped before the add below
            // makes the count briefly too low, which is the safe direction.
            let held = t.held.get().map_or(0, |owner| owner.levels.fetch_or(PARKED, Ordering::SeqCst) & !PARKED);
            self.counts.fetch_add(parked_units(t.depth.get() + held), Ordering::SeqCst);
        });
    }

    /// Takes the calling thread's levels off `parked`, once it holds the claim. Only the claiming
    /// thread reads `parked`, and it is this one, so the brief overcount in between is harmless.
    fn unpark(&self) {
        self.local.with(|t| {
            let held = t.held.get().map_or(0, |owner| owner.levels.fetch_and(!PARKED, Ordering::SeqCst) & !PARKED);
            self.counts.fetch_sub(parked_units(t.depth.get() + held), Ordering::SeqCst);
        });
    }

    /// In the forked child, where only the forking thread exists: its levels are the only ones,
    /// nobody is parked, and shares owned by the parent's other threads no longer count.
    fn reset_in_child(&self) {
        let epoch = self.epoch.fetch_add(1, Ordering::SeqCst) + 1;
        self.local.with(|t| {
            if let Some(owner) = t.held.get() {
                owner.epoch.store(epoch, Ordering::SeqCst);
            }
        });
        self.counts.store(u64::try_from(self.own_depth()).unwrap_or(LOW) & LOW, Ordering::SeqCst);
    }

    /// The child-side reset when this thread never claimed the fork (its before-fork hook didn't
    /// run): [`Gate::reset_in_child`], and the claim cleared too. Another thread in the parent may
    /// have held it, mid-way through its own fork, and no thread in the child is forking.
    fn reset_in_unclaimed_child(&self) {
        self.reset_in_child();
        self.pending.store(false, Ordering::SeqCst);
    }

    /// Back to a fresh gate, so a model reuses one gate per OS thread instead of leaking one per
    /// execution (shuttle's atomics are large, and a run makes millions of executions).
    #[cfg(all(test, shuttle))]
    fn reset_for_model(&self) {
        self.counts.store(0, Ordering::SeqCst);
        self.pending.store(false, Ordering::SeqCst);
        self.epoch.store(0, Ordering::SeqCst);
    }

    #[cfg(test)]
    fn holders(&self) -> isize {
        split(self.counts.load(Ordering::SeqCst)).0
    }

    #[cfg(test)]
    fn parked(&self) -> isize {
        split(self.counts.load(Ordering::SeqCst)).1
    }
}

fn signed(levels: usize) -> isize {
    isize::try_from(levels).unwrap_or(isize::MAX)
}

const LOW: u64 = 0xFFFF_FFFF;
const HOLDER: u64 = 1;
const PARKED_ONE: u64 = 1 << 32;

/// `levels` parked, as an addend to [`Gate::counts`] (wrapping in the high half).
fn parked_units(levels: usize) -> u64 {
    (u64::try_from(levels).unwrap_or(LOW) & LOW) << 32
}

/// (holders, parked) from one load of [`Gate::counts`].
fn split(word: u64) -> (isize, isize) {
    let holders = isize::try_from(word & LOW).unwrap_or(isize::MAX);
    // The high half is an i32 count stored wrapping: reinterpret its bits.
    let parked = u32::try_from(word >> 32).map_or(0, u32::cast_signed);
    (holders, isize::try_from(parked).unwrap_or(0))
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
        let gate = self.gate;
        let owner = gate.local.with(|t| {
            let owner = Arc::clone(t.held.get_or_init(|| {
                Arc::new(Owner {
                    levels: AtomicUsize::new(0),
                    epoch: AtomicU64::new(gate.epoch.load(Ordering::SeqCst)),
                })
            }));
            // Move the level from the depth to the held count; only this thread reads either.
            owner.levels.fetch_add(1, Ordering::SeqCst);
            t.depth.set(t.depth.get().saturating_sub(1));
            owner
        });
        let share = HeldShare { gate, owner };
        std::mem::forget(self);
        share
    }
}

impl Drop for SharedGuard<'_> {
    fn drop(&mut self) {
        // try_with: a guard dropped while this thread's locals are being destroyed still releases.
        let _ = self.gate.local.try_with(|t| t.depth.set(t.depth.get().saturating_sub(1)));
        let prev = self.gate.counts.fetch_sub(HOLDER, Ordering::SeqCst);
        debug_assert_ne!(prev & LOW, 0, "holders underflow");
    }
}

/// A shared level kept across Rust scopes, see [`SharedGuard::into_held`]. It may be dropped on any
/// thread (on free-threaded Python the GC can finalize its owner anywhere): either way the level
/// leaves both the gate's count and the entering thread's own levels.
#[derive(Debug)]
pub struct HeldShare<'g> {
    gate: &'g Gate,
    /// The entering thread's held count.
    owner: Arc<Owner>,
}

impl Drop for HeldShare<'_> {
    fn drop(&mut self) {
        if self.owner.epoch.load(Ordering::SeqCst) != self.gate.epoch.load(Ordering::SeqCst) {
            // Inherited across fork from a thread parked in begin_fork in the parent: the child
            // never counted its level.
            return;
        }
        // Off the owner first: if a fork is pending, the owner then stops counting as reentrant no
        // later than begin_fork stops counting the level.
        let before = self.owner.levels.fetch_sub(1, Ordering::SeqCst);
        // One step for both counts: a forker's snapshot never sees one without the other.
        let units = if before & PARKED != 0 { HOLDER | PARKED_ONE } else { HOLDER };
        let prev = self.gate.counts.fetch_sub(units, Ordering::SeqCst);
        debug_assert_ne!(prev & LOW, 0, "holders underflow");
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
            gate.reset_in_child();
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

/// The process [`begin_fork`] ran in, so a gate entry can tell it's in the child before
/// [`end_fork_child`] has run (see [`enter_child_early`]).
static FORKING_PID: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

/// The process [`end_fork_child`] last ran in, so it runs once per child.
static CHILD_RESET_PID: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

/// [`GATE`]'s early child entry: the forking thread entering at depth 0 before its after-fork hook
/// ran. In the child that's another library's `after_in_child` hook registered before ours (they
/// run in registration order) using a cache: run the child-side reset now, so it doesn't see
/// the parent's connections, write lock or unpoisoned transactions. In the parent (other
/// libraries' fork hooks around ours) it does nothing.
fn enter_child_early() {
    if std::process::id() != FORKING_PID.load(Ordering::Acquire) {
        end_fork_child();
    }
}

/// The before-fork hook: takes [`GATE`] exclusively, checks that no connection pool is mid-update,
/// and closes every pool's idle connections, so the child doesn't inherit SQLite's per-file lock
/// records (see [`crate::pool`]). Call it with the interpreter released.
pub fn begin_fork() {
    // A guard left here by a fork whose after-hook never ran still holds the claim, so
    // GATE.begin_fork would wait for it forever: end it first. CPython runs the parent's after-hooks
    // even when fork() fails, so this needs a hook that was skipped outright; we are then still the
    // process that took the claim, so ending it as the parent is the only sound reading.
    if let Some(stale) = HELD_FORK.with(|held| held.borrow_mut().take()) {
        stale.end_parent();
    }
    // Before the claim, which is what sets this thread's `forking` flag.
    FORKING_PID.store(std::process::id(), Ordering::Release);
    let guard = GATE.begin_fork();
    crate::pool::quiesce_all();
    crate::store::quiesce_registry();
    crate::pool::close_idle_all();
    HELD_FORK.with(|held| *held.borrow_mut() = Some(guard));
}

/// The after-fork hook in the parent.
pub fn end_fork_parent() {
    if let Some(guard) = HELD_FORK.with(|held| held.borrow_mut().take()) {
        guard.end_parent();
    }
}

/// The after-fork hook in the child: bumps the process generation, poisons the databases a write
/// transaction was held on at the fork (D-020, see [`crate::poison`]), then reopens the gate. Runs
/// once per child: the first gate entry by an earlier `after_in_child` hook may have run it already.
pub fn end_fork_child() {
    let pid = std::process::id();
    if CHILD_RESET_PID.swap(pid, Ordering::AcqRel) == pid {
        return;
    }
    GENERATION.fetch_add(1, Ordering::AcqRel);
    crate::poison::poison_live_holds();
    match HELD_FORK.with(|held| held.borrow_mut().take()) {
        Some(guard) => guard.end_child(),
        // The before-hook didn't run (it can't fail, but CPython ignores before-hook errors). Still
        // drop the parent's other threads from the count, and any claim one of them held.
        None => GATE.reset_in_unclaimed_child(),
    }
}

#[cfg(all(test, not(shuttle)))]
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
        assert_eq!(gate.holders(), 2);
        drop(b);
        drop(a);
        assert_eq!(gate.own_depth(), 0);
        assert_eq!(gate.holders(), 0);
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
        assert_eq!(gate.holders(), 2, "the child keeps exactly the forking thread's levels");
        drop(held);
        drop(outer);
        assert_eq!(gate.holders(), 0);
        assert_eq!(gate.own_depth(), 0);
    }

    #[test]
    fn end_child_drops_other_threads_levels() {
        let gate = test_gate!(G);
        // Simulate a level held by a parent thread that doesn't exist in the child: the fork can't
        // wait for it here, so count it in by hand after begin_fork.
        let fork = gate.begin_fork();
        gate.counts.fetch_add(3 * HOLDER, Ordering::SeqCst);
        fork.end_child();
        assert_eq!(gate.holders(), 0);
        assert!(gate.try_enter_shared().is_some());
    }

    #[test]
    fn a_child_whose_before_hook_never_ran_drops_another_threads_claim() {
        let gate = test_gate!(G);
        // A claim taken by a parent thread forking at the same moment; this thread forked without
        // the before-hook, so the child inherits the flag set and no guard to clear it.
        gate.pending.store(true, Ordering::SeqCst);
        gate.counts.fetch_add(HOLDER, Ordering::SeqCst);
        gate.reset_in_unclaimed_child();
        assert_eq!(gate.holders(), 0);
        assert!(gate.try_enter_shared().is_some(), "the child waits for a fork no thread there runs");
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
        assert_eq!(gate.holders(), 0);
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
        assert_eq!(gate.holders(), 0);
    }

    /// Two threads each hold their own level and fork at once: neither may wait for the other's
    /// level while that one is parked behind its claim.
    #[test]
    fn two_forkers_holding_their_own_levels_take_turns() {
        let gate = test_gate!(G);
        let (forked_tx, forked_rx) = mpsc::channel::<usize>();
        let (release_tx, release_rx) = mpsc::channel::<()>();
        let release_rx = Arc::new(std::sync::Mutex::new(release_rx));
        let ready = Arc::new(std::sync::Barrier::new(2));
        let forkers: Vec<_> = (0..2)
            .map(|i| {
                let (forked_tx, release_rx, ready) = (forked_tx.clone(), Arc::clone(&release_rx), Arc::clone(&ready));
                thread::spawn(move || {
                    let held = gate.try_enter_shared().unwrap().into_held();
                    ready.wait();
                    let fork = gate.begin_fork();
                    forked_tx.send(i).unwrap();
                    fork.end_parent();
                    // The parent goes on and ends its transaction once told to.
                    release_rx.lock().unwrap().recv().unwrap();
                    drop(held);
                })
            })
            .collect();
        let first = forked_rx.recv_timeout(Duration::from_secs(5)).expect("the first fork deadlocked");
        // The second forker now holds the claim, but must wait for the first one's level.
        assert!(forked_rx.recv_timeout(SETTLE).is_err(), "the second fork ran while the first still held its level");
        release_tx.send(()).unwrap();
        let second = forked_rx.recv_timeout(Duration::from_secs(5)).expect("the second fork never ran");
        assert_ne!(first, second);
        release_tx.send(()).unwrap();
        for t in forkers {
            t.join().unwrap();
        }
        assert_eq!(gate.holders(), 0);
        assert_eq!(gate.parked(), 0);
    }

    /// Many forkers, each holding a nested level and a held one, all forking repeatedly.
    #[test]
    fn many_forkers_holding_levels_never_deadlock() {
        let gate = test_gate!(G);
        let inside = Arc::new(AtomicUsize::new(0));
        let threads: Vec<_> = (0..4)
            .map(|_| {
                let inside = Arc::clone(&inside);
                thread::spawn(move || {
                    for _ in 0..20 {
                        let outer = gate.enter_shared_blocking();
                        let held = gate.try_enter_shared().unwrap().into_held();
                        let fork = gate.begin_fork();
                        assert_eq!(inside.fetch_add(1, Ordering::SeqCst), 0, "two forks were exclusive at once");
                        inside.fetch_sub(1, Ordering::SeqCst);
                        fork.end_parent();
                        drop(held);
                        drop(outer);
                    }
                })
            })
            .collect();
        for t in threads {
            t.join().unwrap();
        }
        assert_eq!(gate.holders(), 0);
        assert_eq!(gate.parked(), 0);
    }

    #[test]
    fn a_parked_level_is_quiescent_but_a_running_one_is_not() {
        let gate = test_gate!(G);
        let (share_tx, share_rx) = mpsc::channel();
        // B holds a level and parks (as if waiting in begin_fork), handing its share over.
        thread::spawn(move || {
            let held = gate.try_enter_shared().unwrap().into_held();
            gate.park();
            share_tx.send(held).unwrap();
        })
        .join()
        .unwrap();
        let held = share_rx.recv().unwrap();
        assert_eq!(gate.parked(), 1);
        assert!(gate.quiescent(), "a parked level held up the fork");
        // A running level on another thread still holds it up (this thread's own would count as own).
        let (entered_tx, entered_rx) = mpsc::channel();
        let (leave_tx, leave_rx) = mpsc::channel::<()>();
        let runner = thread::spawn(move || {
            let _running = gate.try_enter_shared().unwrap();
            entered_tx.send(()).unwrap();
            leave_rx.recv().unwrap();
        });
        entered_rx.recv().unwrap();
        assert!(!gate.quiescent());
        leave_tx.send(()).unwrap();
        runner.join().unwrap();
        // Dropping the parked owner's share takes it off both counts.
        drop(held);
        assert_eq!(gate.parked(), 0);
        assert_eq!(gate.holders(), 0);
    }

    #[test]
    fn park_and_unpark_balance_with_a_share_dropped_in_between() {
        let gate = test_gate!(G);
        let outer = gate.try_enter_shared().unwrap();
        let kept = gate.try_enter_shared().unwrap().into_held();
        let dropped = gate.try_enter_shared().unwrap().into_held();
        gate.park();
        assert_eq!(gate.parked(), 3);
        thread::spawn(move || drop(dropped)).join().unwrap();
        assert_eq!(gate.parked(), 2);
        assert_eq!(gate.own_depth(), 2, "the parked flag isn't counted as levels");
        gate.unpark();
        assert_eq!(gate.parked(), 0);
        assert_eq!(gate.own_depth(), 2);
        drop(kept);
        drop(outer);
        assert_eq!(gate.holders(), 0);
    }

    /// The child of the first forker: the parked forker doesn't exist there, so its levels and the
    /// parked count are dropped, and its inherited share, if freed, changes nothing.
    #[test]
    fn the_child_forgets_parked_forkers_and_their_shares() {
        let gate = test_gate!(G);
        let (share_tx, share_rx) = mpsc::channel();
        thread::spawn(move || {
            let held = gate.try_enter_shared().unwrap().into_held();
            gate.park();
            share_tx.send(held).unwrap();
        })
        .join()
        .unwrap();
        let parked_share = share_rx.recv().unwrap();
        let own = gate.try_enter_shared().unwrap().into_held();
        let fork = gate.begin_fork();
        fork.end_child();
        assert_eq!(gate.holders(), 1, "only the forking thread's level is left");
        assert_eq!(gate.parked(), 0);
        drop(parked_share);
        assert_eq!(gate.holders(), 1, "an inherited share from a parked forker was counted");
        assert_eq!(gate.parked(), 0);
        // The forking thread's own share still counts after the fork, on any thread.
        thread::spawn(move || drop(own)).join().unwrap();
        assert_eq!(gate.holders(), 0);
        assert_eq!(gate.own_depth(), 0);
        gate.begin_fork().end_parent();
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

#[cfg(all(test, shuttle))]
mod model;
