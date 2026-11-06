//! Locks and counters that a forked child inherits as free.
//!
//! A thread can hold these *outside* the fork gate (the write lock between acquiring it and entering
//! the gate, or while it sleeps in a detached retry), so a fork can copy one held, with its owner
//! left behind in the parent. Each value packs the [process generation](crate::gate::generation) it
//! was taken in next to its state in one `AtomicU64`. A value from an older generation reads as
//! free or zero, so the child needs no reset step that could race with its own threads.
//!
//! Layout: the high 32 bits are the generation (mod 2^32), the low 32 bits the state.

use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use crate::sync::atomic::AtomicU64;
use crate::sync::{now, thread};

use crate::gate;

const LOW: u64 = 0xFFFF_FFFF;

/// How often a blocked [`WriteLock::lock_until`] or [`Slots::acquire_until`] re-checks.
const POLL: Duration = Duration::from_micros(50);

/// The [`Slots::acquire_until`] wait's spin, in `spin_loop` hints (total, not tries), before the
/// [`POLL`] sleeps. Only on 3 or more CPUs, as the SQLite mutex spin ([`crate::sqlite_mutex`]): on a 2-CPU
/// quota, spinners use up the quota the slot holder needs.
#[cfg(not(shuttle))]
const SLOT_SPIN: u32 = 1024;
/// Under the model checker: the spin ends in a few steps.
#[cfg(shuttle)]
const SLOT_SPIN: u32 = 2;

/// The spin budget in force: [`SLOT_SPIN`], or 0 on 2 or fewer CPUs. Computed on first use and kept
/// in a plain atomic, not a `OnceLock`: a fork during the first computation can't leave a child
/// stuck, it only computes it again.
#[cfg(not(shuttle))]
fn slot_spin() -> u32 {
    static SPIN: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(u32::MAX);
    let spin = SPIN.load(Ordering::Relaxed);
    if spin != u32::MAX {
        return spin;
    }
    let cpus = std::thread::available_parallelism().ok().map(std::num::NonZero::get);
    let spin = sweep::slot_spin(slot_spin_for(cpus));
    SPIN.store(spin, Ordering::Relaxed);
    spin
}

/// [`SLOT_SPIN`], or 0 on 2 or fewer CPUs; an unknown count keeps the spin.
#[cfg(not(shuttle))]
fn slot_spin_for(cpus: Option<usize>) -> u32 {
    if cpus.is_some_and(|n| n < 3) { 0 } else { SLOT_SPIN }
}

#[cfg(shuttle)]
fn slot_spin() -> u32 {
    SLOT_SPIN
}

/// How many slot waiters may spin at once, process-wide: half the CPUs, at least 1.
#[cfg(not(shuttle))]
fn slot_spinners() -> u32 {
    static SPINNERS_MAX: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(u32::MAX);
    let max = SPINNERS_MAX.load(Ordering::Relaxed);
    if max != u32::MAX {
        return max;
    }
    let cpus = std::thread::available_parallelism().ok().map(std::num::NonZero::get);
    let max = sweep::slot_spinners(slot_spinners_for(cpus));
    SPINNERS_MAX.store(max, Ordering::Relaxed);
    max
}

/// Half of `cpus`, at least 1; 4 if the count is unknown.
#[cfg(not(shuttle))]
fn slot_spinners_for(cpus: Option<usize>) -> u32 {
    cpus.map_or(4, |n| u32::try_from(n / 2).unwrap_or(u32::MAX).max(1))
}

/// The slot wait's spin budget and spinner limit in force, for the sweep's harness to confirm its
/// overrides took. Only in a sweep build (`--cfg dco_spin_sweep`).
#[cfg(all(dco_spin_sweep, not(shuttle)))]
#[doc(hidden)]
#[must_use]
pub fn sweep_slot_wait() -> (u32, u32) {
    (slot_spin(), slot_spinners())
}

/// The slot-wait sweep's overrides, `DCO_SLOT_SPIN` and `DCO_SLOT_SPINNERS`, compiled in only under
/// `RUSTFLAGS="--cfg dco_spin_sweep"` (a benchmark build, as for [`crate::sqlite_mutex`]). A shipped
/// build has no environment knob.
#[cfg(not(shuttle))]
mod sweep {
    #[cfg(dco_spin_sweep)]
    fn read(name: &str, default: u32) -> u32 {
        std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
    }

