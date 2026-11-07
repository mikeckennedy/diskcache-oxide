//! A lock-free stack that is only pushed to and emptied whole, for state a fork can land in the
//! middle of. A push lands with one compare-and-swap or not at all, and emptying it is one swap, so
//! a thread a fork leaves behind never leaves the child anything held: the child can always push and
//! take. A push the fork cut off loses only its own value, which then is never dropped in the child.
//!
//! There is no pop of a single node, so no node is ever freed while another thread can still reach
//! it through the list: a node is only freed by the thread that swapped the whole list out.
//!
//! ABA still happens at the address level, and the code is written for it. A pusher loads `head`
//! (node n1), a taker swaps the list out and frees n1, another pusher's new node m is allocated at
//! n1's address and published, and the first pusher's compare-and-swap succeeds, since the atomic
//! compares addresses: its node's `next` is then the address of a live node, m, but a pointer to it
//! would carry n1's provenance, a dead allocation, and reading through it would be undefined
//! behavior (Miri found exactly that, plan 14, lane A, seed 31). So `next` is an address, every
//! node's allocation is exposed when it is created, and the walk rebuilds each pointer with
//! `with_exposed_provenance_mut`: a successful compare-and-swap on an address means a live, exposed
//! node sits there, and that is the node the walk must visit. The list itself is right either way:
//! the pusher linked under whatever the head was at the moment of the exchange.
//!
//! One of two modules in core that may use `unsafe`, with `sqlite_mutex` (see
//! `tests::only_these_modules_allow_unsafe`).

#![allow(unsafe_code)]
#![warn(clippy::undocumented_unsafe_blocks)]

use std::marker::PhantomData;
use std::ptr;
use std::sync::atomic::Ordering;

use crate::sync::atomic::AtomicPtr;

/// See the module docs. Values come back newest first.
pub(crate) struct TakeAllStack<T> {
    head: AtomicPtr<Node<T>>,
    /// Owns the values, for the drop check, and makes the stack `Send` and `Sync` only when `T` is
    /// (`AtomicPtr` is both for any `T`).
    _owns: PhantomData<T>,
}

struct Node<T> {
    value: T,
    /// The address of the node below, 0 at the bottom. An address and not a pointer: see the module
    /// docs on ABA and provenance.
    next: usize,
}

impl<T> TakeAllStack<T> {
    pub(crate) const fn new() -> Self {
        Self { head: AtomicPtr::new(ptr::null_mut()), _owns: PhantomData }
    }

    pub(crate) fn push(&self, value: T) {
        let node = Box::into_raw(Box::new(Node { value, next: 0 }));
        // Exposes the node's allocation, so a walk that reaches this node by address can rebuild a
        // pointer to it (module docs). The address itself isn't needed here.
        let _ = node.expose_provenance();
        // Relaxed: the old head only goes into `next` as an address, never read through here.
        let mut head = self.head.load(Ordering::Relaxed);
        loop {
            #[cfg(all(test, not(shuttle)))]
            hooks::between_load_and_exchange(head.addr());
            // SAFETY: `node` came from `Box::into_raw` above and is published only by the exchange
            // below succeeding, after which this loop ends, so until then this thread is its only user.
            unsafe { (*node).next = head.addr() };
            // Release publishes the node. Each push is a read-modify-write, so it carries on the
            // release sequence of the pushes under it, and one acquiring swap sees them all.
            match self.head.compare_exchange_weak(head, node, Ordering::Release, Ordering::Relaxed) {
                Ok(_) => return,
                Err(current) => head = current,
            }
        }
    }

    /// Empties the stack, newest first.
    pub(crate) fn take_all(&self) -> Vec<T> {
        let mut next = self.head.swap(ptr::null_mut(), Ordering::Acquire);
        let mut taken = Vec::new();
        while !next.is_null() {
            // SAFETY: every node in the list came from `Box::into_raw` in `push` and was published
            // with Release, which the Acquire swap above synchronizes with. The swap detached the
            // whole list, so nobody else can reach these nodes, and each is turned back into a `Box`
            // once, here, as the walk moves past it. The head pointer has its node's own provenance;
            // each later one is rebuilt from an address below, which names a live node (only a walk
            // frees list nodes, and this walk has owned the whole list since its swap) whose
            // allocation `push` exposed (module docs).
            let node = unsafe { Box::from_raw(next) };
            let Node { value, next: below } = *node;
            taken.push(value);
            next = ptr::with_exposed_provenance_mut(below);
        }
        taken
    }
}

