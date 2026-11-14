//! One maintenance turn on one store (plan 28, decisions 5 and 9-13): the tasks [`super::schedule`]
//! says are due, in order, never making a request wait.
//!
//! - **Expire, then cull.** One no-wait read first: the claim row, whether anything has expired, and
//!   whether the cache is over `size_limit` under the evicting policy. A tidy cache stops there,
//!   having written nothing. Then chunks, each its own no-wait write: [`CHUNK`] expired rows at a
//!   time first, then the least recently stored, `cull_limit` at a time (at most [`CHUNK`]), while the
//!   cache is over its limit. Never under `eviction_policy='none'`, and never with a stored
//!   `cull_limit` of 0, which in diskcache means "no automatic culling": the app culls itself
//!   (Michael, 2026-11-13). Following `cull_limit` also means a background trim never evicts more at
//!   once than the `set` it stands in for.
//! - **The WAL checkpoint**, only past its trim size ([`Store::background_checkpoint`]).
//! - **Value folders**: dead temporary files and empty folders, hourly, a `FanoutCache`'s shards
//!   included (the close-time sweep skips them for its budget's sake). The walk has its own budget,
//!   [`spill::SWEEP_BUDGET`], outside the turn's, and a close may wait for it, once an hour.
//! - **Connection trimming** is a placeholder until step 6.
//!
//! The turn ends early ([`Stop`]) when its budget runs out, when a request writer is waiting for
//! this process's write lock, when a call is refused (a busy lock, a pending fork), or when the store
//! retires. A store poisoned by a fork (D-020) gets no turn at all.
//!
//! **Taking turns across processes.** Expiry and the folder sweep each have a claim row in
//! `settings` holding the wall time of the last turn that did the work. A process skips the work
//! while that's less than 90% of its own cadence ago, unless the row holds the time this process
//! wrote itself (its own work, continued). The 90% lets a process whose ticks run a little ahead of
//! the claimer's take the work over once and keep it, rather than the two trading it on jitter. The
//! shortest interval among the processes sets the pace. The row is read before any work and again
//! inside the first write, under `BEGIN IMMEDIATE`, so two processes that both saw an old row don't
//! both do the work. A time more than a few seconds in the future is stale (the clock stepped back).
//! Builds that don't know the rows ignore them, since settings are read by name. The folder sweep's
//! claim is a write once an hour per store even when there's nothing to sweep: the one write a tidy
//! cache sees.

use std::time::{Duration, Instant};

use rusqlite::Connection;
use rusqlite::types::ValueRef;

use super::schedule::{Task, TaskSet};
use crate::maintenance::CHUNK;
use crate::ops::Fill;
use crate::store::{BackgroundTurn, Changed, Store};
use crate::{Error, Result, gate, spill};

/// The longest a turn keeps taking chunks (decision 9). An expiry chunk holds the write lock for
/// about 130 µs (100 inline rows), so this is a few hundred chunks.
pub(crate) const TURN_BUDGET: Duration = Duration::from_millis(50);

/// A claim this far in the future is stale: the wall clock stepped back since it was written.
const FUTURE_SLACK_S: f64 = 5.0;

/// Another process's claim counts while it's less than this share of the cadence old.
const FRESH_SHARE: f64 = 0.9;

/// A claim row in `settings`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Claim {
    Expire = 0,
    Dirs = 1,
}

impl Claim {
    const fn row(self) -> &'static str {
        match self {
            Self::Expire => "maintenance.expire_at",
            Self::Dirs => "maintenance.dirs_at",
        }
    }
}

/// Why a turn ended before finishing what was due.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Stop {
    /// Its budget ran out, having made progress: healthy, the work just continues.
    Budget,
    /// A request writer in this process was waiting for the write lock: the turn stepped aside.
    Yielded,
    /// A call was refused: a lock another process or thread holds, a full pool, or a pending fork.
    Refused,
    /// The store retired under the turn.
    Retired,
}