    pub(super) fn slot_spin(default: u32) -> u32 {
        #[cfg(dco_spin_sweep)]
        return read("DCO_SLOT_SPIN", default);
        #[cfg(not(dco_spin_sweep))]
        default
    }

    pub(super) fn slot_spinners(default: u32) -> u32 {
        #[cfg(dco_spin_sweep)]
        return read("DCO_SLOT_SPINNERS", default);
        #[cfg(not(dco_spin_sweep))]
        default
    }
}

/// How long a [`WriteLock::lock_until`] waiter competes on equal terms before it starves (or half
/// its time to the deadline, if that's sooner). From then on, threads that didn't wait can't barge
/// in ahead of it. Waiters find a freed lock by polling,
/// so the lock goes briefly idle at each fair hand-off, and starving must stay rare. At 1 ms (Go's
/// `sync.Mutex` value, which hands off directly), a 99:1 mix at 8 threads on free-threaded Linux
/// stayed in starvation mode and fell from about 740k to 175k ops/s. At 20 ms it's back to 741k,
/// and a starved waiter still gets in well inside any timeout.
#[cfg(not(shuttle))]
const STARVE_AFTER: Duration = Duration::from_millis(20);
/// Under the model checker: a few polls, so schedules reach starvation mode.
#[cfg(shuttle)]
const STARVE_AFTER: Duration = Duration::from_micros(200);

fn current() -> u64 {
    gate::generation() & LOW
}

fn pack(generation: u64, state: u64) -> u64 {
    (generation << 32) | (state & LOW)
}

/// The state of `word` in the current generation; an older generation's state counts as 0.
fn state_now(word: u64, generation: u64) -> u64 {
    if word >> 32 == generation { word & LOW } else { 0 }
}

/// The in-process write lock for one database file (05-phase-2-core-cache.md, "Concurrency rules").
///
/// Threads of one process queue here, with the interpreter released, before `BEGIN IMMEDIATE`, so
/// only other processes ever meet SQLite's busy handler. It's taken *before* the gate, never while
/// holding it; at gate depth > 0 callers use [`WriteLock::try_lock_nested`] only.
///
/// **Starvation:** a free lock goes to whoever takes it first, so a thread that releases it and
/// asks again at once (a tight `set()` loop) can win every time against a waiter that polls. A
/// waiter that has waited `STARVE_AFTER` counts itself in `starving`; while that's non-zero,
/// [`WriteLock::try_lock`] refuses and fresh callers queue, so the lock goes to a starving waiter.
/// Without this, a `clear()` (which yields between chunks) timed out behind one writer thread on
/// free-threaded Linux.
#[derive(Debug, Default)]
pub struct WriteLock {
    /// State 1 = held.
    word: AtomicU64,
    /// State = number of threads waiting in [`WriteLock::lock_until`].
    waiters: AtomicU64,
    /// State = how many of those have waited longer than [`STARVE_AFTER`].
    starving: AtomicU64,
}

/// Proof of holding a [`WriteLock`]. `Send`, so a hold can outlive one Python call and be dropped
/// on another thread (as `transact()` will need in Phase 6b). Dropping it releases the lock, unless
/// it was inherited across `fork()`, in which case it releases nothing.
#[derive(Debug)]
#[must_use = "dropping the token releases the write lock"]
pub struct WriteToken<'a> {
    lock: &'a WriteLock,
    word: u64,
}

impl WriteLock {
    #[must_use]
    pub const fn new() -> Self {
        Self { word: AtomicU64::new(0), waiters: AtomicU64::new(0), starving: AtomicU64::new(0) }
    }