/// Test hooks into `push`, for the ABA case the module docs describe. Nothing in production code
/// calls them, and the hook is a no-op unless a test arms it on its own thread. Not under shuttle:
/// the models don't use it, and its functions would be dead code there.
#[cfg(all(test, not(shuttle)))]
mod hooks {
    use std::cell::Cell;

    /// What a test wants done between a pusher's load of `head` and its exchange, given the address
    /// that `head` names; returns whether it managed what it set out to do.
    type Action = Box<dyn FnMut(usize) -> bool>;

    thread_local! {
        /// The action armed on this thread, taken out while it runs (it re-enters `push`).
        static ARMED: Cell<Option<Action>> = const { Cell::new(None) };
        /// Whether the last action that ran returned true.
        static DONE: Cell<bool> = const { Cell::new(false) };
    }

    /// Arms `action` to run once on this thread, at the next `push` whose loaded head isn't null.
    pub(super) fn arm(action: Action) {
        DONE.with(|d| d.set(false));
        ARMED.with(|a| a.set(Some(action)));
    }

    /// Whether the last armed action reported success.
    pub(super) fn done() -> bool {
        DONE.with(Cell::get)
    }

    pub(super) fn between_load_and_exchange(stale: usize) {
        if stale == 0 {
            return;
        }
        let Some(mut action) = ARMED.with(Cell::take) else { return };
        DONE.with(|d| d.set(action(stale)));
    }
}

impl<T> Drop for TakeAllStack<T> {
    fn drop(&mut self) {
        drop(self.take_all());
    }
}

#[cfg(all(test, not(shuttle)))]
mod tests {
    use std::path::Path;
    use std::sync::Arc;

    use super::*;

    #[test]
    fn take_all_returns_every_push_newest_first_and_empties_it() {
        let stack = TakeAllStack::new();
        assert_eq!(stack.take_all(), Vec::<i32>::new());
        for i in 0..5 {
            stack.push(i);
        }
        assert_eq!(stack.take_all(), [4, 3, 2, 1, 0]);
        assert_eq!(stack.take_all(), Vec::<i32>::new());
        stack.push(9);
        assert_eq!(stack.take_all(), [9]);
    }

    #[test]
    fn dropping_the_stack_drops_what_is_still_in_it() {
        let value = Arc::new(());
        let stack = TakeAllStack::new();
        for _ in 0..3 {
            stack.push(Arc::clone(&value));
        }
        assert_eq!(Arc::strong_count(&value), 4);
        drop(stack);
        assert_eq!(Arc::strong_count(&value), 1);
    }

    /// The ABA case from the module docs, made to happen: between a pusher's load of `head` (node
    /// n1) and its exchange, the stack is emptied (freeing n1) and refilled until a new node lands
    /// at n1's address, so the exchange succeeds against a node the pusher's pointer never knew.
    /// The walk must then reach that node through the address, not through n1's dead provenance:
    /// under Miri the old code reported a dangling box here (plan 14, lane A, seed 31). Many rounds,
    /// because two things have to go the test's way in a round: the allocator must hand n1's
    /// address out again (Miri pools a freed address with probability 0.5 by default; a native
    /// allocator nearly always reuses it at once), and the weak exchange must not fail spuriously
    /// (Miri makes it fail 80% of the time by default; a failure re-reads the head and takes the
    /// safe path). Under Miri's defaults a round takes the stale path about one time in ten, so 64
    /// rounds miss it about once in a thousand runs, and the lane runs 32 seeds.
    #[test]
    fn a_push_whose_head_was_freed_and_reused_links_to_the_live_node() {
        static STACK: TakeAllStack<usize> = TakeAllStack::new();
        let mut reused = 0;
        for _ in 0..64 {
            STACK.push(1); // n1, the head the racing push loads.
            hooks::arm(Box::new(|stale| {
                drop(STACK.take_all()); // Frees n1, the only node freed in this round, so its
                // address is the one the allocator (or Miri's reuse pool) has to hand out again.
                for _ in 0..1_000 {
                    STACK.push(99); // m, hoping for n1's address; kept, never freed, if it isn't.
                    if STACK.head.load(Ordering::Relaxed).addr() == stale {
                        return true;
                    }
                }
                false
            }));
            STACK.push(2);
            let got = STACK.take_all();
            assert_eq!(got.first(), Some(&2), "the racing push landed on top: {got:?}");
            assert!(got[1..].iter().all(|v| *v == 99), "every replacement came back, once: {got:?}");
            assert!(got.len() <= 1_001);
            reused += usize::from(hooks::done());
        }
        // The assertions above hold either way; Miri's verdict on the old code was a dangling box in
        // any round where the address came back. Natively that is nearly every round; under Miri's
        // defaults about half. Zero would mean the test no longer exercises the case.
        eprintln!("address reused in {reused} of 64 rounds");
        assert!(reused > 0, "the allocator never handed the freed address out again, so nothing was tested");
    }

