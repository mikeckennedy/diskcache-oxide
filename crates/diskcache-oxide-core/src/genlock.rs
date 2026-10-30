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
    pub fn acquire_until(&self, cap: u64, deadline: Instant) -> Option<SlotPermit<'_>> {
        loop {
            if let Some(permit) = self.try_acquire(cap) {
                return Some(permit);
            }
            if now() >= deadline {
                return None;
            }
            gate::run_blocking(&|| thread::sleep(POLL));
        }
    }

    /// Slots in use in this process generation.
    #[must_use]
    pub fn in_use(&self) -> u64 {
        state_now(self.word.load(Ordering::Acquire), current())
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