    /// Takes the lock if it's free (or was only held by a thread in a parent process) and no
    /// waiter is starving.
    pub fn try_lock(&self) -> Option<WriteToken<'_>> {
        if self.starving_now() != 0 {
            return None;
        }
        self.take()
    }

    /// For a write at gate depth > 0, which must never wait: takes the lock if it's free, even
    /// while a waiter is starving. Deferring to the starving waiter there wouldn't let it in
    /// sooner (the nested write goes ahead either way, without the lock), it would only send the
    /// two into SQLite's busy handler together. A thread writing in a tight loop at depth > 0 can
    /// so jump ahead of starving waiters repeatedly; those writes would go ahead anyway.
    pub fn try_lock_nested(&self) -> Option<WriteToken<'_>> {
        self.take()
    }

    /// Takes the lock if it's free, whether or not anyone is starving.
    fn take(&self) -> Option<WriteToken<'_>> {
        let generation = current();
        let held = pack(generation, 1);
        let word = self.word.load(Ordering::Acquire);
        if state_now(word, generation) != 0 {
            return None;
        }
        self.word
            .compare_exchange(word, held, Ordering::AcqRel, Ordering::Acquire)
            .ok()
            .map(|_| WriteToken { lock: self, word: held })
    }

    /// Takes the lock, waiting through the gate's blocker (interpreter released) until `deadline`.
    /// Returns `None` if the deadline passes first. Must not be called while holding the gate.
    pub fn lock_until(&self, deadline: Instant) -> Option<WriteToken<'_>> {
        self.lock_until_after(deadline, STARVE_AFTER)
    }

    /// [`WriteLock::lock_until`] for a caller that just stepped aside for waiting writers (a
    /// maintenance loop between chunks). It has already waited its turn, so it starves at once
    /// instead of after `STARVE_AFTER`: otherwise a steady writer would cost `clear()` 20 ms per
    /// chunk.
    pub fn lock_until_with_priority(&self, deadline: Instant) -> Option<WriteToken<'_>> {
        self.lock_until_after(deadline, Duration::ZERO)
    }

    fn lock_until_after(&self, deadline: Instant, starve_after: Duration) -> Option<WriteToken<'_>> {
        if let Some(token) = self.try_lock() {
            return Some(token);
        }
        // Both counts are guards, so a wait that unwinds (a panic out of the blocker) takes them back:
        // a stuck `starving` would make every `try_lock` fail.
        let _waiting = Counted::new(&self.waiters);
        // No later than halfway to the deadline, so a short timeout still gets its turn.
        let start = now();
        let starve_at = start + starve_after.min(deadline.saturating_duration_since(start) / 2);
        let mut starving = None;
        loop {
            // A starving waiter ignores the starving count (its own included); others defer to it.
            let token = if starving.is_some() { self.take() } else { self.try_lock() };
            if token.is_some() || now() >= deadline {
                return token;
            }
            if starving.is_none() && now() >= starve_at {
                starving = Some(Counted::new(&self.starving));
                continue;
            }
            // Sleep with the interpreter released until the lock looks free to us, the deadline
            // passes, or it's time to start starving. A waiter that isn't starving keeps sleeping
            // while the lock is reserved for one that is: `try_lock` would refuse it anyway. The
            // blocker can't hand a token out of its `Fn`, so the next turn of the loop takes it.
            let is_starving = starving.is_some();
            let until = if is_starving { deadline } else { deadline.min(starve_at) };
            gate::run_blocking(&|| {
                thread::sleep(POLL);
                while (self.state() != 0 || (!is_starving && self.starving_now() != 0)) && now() < until {
                    thread::sleep(POLL);
                }
            });
        }
    }

    /// Threads currently waiting in [`WriteLock::lock_until`] (in this process generation).
    /// Maintenance loops yield between chunks while this is non-zero (fairness).
    #[must_use]
    pub fn waiters(&self) -> u64 {
        state_now(self.waiters.load(Ordering::Acquire), current())
    }

    fn state(&self) -> u64 {
        state_now(self.word.load(Ordering::Acquire), current())
    }

    fn starving_now(&self) -> u64 {
        state_now(self.starving.load(Ordering::Acquire), current())
    }
}

/// One count in a waiter counter ([`WriteLock`]'s `waiters` or `starving`), taken back on drop, so
/// a wait that unwinds can't leave it raised. Keep it bound to a named variable (`let _ =` drops it
/// at once). A forked child inherits a raised count but no guard, and reads it as 0. A guard only
/// sees an older generation if its own thread forked mid-wait, which the gate rules out, so that
/// check is defensive, as in [`SlotPermit`].
struct Counted<'a> {
    counter: &'a AtomicU64,
    generation: u64,
}

impl<'a> Counted<'a> {
    fn new(counter: &'a AtomicU64) -> Self {
        let generation = current();
        let _ = counter.try_update(Ordering::AcqRel, Ordering::Acquire, |word| {
            Some(pack(generation, state_now(word, generation).saturating_add(1)))
        });
        Self { counter, generation }
    }
}

