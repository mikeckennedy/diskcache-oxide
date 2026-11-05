//! The primitives `gate`, `genlock` and `stack` synchronize through: std's, or shuttle's under
//! `cfg(shuttle)` so the model checker (`crate::model`) controls every interleaving. Only they (and a
//! gate test in `poison`, for its thread-local) import this.
//!
//! Under shuttle, `thread::sleep` is a yield (a scheduling point that also tells PCT to deprioritize
//! the sleeper) that advances a logical clock by the slept duration, and `now()` reads that clock, so
//! deadlines and `STARVE_AFTER` pass after a deterministic number of polls and failing schedules
//! replay exactly.

pub(crate) use std::sync::Arc;

#[cfg(not(shuttle))]
pub(crate) use std::{sync::atomic, thread, thread::LocalKey, thread_local};

#[cfg(not(shuttle))]
#[inline]
pub(crate) fn now() -> std::time::Instant {
    std::time::Instant::now()
}

#[cfg(shuttle)]
pub(crate) use shuttle::{thread::LocalKey, thread_local};

#[cfg(shuttle)]
pub(crate) mod atomic {
    pub(crate) use shuttle::sync::atomic::{AtomicBool, AtomicPtr, AtomicU64, AtomicUsize};
}

#[cfg(shuttle)]
pub(crate) mod thread {
    /// A scheduling point that lets logical time pass.
    pub(crate) fn sleep(dur: std::time::Duration) {
        super::clock::advance(dur);
        shuttle::thread::yield_now();
    }
}

#[cfg(shuttle)]
pub(crate) use clock::now;

#[cfg(shuttle)]
pub(crate) mod clock {
    use std::cell::Cell;
    use std::time::{Duration, Instant};

    /// Logical time past which a model is stuck rather than slow: every wait in the models has a
    /// deadline or a partner far below this, so reaching it is a livelock.
    pub(crate) const LIVELOCK: Duration = Duration::from_secs(60);

    // A std thread-local, not a shuttle one: shuttle runs one execution's threads as coroutines on
    // the OS thread that called the runner, so this is one clock per execution, shared by its
    // modelled threads and private to it from other #[test]s running in parallel.
    std::thread_local! {
        static BASE: Instant = Instant::now();
        static ELAPSED: Cell<Duration> = const { Cell::new(Duration::ZERO) };
    }

    /// Starts an execution at logical time zero (`crate::model::check` calls it first).
    #[cfg(test)]
    pub(crate) fn reset() {
        ELAPSED.with(|e| e.set(Duration::ZERO));
    }

    pub(crate) fn advance(dur: Duration) {
        let t = ELAPSED.with(|e| {
            let t = e.get() + dur;
            e.set(t);
            t
        });
        assert!(t < LIVELOCK, "livelock: logical time reached {t:?} without the model finishing");
    }

    pub(crate) fn now() -> Instant {
        BASE.with(|b| *b) + ELAPSED.with(Cell::get)
    }
}