/// What a turn did.
#[derive(Debug)]
pub(crate) struct TurnReport {
    /// The tasks it finished (or found another process had done).
    pub done: TaskSet,
    /// Why it stopped early, if it did: the rest is still due, a second from now.
    pub stopped: Option<Stop>,
    /// Rows it removed.
    pub removed: u64,
    /// It found work to do (expired rows, or a cache over its limit), whether or not it got to it:
    /// with `stopped` and `removed`, what tells a healthy turn from a starved one (decision 16).
    pub work_seen: bool,
    /// The first failure, if a task failed; that task isn't in `done`, and the tasks after it ran.
    pub error: Option<Error>,
}

impl TurnReport {
    pub(crate) const fn cut_short(&self) -> bool {
        self.stopped.is_some()
    }
}

/// How one task went.
enum Step {
    Done,
    Cut(Stop),
    Failed(Error),
}

/// One chunk of expiry or eviction.
enum Chunk {
    /// Another process holds the claim: the work is its.
    Elsewhere,
    Expired(usize),
    Evicted(usize),
    /// Nothing to evict: within the limit, the policy is `'none'`, or `cull_limit` is 0.
    Within,
}

/// What the first read of a turn finds.
struct Tidy {
    claim: Option<f64>,
    expired: bool,
    /// Over the limit, under a policy and `cull_limit` that let maintenance evict.
    to_evict: bool,
}

impl Store {
    /// Runs `tasks` on this store as one maintenance turn at wall time `now` (seconds), on a store
    /// whose maintenance interval is `interval`. None means no turn: the store has retired, a fork is
    /// pending, or a fork poisoned it (D-020); the caller skips this store until its next tick.
    pub(crate) fn maintenance_turn(&self, tasks: TaskSet, now: f64, interval: Duration) -> Option<TurnReport> {
        self.maintenance_turn_within(tasks, now, interval, TURN_BUDGET)
    }

    fn maintenance_turn_within(
        &self,
        tasks: TaskSet,
        now: f64,
        interval: Duration,
        budget: Duration,
    ) -> Option<TurnReport> {
        // A poisoned store can never be written, so a turn would be refused every second for as long
        // as the process lived.
        if self.is_poisoned() {
            return None;
        }
        let turn = self.begin_background_turn()?;
        let deadline = Instant::now() + budget;
        let interval_ms = u64::try_from(interval.as_millis()).unwrap_or(u64::MAX);
        let cadence_s = |task: Task| Duration::from_millis(task.cadence(interval_ms)).as_secs_f64();
        let mut report = TurnReport { done: TaskSet::EMPTY, stopped: None, removed: 0, work_seen: false, error: None };
        for task in tasks.iter() {
            let step = match task {
                Task::Expire => self.expire_and_cull(&turn, now, cadence_s(task), deadline, &mut report),
                Task::Checkpoint => self.checkpoint_step(&turn),
                Task::ValueDirs => self.value_dirs_step(&turn, now, cadence_s(task)),
                Task::TrimPool => Step::Done,
            };
            match step {
                Step::Done => report.done = report.done.with(task),
                Step::Cut(reason) => {
                    report.stopped = Some(reason);
                    break;
                }
                Step::Failed(error) => {
                    report.error.get_or_insert(error);
                }
            }
        }
        Some(report)
    }

    /// Why a turn should stop taking chunks, if it should: the store has retired, a request writer in
    /// this process is waiting for the write lock, or the budget is spent.
    fn turn_should_stop(&self, deadline: Instant) -> Option<Stop> {
        if self.is_retired() {
            Some(Stop::Retired)
        } else if crate::background::worker::stopping() {
            // A fork is stopping the thread: end the turn at once.
            Some(Stop::Refused)
        } else if self.write_waiters() > 0 {
            Some(Stop::Yielded)
        } else if Instant::now() >= deadline {
            Some(Stop::Budget)
        } else {
            None
        }
    }