impl Drop for Counted<'_> {
    fn drop(&mut self) {
        release_one(self.counter, self.generation);
    }
}

/// Takes one off `counter` if its value is still from `generation`; an older generation's count
/// (inherited across `fork()`) never included this one.
fn release_one(counter: &AtomicU64, generation: u64) {
    let _ = counter.try_update(Ordering::AcqRel, Ordering::Acquire, |word| {
        (word >> 32 == generation).then(|| pack(generation, (word & LOW).saturating_sub(1)))
    });
}

impl WriteToken<'_> {
    /// Detaches the hold from this token's lifetime, for a hold kept across calls (a held
    /// transaction). The hold must later be released with [`WriteLock::release_raw`] on the same
    /// lock. The returned word encodes the generation, so a release after `fork()` is a no-op.
    #[must_use]
    pub fn into_raw(self) -> u64 {
        let word = self.word;
        std::mem::forget(self);
        word
    }
}

impl WriteLock {
    /// Releases a hold detached with [`WriteToken::into_raw`].
    pub fn release_raw(&self, word: u64) {
        let _ = self.word.compare_exchange(word, 0, Ordering::AcqRel, Ordering::Acquire);
    }
}

impl Drop for WriteToken<'_> {
    fn drop(&mut self) {
        // Only the exact (generation, held) word this token took is released. In a forked child the
        // word belongs to an older generation, and the child's threads already treat it as free.
        let _ = self.lock.word.compare_exchange(self.word, 0, Ordering::AcqRel, Ordering::Acquire);
    }
}

/// A counting limit on pooled connections in use (`max_connections`).
///
/// Acquired before entering the gate, never while holding it. A nested operation at gate depth > 0
/// skips it, opening a connection over the cap instead of waiting for one it might itself hold.
#[derive(Debug, Default)]
pub struct Slots {
    /// State = slots in use.
    word: AtomicU64,
}

/// A reserved slot, released on drop (unless inherited across `fork()`).
#[derive(Debug)]
#[must_use = "dropping the permit releases the slot"]
pub struct SlotPermit<'a> {
    slots: &'a Slots,
    generation: u64,
}

impl Slots {
    #[must_use]
    pub const fn new() -> Self {
        Self { word: AtomicU64::new(0) }
    }

    /// Reserves a slot if fewer than `cap` are in use.
    pub fn try_acquire(&self, cap: u64) -> Option<SlotPermit<'_>> {
        let generation = current();
        self.word
            .try_update(Ordering::AcqRel, Ordering::Acquire, |word| {
                let used = state_now(word, generation);
                (used < cap).then(|| pack(generation, used + 1))
            })
            .ok()
            .map(|_| SlotPermit { slots: self, generation })
    }

    /// Reserves a slot, waiting through the gate's blocker until `deadline`.
    ///
    /// Test-and-test-and-set: the permit is only ever taken here, attached. The detached wait only
    /// watches a plain load until the count shows room (a short spin, then 50 µs sleeps)
    /// and returns, and the next turn takes the permit. A permit taken while detached would
    /// be held through the re-attach, up to a GIL holder's switch interval or a stop-the-world on a
    /// free-threaded build, and the blocker's `Fn` can't hand one back anyway. One detach per wait,
    /// not per poll. Holds no gate level, so a fork can land mid-wait. The only thing the wait holds
    /// is a spinner count (generation-packed), which a forked child reads as 0.
    pub fn acquire_until(&self, cap: u64, deadline: Instant) -> Option<SlotPermit<'_>> {
        loop {
            if let Some(permit) = self.try_acquire(cap) {
                return Some(permit);
            }
            if now() >= deadline {
                return None;
            }
            gate::run_blocking(&|| self.wait_for_room(cap, deadline));
            #[cfg(all(test, shuttle))]
            tests_support::phase(tests_support::Phase::Attached);
        }
    }

    /// Returns once fewer than `cap` slots look in use or `deadline` has passed. Takes nothing, so
    /// another thread may win the slot it saw free; the caller retries.
    fn wait_for_room(&self, cap: u64, deadline: Instant) {
        let room = || self.in_use() < cap;
        // Spin only while few other waiters in the process do: past that, the spinners take the
        // CPUs the slot holders need (more busy threads than CPUs). No spin, no count.
        if slot_spin() > 0
            && let Some(_spinner) = Spinner::admit()
            && spin(&room)
        {
            return;
        }
        #[cfg(all(test, shuttle))]
        tests_support::phase(tests_support::Phase::Poll);
        // Shorter sleeps don't help on Linux, whose default timer slack is 50 µs.
        while !room() && now() < deadline {
            thread::sleep(POLL);
        }
    }

    /// Slots in use in this process generation.
    #[must_use]
    pub fn in_use(&self) -> u64 {
        state_now(self.word.load(Ordering::Acquire), current())
    }
}

