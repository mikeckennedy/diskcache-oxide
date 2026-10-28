//! Waiting for SQLite's write lock without freezing the interpreter (04-design.md section 7).
//!
//! SQLite calls a connection's busy handler on the thread that's running the statement, while that
//! thread is still attached to the interpreter and holds the fork gate. Sleeping there for the whole
//! `timeout` would freeze every thread on a GIL build, stall a free-threaded stop-the-world GC, and
//! deadlock against a fork. So waiting has two parts:
//!
//! 1. **A short attached attempt.** The busy handler polls (10 µs, doubling to 100 µs) only until the
//!    calling thread's [`AttachedWindow`] closes, about [`ATTACHED_WINDOW`] after it opened.
//! 2. **A detached retry.** When SQLite then reports busy, the caller gives everything back
//!    (transaction, connection, gate), sleeps with the interpreter released ([`backoff`]) and retries
//!    from the start, until the operation's own deadline.
//!
//! rusqlite's busy handler is a plain `fn` with no per-connection state. That works here because
//! SQLite runs it on the calling thread, so the window lives in a thread-local.

use std::cell::Cell;
use std::time::{Duration, Instant};

use rusqlite::Connection;

use crate::gate;
use crate::{Error, Result};

/// How long one attached attempt may busy-wait before giving up and retrying detached.
pub const ATTACHED_WINDOW: Duration = Duration::from_millis(2);

/// First and largest poll interval inside the attached window.
const FIRST_POLL: Duration = Duration::from_micros(10);
const MAX_POLL: Duration = Duration::from_micros(100);

/// Detached backoff: short sleeps at first, longer once a wait has gone on for a while.
const SHORT_BACKOFF: Duration = Duration::from_micros(100);
const LONG_BACKOFF: Duration = Duration::from_millis(1);
const LONG_AFTER: Duration = Duration::from_millis(20);

thread_local! {
    static WINDOW_ENDS: Cell<Option<Instant>> = const { Cell::new(None) };
}

/// Installs the busy handler on a connection. Without an open [`AttachedWindow`] on the calling
/// thread it never waits: SQLite reports busy at once.
pub fn install(conn: &mut Connection) -> rusqlite::Result<()> {
    conn.busy_handler(Some(busy_handler))
}

fn busy_handler(count: i32) -> bool {
    let Some(ends) = WINDOW_ENDS.with(Cell::get) else { return false };
    let now = Instant::now();
    if now >= ends {
        return false;
    }
    let shift = u32::try_from(count).unwrap_or(0).min(4);
    let poll = (FIRST_POLL * (1 << shift)).min(MAX_POLL).min(ends - now);
    std::thread::sleep(poll);
    true
}

/// Opens the calling thread's attached busy-wait window until it's dropped: [`ATTACHED_WINDOW`] from
/// now, or the operation's `deadline` if that's sooner. Windows don't nest; an inner one restores
/// the outer one on drop.
#[derive(Debug)]
#[must_use = "the window closes when this guard is dropped"]
pub struct AttachedWindow {
    previous: Option<Instant>,
}

impl AttachedWindow {
    pub fn open(deadline: Instant) -> Self {
        let ends = (Instant::now() + ATTACHED_WINDOW).min(deadline);
        let previous = WINDOW_ENDS.with(|w| w.replace(Some(ends)));
        Self { previous }
    }
}

impl Drop for AttachedWindow {
    fn drop(&mut self) {
        WINDOW_ENDS.with(|w| w.set(self.previous));
    }
}

/// Sleeps before the next detached retry, with the interpreter released. `waited` is how long the
/// operation has been retrying. Must be called with no gate level, connection or transaction held.
pub fn backoff(waited: Duration) {
    let pause = if waited < LONG_AFTER { SHORT_BACKOFF } else { LONG_BACKOFF };
    gate::run_blocking(&|| std::thread::sleep(pause));
}

/// The longest wait a deadline can represent; longer timeouts wait this long.
pub const MAX_TIMEOUT: Duration = Duration::from_hours(100 * 365 * 24);

/// `start + timeout`, which can't overflow: a huge `timeout` waits [`MAX_TIMEOUT`] instead of
/// panicking (`Instant + Duration` panics on overflow).
#[must_use]
pub fn deadline_after(start: Instant, timeout: Duration) -> Instant {
    start.checked_add(timeout.min(MAX_TIMEOUT)).or_else(|| start.checked_add(Duration::from_hours(24))).unwrap_or(start)
}

/// Runs `attempt` until it stops reporting a lock timeout or `timeout` runs out.
///
/// Each attempt gets its own [`AttachedWindow`]. Between attempts it [`backoff`]s with the
/// interpreter released, so `attempt` must acquire everything it needs (gate level, connection,
/// transaction) itself and release it all before returning. Retrying from the start is safe because
/// operations encode values and write value files before they start a transaction. When time runs
/// out, the error is a [`Error::Timeout`] for the whole `timeout`.
pub fn retry<T>(timeout: Duration, mut attempt: impl FnMut(Instant) -> Result<T>) -> Result<T> {
    let start = Instant::now();
    let deadline = deadline_after(start, timeout);
    loop {
        let outcome = {
            let _window = AttachedWindow::open(deadline);
            attempt(deadline)
        };
        match outcome {
            Err(Error::Timeout { .. }) if Instant::now() < deadline => backoff(start.elapsed()),
            Err(Error::Timeout { .. }) => return Err(Error::Timeout { seconds: timeout.as_secs_f64() }),
            other => return other,
        }
    }
}

#[cfg(test)]
mod tests {
    use rusqlite::TransactionBehavior;

    use super::*;
    use crate::open::{self, Requested};
    use crate::testutil::TempDir;

    #[test]
    fn a_huge_timeout_makes_a_far_deadline_not_a_panic() {
        let start = Instant::now();
        let far = deadline_after(start, Duration::MAX);
        assert!(far > start + Duration::from_hours(24 * 365));
        assert_eq!(deadline_after(start, Duration::from_secs(3)), start + Duration::from_secs(3));
    }

    #[test]
    fn without_a_window_busy_is_reported_at_once() {
        assert!(!busy_handler(0));
    }

    #[test]
    fn a_window_waits_about_its_length_then_reports_busy() {
        let tmp = TempDir::new("lockwait");
        open::ensure_directory(tmp.path()).unwrap();
        let mut holder = open::connect(tmp.path(), Duration::from_secs(5)).unwrap();
        open::prepare(&mut holder, Requested::default(), Duration::from_secs(5)).unwrap();
        let tx = holder.transaction_with_behavior(TransactionBehavior::Immediate).unwrap();

        let mut waiter = open::connect(tmp.path(), Duration::from_secs(5)).unwrap();
        let start = Instant::now();
        let result = {
            let _window = AttachedWindow::open(Instant::now() + Duration::from_secs(60));
            waiter.transaction_with_behavior(TransactionBehavior::Immediate).map(|_| ())
        };
        let elapsed = start.elapsed();
        assert!(result.is_err(), "the lock is held, so the attempt must report busy");
        assert!(elapsed >= ATTACHED_WINDOW, "gave up after {elapsed:?}");
        assert!(elapsed < Duration::from_millis(250), "waited {elapsed:?}, far past the window");
        drop(tx);
        assert!(WINDOW_ENDS.with(Cell::get).is_none(), "the window must close on drop");
    }
}