    fn expire_and_cull(
        &self,
        turn: &BackgroundTurn<'_>,
        now: f64,
        cadence_s: f64,
        deadline: Instant,
        report: &mut TurnReport,
    ) -> Step {
        let tidy = match turn.read(|conn| read_tidy(conn, now)) {
            None => return Step::Cut(Stop::Refused),
            Some(Err(error)) => return Step::Failed(error),
            Some(Ok(tidy)) => tidy,
        };
        let own = self.upkeep().own_claim(Claim::Expire);
        if held_elsewhere(tidy.claim, own, now, cadence_s) || !(tidy.expired || tidy.to_evict) {
            return Step::Done;
        }
        report.work_seen = true;
        let mut claimed = false;
        let mut expiring = tidy.expired;
        loop {
            if let Some(stop) = self.turn_should_stop(deadline) {
                return Step::Cut(stop);
            }
            let chunk = turn.write_files(|writer, conn| {
                if !claimed && !claim(conn, Claim::Expire, now, own, cadence_s)? {
                    return Ok(Changed::unchanged(Chunk::Elsewhere));
                }
                if expiring {
                    let batch = writer.delete_expired(now, CHUNK)?;
                    return Ok(Changed::removing(Chunk::Expired(batch.rows.len()), batch.rows));
                }
                // Read in this transaction, as `cull` does.
                let fill = Fill::read(conn, Duration::ZERO)?;
                if fill.cull_limit <= 0 || !fill.over(conn, Duration::ZERO)? {
                    return Ok(Changed::unchanged(Chunk::Within));
                }
                let batch = writer.delete_oldest(fill.cull_limit.min(CHUNK))?;
                Ok(Changed::removing(Chunk::Evicted(batch.rows.len()), batch.rows))
            });
            let chunk = match chunk {
                None => return Step::Cut(Stop::Refused),
                Some(Err(error)) => return Step::Failed(error),
                Some(Ok(Chunk::Elsewhere)) => return Step::Done,
                Some(Ok(chunk)) => chunk,
            };
            if !claimed {
                claimed = true;
                self.upkeep().set_own_claim(Claim::Expire, now);
            }
            match chunk {
                Chunk::Expired(rows) => {
                    report.removed += rows as u64;
                    if !is_full(rows) {
                        expiring = false;
                        if !tidy.to_evict {
                            return Step::Done;
                        }
                    }
                }
                Chunk::Evicted(rows) => {
                    report.removed += rows as u64;
                    if rows == 0 {
                        return Step::Done;
                    }
                }
                Chunk::Within | Chunk::Elsewhere => return Step::Done,
            }
        }
    }

    fn checkpoint_step(&self, turn: &BackgroundTurn<'_>) -> Step {
        use crate::wal::Outcome;
        match self.background_checkpoint(turn) {
            Some(Outcome::Failed(error)) => Step::Failed(error),
            Some(Outcome::ForkPending) => Step::Cut(Stop::Refused),
            // Busy backs off on its own (`record_outcome`); the next tick tries again.
            None | Some(Outcome::Done | Outcome::Busy | Outcome::NoSlot) => Step::Done,
        }
    }

    fn value_dirs_step(&self, turn: &BackgroundTurn<'_>, now: f64, cadence_s: f64) -> Step {
        if self.is_retired() {
            return Step::Cut(Stop::Retired);
        }
        let own = self.upkeep().own_claim(Claim::Dirs);
        // A read first, so the hourly claim write is skipped when another process has the sweep.
        match turn.read(|conn| read_claim(conn, Claim::Dirs)) {
            None => return Step::Cut(Stop::Refused),
            Some(Err(error)) => return Step::Failed(error),
            Some(Ok(last)) if held_elsewhere(last, own, now, cadence_s) => return Step::Done,
            Some(Ok(_)) => {}
        }
        match turn.write_files(|_, conn| claim(conn, Claim::Dirs, now, own, cadence_s).map(Changed::unchanged)) {
            None => return Step::Cut(Stop::Refused),
            Some(Err(error)) => return Step::Failed(error),
            Some(Ok(false)) => return Step::Done,
            Some(Ok(true)) => self.upkeep().set_own_claim(Claim::Dirs, now),
        }
        let deadline = Instant::now() + spill::SWEEP_BUDGET;
        let start = spill::sweep_start();
        // On the maintenance thread, marked native, this runs the walk directly (decision 2).
        gate::run_blocking(&|| spill::sweep(self.directory(), deadline, spill::SWEEP_MIN_AGE, start));
        Step::Done
    }
}