/// The spin phase; true once `room` held.
fn spin(room: &impl Fn() -> bool) -> bool {
    #[cfg(all(test, shuttle))]
    tests_support::phase(tests_support::Phase::Spin);
    // Loads with `spin_loop` hints between them, gaps doubling to 64, until the budget of hints
    // is spent. No clock reads (one costs more than the spin).
    let budget = slot_spin();
    let (mut spent, mut gap) = (0, 1);
    while spent < budget {
        if room() {
            return true;
        }
        for _ in 0..gap {
            std::hint::spin_loop();
        }
        spent += gap;
        gap = (gap * 2).min(64);
    }
    false
}

/// One of the process's slot waiters in the spin phase, at most [`slot_spinners`] at a
/// time, taken back on drop. Generation-packed like the other counts, so a fork mid-spin leaves the
/// child a count it reads as 0.
#[cfg(not(shuttle))]
struct Spinner {
    generation: u64,
}

#[cfg(not(shuttle))]
static SPINNERS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

#[cfg(not(shuttle))]
impl Spinner {
    fn admit() -> Option<Self> {
        let limit = u64::from(slot_spinners());
        let generation = current();
        SPINNERS
            .try_update(Ordering::AcqRel, Ordering::Acquire, |word| {
                let n = state_now(word, generation);
                (n < limit).then(|| pack(generation, n + 1))
            })
            .ok()
            .map(|_| Self { generation })
    }
}

#[cfg(not(shuttle))]
impl Drop for Spinner {
    fn drop(&mut self) {
        release_one(&SPINNERS, self.generation);
    }
}

/// Under the model checker every waiter spins: the limit is a CPU heuristic, not a correctness
/// rule, and a process-wide count would be shared between models running in parallel.
#[cfg(shuttle)]
struct Spinner;

#[cfg(shuttle)]
impl Spinner {
    #[allow(clippy::unnecessary_wraps)]
    fn admit() -> Option<Self> {
        Some(Self)
    }
}

impl Drop for SlotPermit<'_> {
    fn drop(&mut self) {
        // A permit from an older generation was inherited across fork(): the child's count never
        // included it, so there's nothing to release.
        release_one(&self.slots.word, self.generation);
    }
}

#[cfg(test)]
pub(crate) mod tests_support {
    use super::{WriteLock, current, pack};
    use std::sync::atomic::Ordering;

    /// Where a [`super::Slots::acquire_until`] wait is, for the models to aim at a phase. A std
    /// thread-local: shuttle runs one execution's threads on one OS thread, so this is one value per
    /// execution (models using it have a single waiter).
    #[cfg(shuttle)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub(crate) enum Phase {
        Attached,
        Spin,
        Poll,
    }

    #[cfg(shuttle)]
    std::thread_local! {
        static PHASE: std::cell::Cell<Phase> = const { std::cell::Cell::new(Phase::Attached) };
    }

    #[cfg(shuttle)]
    pub(crate) fn phase(p: Phase) {
        PHASE.with(|c| c.set(p));
    }

    #[cfg(shuttle)]
    pub(crate) fn current_phase() -> Phase {
        PHASE.with(std::cell::Cell::get)
    }

    /// Records one starving waiter in the current generation, as if a thread were waiting.
    pub(crate) fn mark_starving(lock: &WriteLock) {
        lock.starving.store(pack(current(), 1), Ordering::Release);
    }
}

#[cfg(all(test, not(shuttle)))]
mod tests {
    use super::*;

    fn soon(ms: u64) -> Instant {
        Instant::now() + Duration::from_millis(ms)
    }