    /// Pushers racing takers on real threads: every value comes out exactly once.
    #[test]
    fn concurrent_pushes_and_takes_lose_and_repeat_nothing() {
        const THREADS: usize = 8;
        // Miri explores interleavings with many seeds rather than many values, and 160,000 values
        // cost it about eight minutes a seed (plan 14, lane A).
        const EACH: usize = if cfg!(miri) { 200 } else { 20_000 };
        let stack = Arc::new(TakeAllStack::new());
        let done = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let pushers: Vec<_> = (0..THREADS)
            .map(|t| {
                let (stack, done) = (Arc::clone(&stack), Arc::clone(&done));
                std::thread::spawn(move || {
                    for i in 0..EACH {
                        stack.push(t * EACH + i);
                    }
                    done.fetch_add(1, Ordering::SeqCst);
                })
            })
            .collect();
        let takers: Vec<_> = (0..2)
            .map(|_| {
                let (stack, done) = (Arc::clone(&stack), Arc::clone(&done));
                std::thread::spawn(move || {
                    let mut got = Vec::new();
                    while done.load(Ordering::SeqCst) < THREADS {
                        got.extend(stack.take_all());
                    }
                    got
                })
            })
            .collect();
        for pusher in pushers {
            pusher.join().unwrap();
        }
        let mut got: Vec<usize> = takers.into_iter().flat_map(|t| t.join().unwrap()).collect();
        got.extend(stack.take_all());
        got.sort_unstable();
        assert_eq!(got, (0..THREADS * EACH).collect::<Vec<_>>());
    }

    /// Core is `deny(unsafe_code)`, and this module and `sqlite_mutex` (SQLite's mutex methods, plan
    /// 12 step 5a) are the exceptions. Another one is a design change, so it should fail here first.
    #[test]
    fn only_these_modules_allow_unsafe() {
        fn scan(dir: &Path, found: &mut Vec<String>) {
            for entry in std::fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    scan(&path, found);
                } else if path.extension().is_some_and(|e| e == "rs") {
                    let text = std::fs::read_to_string(&path).unwrap();
                    if text.lines().any(|l| l.contains("unsafe_code") && !l.trim_start().starts_with("//")) {
                        found.push(path.display().to_string());
                    }
                }
            }
        }
        let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let mut found = Vec::new();
        scan(&src, &mut found);
        found.sort();
        let expected =
            [src.join("lib.rs"), src.join("sqlite_mutex.rs"), src.join("stack.rs")].map(|p| p.display().to_string());
        assert_eq!(
            found, expected,
            "only lib.rs (the deny), sqlite_mutex.rs and stack.rs (the allows) may name unsafe_code"
        );
    }
}

#[cfg(all(test, shuttle))]
mod model {
    use std::sync::Arc;

    use shuttle::thread;

    use super::TakeAllStack;
    use crate::model::{Reach, check};

    /// Two pushers and a taker that takes twice: every value comes out exactly once, whatever the
    /// interleaving, and some schedule takes between pushes.
    #[test]
    fn model_every_push_is_taken_exactly_once() {
        static PARTIAL: Reach = Reach::new();
        check(|| {
            let stack = Arc::new(TakeAllStack::new());
            let pushers: Vec<_> = (0..2)
                .map(|t| {
                    let stack = Arc::clone(&stack);
                    thread::spawn(move || {
                        for i in 0..2 {
                            stack.push(t * 10 + i);
                        }
                    })
                })
                .collect();
            let taker = {
                let stack = Arc::clone(&stack);
                thread::spawn(move || {
                    let mut got = stack.take_all();
                    got.extend(stack.take_all());
                    got
                })
            };
            for pusher in pushers {
                pusher.join().unwrap();
            }
            let mut got = taker.join().unwrap();
            if !got.is_empty() && got.len() < 4 {
                PARTIAL.hit();
            }
            got.extend(stack.take_all());
            got.sort_unstable();
            assert_eq!(got, [0, 1, 10, 11]);
        });
        PARTIAL.assert_reached("a take between pushes");
    }
}
