//! The D-020 poison registry: which databases this process can never use, keyed by inode.
//!
//! SQLite keeps one lock record per database file per process, and `fork()` copies the record to
//! the child but not the kernel locks behind it. A child that inherits an *open connection* to a
//! file therefore holds, by SQLite's account, locks the kernel never gave it: a new connection
//! there takes none. Once the parent closes, its reads go stale and its writes are lost, with no
//! error; and with a write transaction open at the fork, releasing the "locks" in the child would
//! corrupt the parent's transaction. So such a child can't use that database at all (plan 32).
//!
//! Two things mark a file, both in the child's after-fork hook:
//!
//! - A thread that holds a write transaction across calls (a *hold*) records the database's
//!   [`FileId`] here for as long as the transaction is open, and the child marks every live hold
//!   (`poison_live_holds`). This half is atomics only, so it never depends on a mutex.
//! - The pool scan ([`crate::pool`]) marks every file a pool still has a connection on: a lease
//!   live at the fork, or anything left open by a fork whose before-hook never ran.
//!
//! [`check_usable`] then fails with [`Error::Unusable`] wherever a connection would be opened or
//! leased, and write paths call it first so they fail at once instead of waiting out a timeout.
//!
//! The check is by inode, not by store, because SQLite's lock record is per inode: a fresh store on
//! the same file in the child is refused too. A grandchild inherits the set and adds to it.
//!
//! # Across `fork()`
//!
//! Like the gate, the registry is atomics only, so a child never inherits it locked:
//!
//! - holds are added under the gate (the [`SharedGuard`] argument is the proof), by a thread whose
//!   level the fork waits for, so a child never sees a half-written entry;
//! - a hold is removed with one atomic store, from any thread;
//! - the unusable set is only added to in the child's after-fork hook, while the forking thread is
//!   the only one, and is only ever added to (tests aside: `unusable_for_test`'s entry goes when its
//!   guard drops).

use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicU64, Ordering};

use crate::gate::SharedGuard;
use crate::{Error, Result};

/// A database file's identity: `(st_dev, st_ino)`.
pub type FileId = (u64, u64);

/// The process-wide registry every store uses.
static REGISTRY: Registry = Registry::new();

/// Records that the calling thread holds a write transaction on `id` across calls, until the
/// returned guard is dropped.
///
/// Call it with the gate held (`gate` is the proof), before the level is turned into a
/// [`HeldShare`](crate::gate::HeldShare). Drop the guard after the transaction ends, but before
/// that share is dropped, so a fork never sees the transaction open and not registered. A parked end
/// drops the share first and keeps the hold, which only widens the window a child is poisoned in.
pub fn begin_hold(gate: &SharedGuard<'_>, id: FileId) -> HoldGuard<'static> {
    REGISTRY.begin_hold(gate, id)
}

/// Fails with [`Error::Unusable`] if this process was forked while a connection to `id` was open
/// (or descends from one that was): see the module docs. One relaxed load in a process that wasn't.
///
/// Only a check made under a gate level is final: in a child, the first gate entry is what runs
/// the after-fork scan when another library's `after_in_child` hook gets there before ours.
#[inline]
pub fn check_usable(id: FileId) -> Result<()> {
    REGISTRY.check_usable(id)
}

/// The after-fork hook in the child: `id` had a connection open at the fork. Call it only while
/// the calling thread is the only one in the process.
pub(crate) fn mark_unusable(id: FileId) {
    REGISTRY.mark_unusable(id);
}

/// The after-fork hook in the child, when it can't tell which files had a connection open (the pool
/// registry was inherited locked): no database is usable in this process.
pub(crate) fn mark_all_unusable() {
    REGISTRY.mark_all_unusable();
}

/// Whether a file was marked unusable since the last call, for the hook's one log line.
pub fn take_newly_unusable() -> bool {
    REGISTRY.newly_unusable.swap(false, Ordering::AcqRel)
}

/// Whether no database is usable in this process: the after-fork scan couldn't tell which files
/// had a connection open.
#[must_use]
pub fn all_unusable() -> bool {
    REGISTRY.all_unusable.load(Ordering::Acquire)
}

/// The after-fork hook in the child: marks every database a hold was live on at the fork as
/// unusable. Call it only while the calling thread is the only one in the process.
pub(crate) fn poison_live_holds() {
    REGISTRY.poison_live_holds();
}

/// Tests only: makes `id` unusable, as in a child that inherited an open connection to it, until the
/// returned guard is dropped.
#[cfg(test)]
pub(crate) fn unusable_for_test(id: FileId) -> HoldGuard<'static> {
    let slot = REGISTRY.unusable.insert(id);
    REGISTRY.any_unusable.store(true, Ordering::Release);
    HoldGuard { slot }
}

/// A live hold, see [`begin_hold`]. It may be dropped on any thread.
#[derive(Debug)]
#[must_use = "the hold is unregistered as soon as the guard is dropped"]
pub struct HoldGuard<'r> {
    slot: &'r Slot,
}