    #[test]
    fn write_lock_is_exclusive_and_released_on_drop() {
        let lock = WriteLock::new();
        let token = lock.try_lock().unwrap();
        assert!(lock.try_lock().is_none());
        drop(token);
        assert!(lock.try_lock().is_some());
    }

    #[test]
    fn write_lock_waits_and_times_out() {
        let lock = WriteLock::new();
        let token = lock.try_lock().unwrap();
        let start = Instant::now();
        assert!(lock.lock_until(soon(20)).is_none());
        assert!(start.elapsed() >= Duration::from_millis(20));
        assert_eq!(lock.waiters(), 0);
        drop(token);
    }

    #[test]
    fn write_lock_serializes_threads_and_counts_waiters() {
        let lock = WriteLock::new();
        let counter = std::sync::atomic::AtomicU64::new(0);
        let held = lock.try_lock().unwrap();
        std::thread::scope(|s| {
            for _ in 0..4 {
                s.spawn(|| {
                    for _ in 0..200 {
                        let _token = lock.lock_until(soon(10_000)).unwrap();
                        // Non-atomic read-modify-write under the lock: lost updates would show.
                        let n = counter.load(Ordering::Relaxed);
                        counter.store(n + 1, Ordering::Relaxed);
                    }
                });
            }
            while lock.waiters() == 0 {
                std::thread::yield_now();
            }
            drop(held);
        });
        assert_eq!(counter.load(Ordering::Relaxed), 800);
        assert_eq!(lock.waiters(), 0);
    }

    #[test]
    fn a_waiter_is_not_starved_by_a_thread_that_keeps_retaking_the_lock() {
        // The free-threaded Linux failure: one thread re-takes the lock in a tight loop, as a
        // `set()` loop does: busy for about one SQLite write, then free for only an instant. The
        // other plays `clear()`: it steps aside between chunks, waits until the hammer has the
        // lock, then must win it back well within its deadline, every round.
        let lock = WriteLock::new();
        let stop = std::sync::atomic::AtomicBool::new(false);
        let mut waits = Vec::new();
        std::thread::scope(|s| {
            s.spawn(|| {
                while !stop.load(Ordering::Relaxed) {
                    if let Some(token) = lock.lock_until(soon(10_000)) {
                        let busy = Instant::now();
                        while busy.elapsed() < Duration::from_micros(20) {
                            std::hint::spin_loop();
                        }
                        drop(token);
                    }
                }
            });
            for _ in 0..50 {
                while lock.state() == 0 {
                    std::hint::spin_loop(); // Stepped aside: let the hammer in first.
                }
                let start = Instant::now();
                let token = lock.lock_until(soon(1_000));
                waits.push((token.is_some(), start.elapsed()));
                drop(token);
            }
            // Before any assertion, so a failure can't leave the hammer running and hang the scope.
            stop.store(true, Ordering::Relaxed);
        });
        for (got, waited) in &waits {
            assert!(*got, "a waiter was starved until its deadline");
            assert!(*waited < Duration::from_millis(200), "waited {waited:?}");
        }
        assert_eq!(state_now(lock.starving.load(Ordering::Acquire), current()), 0);
        assert_eq!(lock.waiters(), 0);
    }

    #[test]
    fn a_waiter_with_a_short_deadline_still_starves_in_time() {
        // As above, with a deadline shorter than STARVE_AFTER (a `Cache(timeout=0.01)`): the waiter
        // must start starving early enough to win the lock back before its deadline.
        let lock = WriteLock::new();
        let stop = std::sync::atomic::AtomicBool::new(false);
        let mut got = Vec::new();
        std::thread::scope(|s| {
            s.spawn(|| {
                while !stop.load(Ordering::Relaxed) {
                    if let Some(token) = lock.lock_until(soon(10_000)) {
                        let busy = Instant::now();
                        while busy.elapsed() < Duration::from_micros(20) {
                            std::hint::spin_loop();
                        }
                        drop(token);
                    }
                }
            });
            for _ in 0..50 {
                while lock.state() == 0 {
                    std::hint::spin_loop();
                }
                got.push(lock.lock_until(soon(10)).is_some());
            }
            stop.store(true, Ordering::Relaxed);
        });
        // Without the early start, every wait timed out (50 of 50). A few may still on a loaded
        // machine, since 10 ms is only a few scheduler time slices: under 8 CPU hogs on 8 CPUs in the
        // soak's Linux VM, 1 to 5 of 50 did (1 of 50 once in a full test run there).
        let lost = got.iter().filter(|g| !**g).count();
        assert!(lost <= 10, "{lost} of 50 waits with a 10 ms deadline timed out");
    }