/// Whether a chunk came back full, so there may be more.
fn is_full(rows: usize) -> bool {
    i64::try_from(rows).unwrap_or(i64::MAX) >= CHUNK
}

/// The turn's first read: the expiry claim, whether any row has expired (the delete's own predicate,
/// so the probe never reports work the delete won't do), and whether there's anything to evict.
fn read_tidy(conn: &Connection, now: f64) -> Result<Tidy> {
    let (claim, expired) = conn
        .prepare_cached(
            "SELECT (SELECT value FROM settings WHERE name = ?1),
                    EXISTS (SELECT 1 FROM cache WHERE expire_time IS NOT NULL AND expire_time < ?2)",
        )
        .and_then(|mut stmt| {
            stmt.query_row((Claim::Expire.row(), now), |r| Ok((as_time(r.get_ref(0)?), r.get::<_, bool>(1)?)))
        })
        .map_err(|e| Error::from_sqlite(e, Duration::ZERO))?;
    let fill = Fill::read(conn, Duration::ZERO)?;
    let to_evict = fill.cull_limit > 0 && fill.over(conn, Duration::ZERO)?;
    Ok(Tidy { claim, expired, to_evict })
}

/// A claim row's time, if it holds one.
fn read_claim(conn: &Connection, claim: Claim) -> Result<Option<f64>> {
    conn.prepare_cached("SELECT value FROM settings WHERE name = ?1")
        .and_then(|mut stmt| {
            let mut rows = stmt.query([claim.row()])?;
            rows.next()?.map(|row| row.get_ref(0).map(as_time)).transpose().map(Option::flatten)
        })
        .map_err(|e| Error::from_sqlite(e, Duration::ZERO))
}

/// A time stored as a number; anything else (a row another program wrote) reads as none.
fn as_time(value: ValueRef<'_>) -> Option<f64> {
    match value {
        ValueRef::Real(at) => Some(at),
        #[expect(clippy::cast_precision_loss, reason = "a wall time in seconds fits an f64 exactly")]
        ValueRef::Integer(at) => Some(at as f64),
        _ => None,
    }
}

/// Inside a write transaction: takes `claim` for this process at `now`, unless another process holds
/// it. Returns whether this process has it now.
fn claim(conn: &Connection, claim: Claim, now: f64, own: Option<f64>, cadence_s: f64) -> Result<bool> {
    if held_elsewhere(read_claim(conn, claim)?, own, now, cadence_s) {
        return Ok(false);
    }
    conn.prepare_cached(
        "INSERT INTO settings (name, value) VALUES (?1, ?2) ON CONFLICT (name) DO UPDATE SET value = excluded.value",
    )
    .and_then(|mut stmt| stmt.execute((claim.row(), now)))
    .map_err(|e| Error::from_sqlite(e, Duration::ZERO))?;
    Ok(true)
}

/// Whether another process did this work recently enough that this one skips it: the row holds a time
/// that isn't this process's own, less than [`FRESH_SHARE`] of the cadence ago, and not more than
/// [`FUTURE_SLACK_S`] in the future.
fn held_elsewhere(last: Option<f64>, own: Option<f64>, now: f64, cadence_s: f64) -> bool {
    let Some(last) = last else { return false };
    if own.is_some_and(|own| own.to_bits() == last.to_bits()) {
        return false;
    }
    let age = now - last;
    age > -FUTURE_SLACK_S && age < cadence_s * FRESH_SHARE
}

#[cfg(test)]
mod tests;