impl Drop for HoldGuard<'_> {
    fn drop(&mut self) {
        self.slot.state.store(FREE, Ordering::Release);
    }
}

/// The registry, as a type so tests can run on their own instance.
#[derive(Debug)]
pub(crate) struct Registry {
    live: IdList,
    /// The files an open connection was inherited to, held transactions' among them.
    unusable: IdList,
    /// Set once `unusable` has an entry or `all_unusable` is set. Only ever set, in a child before
    /// it has other threads.
    any_unusable: AtomicBool,
    /// Every file is unusable. Only ever set, like `any_unusable`, and before it.
    all_unusable: AtomicBool,
    /// Something was marked unusable and the hook hasn't logged it yet.
    newly_unusable: AtomicBool,
}

impl Registry {
    pub(crate) const fn new() -> Self {
        Self {
            live: IdList::new(),
            unusable: IdList::new(),
            any_unusable: AtomicBool::new(false),
            all_unusable: AtomicBool::new(false),
            newly_unusable: AtomicBool::new(false),
        }
    }

    pub(crate) fn begin_hold(&self, _gate: &SharedGuard<'_>, id: FileId) -> HoldGuard<'_> {
        HoldGuard { slot: self.live.insert(id) }
    }

    #[inline]
    pub(crate) fn check_usable(&self, id: FileId) -> Result<()> {
        // Relaxed: the flag is only set in a child before it starts other threads, and spawning a
        // thread synchronizes with it.
        if self.any_unusable.load(Ordering::Relaxed) {
            return self.check_usable_cold(id);
        }
        Ok(())
    }

    #[cold]
    fn check_usable_cold(&self, id: FileId) -> Result<()> {
        if self.all_unusable.load(Ordering::Relaxed) || self.unusable.contains(id) {
            return Err(Error::Unusable);
        }
        Ok(())
    }

    pub(crate) fn mark_unusable(&self, id: FileId) {
        if !self.unusable.contains(id) {
            let _ = self.unusable.insert(id);
            self.newly_unusable.store(true, Ordering::Release);
        }
        self.any_unusable.store(true, Ordering::Release);
    }

    pub(crate) fn mark_all_unusable(&self) {
        if !self.all_unusable.swap(true, Ordering::AcqRel) {
            self.newly_unusable.store(true, Ordering::Release);
        }
        self.any_unusable.store(true, Ordering::Release);
    }

    pub(crate) fn poison_live_holds(&self) {
        // A hold keeps its connection open. Marked here as well as by the pool scan, so it never
        // depends on a mutex the child may have inherited locked.
        self.live.for_each(|id| self.mark_unusable(id));
    }

    #[cfg(test)]
    fn live_ids(&self) -> Vec<FileId> {
        let mut ids = Vec::new();
        self.live.for_each(|id| ids.push(id));
        ids
    }
}

const FREE: u8 = 0;
const WRITING: u8 = 1;
const LIVE: u8 = 2;

#[derive(Debug)]
struct Slot {
    state: AtomicU8,
    dev: AtomicU64,
    ino: AtomicU64,
}

impl Slot {
    const fn new() -> Self {
        Self { state: AtomicU8::new(FREE), dev: AtomicU64::new(0), ino: AtomicU64::new(0) }
    }

    fn id(&self) -> FileId {
        (self.dev.load(Ordering::Relaxed), self.ino.load(Ordering::Relaxed))
    }
}

const CHUNK: usize = 32;

/// A lock-free multiset of ids: a chain of fixed chunks that grows and is never freed. One per
/// process holds as many slots as the most holds ever live at once, plus at most a chunk.
#[derive(Debug)]
struct IdList {
    slots: [Slot; CHUNK],
    next: OnceLock<Box<IdList>>,
}

impl IdList {
    const fn new() -> Self {
        Self { slots: [const { Slot::new() }; CHUNK], next: OnceLock::new() }
    }

    fn insert(&self, id: FileId) -> &Slot {
        let mut list = self;
        loop {
            for slot in &list.slots {
                if slot.state.compare_exchange(FREE, WRITING, Ordering::Acquire, Ordering::Relaxed).is_ok() {
                    slot.dev.store(id.0, Ordering::Relaxed);
                    slot.ino.store(id.1, Ordering::Relaxed);
                    slot.state.store(LIVE, Ordering::Release);
                    return slot;
                }
            }
            list = list.next.get_or_init(|| Box::new(Self::new()));
        }
    }

    /// Calls `f` with every live id. Exact only while nothing inserts concurrently (the child's
    /// after-fork hook, or a set only added to there).
    fn for_each(&self, mut f: impl FnMut(FileId)) {
        let mut list = Some(self);
        while let Some(current) = list {
            for slot in &current.slots {
                if slot.state.load(Ordering::Acquire) == LIVE {
                    f(slot.id());
                }
            }
            list = current.next.get().map(Box::as_ref);
        }
    }