    #[test]
    fn a_token_is_send_and_releases_from_another_thread() {
        let lock = WriteLock::new();
        let token = lock.try_lock().unwrap();
        std::thread::scope(|s| {
            s.spawn(move || drop(token));
        });
        assert!(lock.try_lock().is_some());
    }

    #[test]
    fn an_older_generations_hold_reads_as_free() {
        let lock = WriteLock::new();
        lock.word.store(pack(current().wrapping_sub(1) & LOW, 1), Ordering::Release);
        let token = lock.try_lock().expect("a parent's hold is free in the child");
        assert!(lock.try_lock().is_none());
        drop(token);
    }

    #[test]
    fn a_nested_writer_takes_a_free_lock_even_while_someone_starves() {
        let lock = WriteLock::new();
        tests_support::mark_starving(&lock);
        assert!(lock.try_lock().is_none(), "try_lock defers to a starving waiter");
        let token = lock.try_lock_nested().expect("a free lock goes to a nested writer");
        assert!(lock.try_lock_nested().is_none(), "but never twice");
        drop(token);
    }

    #[test]
    fn a_wait_that_unwinds_takes_its_counts_back() {
        // Exercises the guards directly: the blocker is a process-wide `OnceLock`, so a test can't
        // install one that panics inside `lock_until`.
        let lock = WriteLock::new();
        let unwound = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _waiting = Counted::new(&lock.waiters);
            let _starving = Counted::new(&lock.starving);
            assert_eq!(lock.waiters(), 1);
            assert!(lock.try_lock().is_none(), "a starving waiter must hold off try_lock");
            panic!("the blocker unwound");
        }));
        assert!(unwound.is_err());
        assert_eq!(lock.waiters(), 0);
        assert!(lock.try_lock().is_some(), "a starving count leaked by the unwind blocks try_lock");
    }

    #[test]
    fn a_count_inherited_across_fork_releases_nothing_in_the_child() {
        let lock = WriteLock::new();
        let older = current().wrapping_sub(1) & LOW;
        let inherited = Counted { counter: &lock.waiters, generation: older };
        let _mine = Counted::new(&lock.waiters);
        drop(inherited);
        assert_eq!(lock.waiters(), 1, "a parent's waiter took a count off the child's");
    }

    #[test]
    fn inherited_starving_and_waiter_counts_read_as_zero() {
        // A fork child inherits the counters of waiters left behind in the parent. They must not
        // block its own threads (a stuck `starving` would make every try_lock fail).
        let lock = WriteLock::new();
        let older = current().wrapping_sub(1) & LOW;
        lock.starving.store(pack(older, 1), Ordering::Release);
        lock.waiters.store(pack(older, 3), Ordering::Release);
        assert_eq!(lock.waiters(), 0);
        assert!(lock.try_lock().is_some(), "an inherited starving count blocked try_lock");
    }

    #[test]
    fn slots_cap_and_release() {
        let slots = Slots::new();
        let a = slots.try_acquire(2).unwrap();
        let _b = slots.try_acquire(2).unwrap();
        assert!(slots.try_acquire(2).is_none());
        assert!(slots.acquire_until(2, soon(10)).is_none());
        drop(a);
        assert!(slots.try_acquire(2).is_some());
    }

    #[test]
    fn the_slot_spin_is_off_on_two_or_fewer_cpus() {
        assert_eq!(slot_spin_for(Some(1)), 0);
        assert_eq!(slot_spin_for(Some(2)), 0);
        assert_eq!(slot_spin_for(Some(3)), SLOT_SPIN);
        assert_eq!(slot_spin_for(None), SLOT_SPIN, "an unknown count keeps the spin");
        let cpus = std::thread::available_parallelism().ok().map(std::num::NonZero::get);
        assert_eq!(slot_spin(), slot_spin_for(cpus));
    }

    #[test]
    fn spinners_are_capped_at_half_the_cpus_and_given_back() {
        assert_eq!(slot_spinners_for(Some(1)), 1);
        assert_eq!(slot_spinners_for(Some(8)), 4);
        assert_eq!(slot_spinners_for(Some(18)), 9);
        assert_eq!(slot_spinners_for(None), 4);
        // Other tests' waiters may hold some; take what's left, then one more must fail.
        let mut held = Vec::new();
        while let Some(s) = Spinner::admit() {
            held.push(s);
            assert!(held.len() <= slot_spinners() as usize, "admitted past the limit");
        }
        drop(held);
        // Other tests' waiters (`contended_slot_waits_...` runs in parallel) can fill the limit for a
        // while, but none holds a spinner for long, so one must come free well inside the deadline.
        let start = Instant::now();
        let spinner = loop {
            if let Some(s) = Spinner::admit() {
                break s;
            }
            assert!(start.elapsed() < Duration::from_secs(5), "spinner counts leaked");
            std::thread::yield_now();
        };
        drop(spinner);
    }

    #[test]
    fn a_timed_out_slot_wait_leaks_nothing() {
        let slots = Slots::new();
        let held = slots.try_acquire(1).unwrap();
        let start = Instant::now();
        assert!(slots.acquire_until(1, soon(5)).is_none());
        assert!(start.elapsed() >= Duration::from_millis(5));
        assert_eq!(slots.in_use(), 1, "the wait left a count behind");
        drop(held);
        assert_eq!(slots.in_use(), 0);
    }

    #[test]
    fn a_slot_waiter_gets_in_after_a_release() {
        let slots = Slots::new();
        let held = slots.try_acquire(1).unwrap();
        std::thread::scope(|s| {
            let waiter = s.spawn(|| slots.acquire_until(1, soon(10_000)).map(|permit| (Instant::now(), permit)));
            std::thread::sleep(Duration::from_millis(5)); // Well into the poll phase.
            let released = Instant::now();
            drop(held);
            let (got, permit) = waiter.join().unwrap().expect("the waiter timed out");
            // One POLL plus scheduling; generous for a loaded machine.
            assert!(got.duration_since(released) < Duration::from_millis(100));
            drop(permit);
        });
        assert_eq!(slots.in_use(), 0);
    }

    #[test]
    fn contended_slot_waits_keep_the_cap_and_never_time_out() {
        // More threads than slots, each taking and releasing in a loop with a short hold, as reads on
        // a FanoutCache shard do. A barger can win a slot a waiter saw free; every waiter must still
        // get in long before its deadline, and the cap must hold.
        let slots = Slots::new();
        let inside = std::sync::atomic::AtomicU64::new(0);
        let peak = std::sync::atomic::AtomicU64::new(0);
        let worst = std::sync::Mutex::new(Duration::ZERO);
        std::thread::scope(|s| {
            for _ in 0..8 {
                s.spawn(|| {
                    for _ in 0..2_000 {
                        let start = Instant::now();
                        let permit = slots.acquire_until(2, soon(10_000)).expect("a slot wait timed out");
                        let waited = start.elapsed();
                        let n = inside.fetch_add(1, Ordering::SeqCst) + 1;
                        peak.fetch_max(n, Ordering::SeqCst);
                        let busy = Instant::now();
                        while busy.elapsed() < Duration::from_micros(2) {
                            std::hint::spin_loop();
                        }
                        inside.fetch_sub(1, Ordering::SeqCst);
                        drop(permit);
                        let mut w = worst.lock().unwrap();
                        *w = (*w).max(waited);
                    }
                });
            }
        });
        assert!(peak.load(Ordering::SeqCst) <= 2, "more than the cap inside");
        assert_eq!(slots.in_use(), 0);
        let worst = *worst.lock().unwrap();
        // A real-time bound, so native only: Miri's clock follows instructions (or runs about 1000x
        // slow on the host's), while the cap and the no-timeout checks above hold under it too.
        if !cfg!(miri) {
            assert!(worst < Duration::from_secs(2), "a waiter waited {worst:?}");
        }
    }

    #[test]
    fn an_inherited_permit_releases_nothing() {
        let slots = Slots::new();
        let permit = SlotPermit { slots: &slots, generation: current().wrapping_sub(1) & LOW };
        let _live = slots.try_acquire(5).unwrap();
        drop(permit);
        assert_eq!(slots.in_use(), 1);
    }
}

#[cfg(all(test, shuttle))]
mod model;
