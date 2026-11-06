//! A lock-free stack that is only pushed to and emptied whole, for state a fork can land in the
//! middle of. A push lands with one compare-and-swap or not at all, and emptying it is one swap, so
//! a thread a fork leaves behind never leaves the child anything held: the child can always push and
//! take. A push the fork cut off loses only its own value, which then is never dropped in the child.
//!
//! There is no pop of a single node, so the ABA problem can't arise: a node is only freed by the
//! thread that swapped the whole list out, after which nobody else can reach it.
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
    next: *mut Node<T>,
}

impl<T> TakeAllStack<T> {
    pub(crate) const fn new() -> Self {
        Self { head: AtomicPtr::new(ptr::null_mut()), _owns: PhantomData }
    }

    pub(crate) fn push(&self, value: T) {
        let node = Box::into_raw(Box::new(Node { value, next: ptr::null_mut() }));
        // Relaxed: the old head only goes into `next`, never read through here.
        let mut head = self.head.load(Ordering::Relaxed);
        loop {
            // SAFETY: `node` came from `Box::into_raw` above and is published only by the exchange
            // below succeeding, after which this loop ends, so until then this thread is its only user.
            unsafe { (*node).next = head };
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
            // once, here, as the walk moves past it.
            let node = unsafe { Box::from_raw(next) };
            let Node { value, next: below } = *node;
            taken.push(value);
            next = below;
        }
        taken
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

    /// Pushers racing takers on real threads: every value comes out exactly once.
    #[test]
    fn concurrent_pushes_and_takes_lose_and_repeat_nothing() {
        const THREADS: usize = 8;
        const EACH: usize = 20_000;
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
