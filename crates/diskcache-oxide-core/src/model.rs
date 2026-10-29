//! The shuttle harness shared by `gate::model` and `genlock::model`. Run with scripts/model-check.sh.

use std::time::Duration;

use shuttle::scheduler::{PctScheduler, RandomScheduler};
use shuttle::{Config, MaxSteps, PortfolioRunner};

/// Schedules per scheduler. `DCO_MODEL_ITERATIONS` raises it for the `--slow` run.
fn iterations() -> usize {
    std::env::var("DCO_MODEL_ITERATIONS").ok().and_then(|v| v.parse().ok()).unwrap_or(100_000)
}

/// Runs `model` under a random scheduler and PCT at depths 2 and 3, stopping at the first failure,
/// which prints a schedule `shuttle::replay` reproduces exactly (the logical clock keeps it
/// deterministic).
pub(crate) fn check(model: impl Fn() + Send + Sync + 'static) {
    // DCO_MODEL_REPLAY=<the schedule a failure printed> replays it exactly (pick the model with the
    // test-name filter), with the clock reset as in a normal run.
    if let Ok(schedule) = std::env::var("DCO_MODEL_REPLAY") {
        shuttle::replay(
            move || {
                crate::sync::clock::reset();
                model();
            },
            &schedule,
        );
        return;
    }
    let n = iterations();
    let mut config = Config::new();
    // A backstop only: the logical clock's LIVELOCK assert names a stuck model first.
    config.max_steps = MaxSteps::FailAfter(2_000_000);
    config.max_time = std::env::var("DCO_MODEL_SECONDS").ok().and_then(|v| v.parse().ok()).map(Duration::from_secs);
    // Shuttle runs every ordering as SeqCst. The gate is SeqCst throughout; genlock's
    // Acquire/AcqRel words are single-location RMW protocols, where coherence is all that matters.
    config.silence_warnings = true;
    let mut runner = PortfolioRunner::new(true, config);
    runner.add(RandomScheduler::new(n));
    runner.add(PctScheduler::new(2, n));
    runner.add(PctScheduler::new(3, n));
    runner.run(move || {
        crate::sync::clock::reset();
        model();
    });
}

/// Counts, across a run's executions, how often a model reached the state it exists to test, so a
/// pass can't be vacuous: [`Reach::assert_reached`] after [`check`] fails if no schedule got there.
/// A std atomic, outside shuttle's control, shared by every execution of the run.
pub(crate) struct Reach(std::sync::atomic::AtomicUsize);

impl Reach {
    pub(crate) const fn new() -> Self {
        Self(std::sync::atomic::AtomicUsize::new(0))
    }

    pub(crate) fn hit(&self) {
        self.0.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    }

    pub(crate) fn assert_reached(&self, what: &str) {
        if std::env::var_os("DCO_MODEL_REPLAY").is_none() {
            assert!(self.0.load(std::sync::atomic::Ordering::Relaxed) > 0, "no schedule reached: {what}");
        }
    }
}