    fn contains(&self, id: FileId) -> bool {
        let mut found = false;
        self.for_each(|live| found |= live == id);
        found
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gate::{Gate, ThreadState};

    crate::sync::thread_local! { static LOCAL: ThreadState = const { ThreadState::new() }; }
    static G: Gate = Gate::new(&LOCAL);

    const A: FileId = (1, 10);
    const B: FileId = (1, 11);
    const C: FileId = (2, 10);

    #[test]
    fn holds_are_listed_until_dropped() {
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        let a = registry.begin_hold(&gate, A);
        let a_again = registry.begin_hold(&gate, A);
        let b = registry.begin_hold(&gate, B);
        let mut live = registry.live_ids();
        live.sort_unstable();
        assert_eq!(live, vec![A, A, B]);
        drop(a);
        assert_eq!(registry.live_ids().iter().filter(|&&id| id == A).count(), 1, "a multiset: one A is left");
        drop((a_again, b));
        assert_eq!(registry.live_ids(), Vec::<FileId>::new());
    }

    #[test]
    fn a_hold_dropped_on_another_thread_is_unlisted() {
        // A scoped thread rather than a leaked `'static` registry, so Miri's leak check stays on.
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        let hold = registry.begin_hold(&gate, A);
        std::thread::scope(|s| {
            s.spawn(move || drop(hold));
        });
        assert_eq!(registry.live_ids(), Vec::<FileId>::new());
    }

    #[test]
    fn the_list_grows_past_a_chunk_and_reuses_slots() {
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        let holds: Vec<_> = (0..(CHUNK as u64 * 2 + 5)).map(|i| registry.begin_hold(&gate, (9, i))).collect();
        assert_eq!(registry.live_ids().len(), CHUNK * 2 + 5);
        drop(holds);
        assert_eq!(registry.live_ids(), Vec::<FileId>::new());
        let again: Vec<_> = (0..(CHUNK as u64 * 2 + 5)).map(|i| registry.begin_hold(&gate, (9, i))).collect();
        assert_eq!(registry.live_ids().len(), CHUNK * 2 + 5);
        assert!(registry.live.next.get().and_then(|n| n.next.get()).and_then(|n| n.next.get()).is_none());
        drop(again);
    }

    #[test]
    fn check_usable_passes_until_a_fork_marks_live_holds() {
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        let hold = registry.begin_hold(&gate, A);
        assert!(registry.check_usable(A).is_ok(), "the parent itself is never poisoned");

        // The child's after-fork hook, without forking.
        registry.poison_live_holds();
        assert!(matches!(registry.check_usable(A), Err(Error::Unusable)));
        assert!(registry.check_usable(B).is_ok(), "other files stay usable");
        assert!(registry.check_usable(C).is_ok(), "same inode, other device: another file");

        // Ending the inherited hold in the child doesn't make the file usable again.
        drop(hold);
        assert!(matches!(registry.check_usable(A), Err(Error::Unusable)));
    }

    #[test]
    fn a_fork_with_no_live_holds_marks_nothing() {
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        drop(registry.begin_hold(&gate, A));
        registry.poison_live_holds();
        assert!(!registry.any_unusable.load(Ordering::Relaxed));
        assert!(registry.check_usable(A).is_ok());
    }

    #[test]
    fn a_grandchild_inherits_the_unusable_set_and_adds_new_holds() {
        let registry = Registry::new();
        let gate = G.try_enter_shared().unwrap();
        let a = registry.begin_hold(&gate, A);
        registry.poison_live_holds(); // child
        drop(a);
        let b = registry.begin_hold(&gate, B);
        registry.poison_live_holds(); // grandchild, forked while the child held B
        assert!(matches!(registry.check_usable(A), Err(Error::Unusable)));
        assert!(matches!(registry.check_usable(B), Err(Error::Unusable)));
        // Repeated forks don't duplicate entries.
        registry.poison_live_holds();
        let mut unusable = Vec::new();
        registry.unusable.for_each(|id| unusable.push(id));
        unusable.sort_unstable();
        assert_eq!(unusable, vec![A, B]);
        drop(b);
    }

    #[test]
    fn a_file_marked_unusable_fails_the_check_and_others_pass() {
        let registry = Registry::new();
        assert!(registry.check_usable(A).is_ok());
        registry.mark_unusable(A);
        assert!(matches!(registry.check_usable(A), Err(Error::Unusable)));
        assert!(registry.check_usable(B).is_ok());
        assert!(registry.check_usable(C).is_ok(), "same inode, other device: another file");
        assert!(registry.newly_unusable.swap(false, Ordering::AcqRel), "flagged once for the log line");
        registry.mark_unusable(A);
        assert!(!registry.newly_unusable.load(Ordering::Acquire), "marking it again is not news");
    }

    #[test]
    fn all_unusable_fails_every_file() {
        let registry = Registry::new();
        registry.mark_all_unusable();
        for id in [A, B, C] {
            assert!(matches!(registry.check_usable(id), Err(Error::Unusable)));
        }
    }
}
