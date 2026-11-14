//! When each store's maintenance turn comes, and what it does (plan 28, decisions 3-7).
//!
//! Pure: every function takes the time as milliseconds on a monotonic clock and changes only the
//! [`Schedule`] it's given, so the rules can be tested without a thread, a database or a real clock.
//!
//! - Each store has a grid of **regular ticks**, one interval apart. A store's interval is the
//!   shortest any handle on it asked for; a handle that asked for 0 doesn't count, and with no other
//!   request maintenance is off. Intervals are capped at [`MAX_INTERVAL_MS`].
//! - A new store's first tick is at least one interval after it opens, so opening a cache never leads
//!   straight into maintenance, and a short script never runs any. It's placed in the middle of the
//!   widest gap between the other stores' coming regular ticks, so the stores in a process take turns
//!   across the interval: four caches opened together on 60 s tick at 60, 75, 90 and 105 s.
//! - Placement happens once. Ticks never move later: nothing can keep pushing a turn back, and a
//!   store that retires and is revived keeps its place. A handle asking for a shorter interval keeps
//!   the store's phase and may bring its next tick earlier.
//! - The thread asks [`Schedule::begin`] whether a turn is due; if it returns tasks, the turn runs
//!   them in [`Task::ALL`]'s order and reports back with [`Schedule::finish`]. A wake that runs no
//!   turn for a due store (it's retired, or the gate refused) calls [`Schedule::skip`]. Either way
//!   the next turn is in the future, so a thread can't spin on a store.
//! - A task is due on a tick when it has never run or its cadence has passed since the tick it last
//!   ran on. Measured in ticks, not in when a turn happened to start, so a wake a few milliseconds
//!   late never makes the next tick look early. The cadence is the interval for expiry and the WAL,
//!   longer for the rest (see [`Task::cadence`]).
//! - A turn cut short (its budget ran out, or a writer was waiting) runs again [`CONTINUE_AFTER_MS`]
//!   later, for the tasks it didn't finish, never past the next regular tick.

/// The default interval (decision 4): the review's numbers put a tidy turn at about 10-20 µs per
/// store, so a shorter one would cost little, but it would buy nothing either. Dead rows, an
/// over-limit cache and a large WAL can wait minutes.
pub const DEFAULT_INTERVAL_MS: u64 = 300_000;

/// The shortest interval a handle may ask for through the public API (tests use a hidden hook).
pub const MIN_INTERVAL_MS: u64 = 1_000;

/// The longest interval: a day. Longer requests are capped, so a large number meant as "never" can't
/// overflow the arithmetic; `0` is the way to turn maintenance off.
pub const MAX_INTERVAL_MS: u64 = 86_400_000;

/// How soon a turn cut short runs again.
pub const CONTINUE_AFTER_MS: u64 = 1_000;

/// Placement ignores another store that ticks more than this many times in the window: dodging it
/// gains at most half its interval, under 2% of the window, and the work stays bounded.
const MAX_TICKS_PER_STORE: u64 = 64;

/// One kind of housekeeping, in the order a turn runs them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Task {
    /// Remove expired rows, then evict down to `size_limit` if the cache is over it. Shared across
    /// processes by a claim row.
    Expire,
    /// Checkpoint the WAL if it's past its trim size. After expiry, whose deletes grow it.
    Checkpoint,
    /// Remove dead temporary files and empty value folders. Shared across processes by its own row.
    ValueDirs,
    /// Close connections idle for longer than the cadence, keeping one. In this process only.
    TrimPool,
}

impl Task {
    /// Every task, in turn order.
    pub const ALL: [Self; 4] = [Self::Expire, Self::Checkpoint, Self::ValueDirs, Self::TrimPool];

    /// The shortest time between two runs of this task, whatever the interval. The value-folder sweep
    /// matches the hour a temporary file must sit untouched before it's removed.
    const fn min_cadence_ms(self) -> u64 {
        match self {
            Self::Expire | Self::Checkpoint => 0,
            Self::ValueDirs => 3_600_000,
            Self::TrimPool => 300_000,
        }
    }

    /// How often this task runs on a store whose interval is `interval_ms`.
    #[must_use]
    pub const fn cadence(self, interval_ms: u64) -> u64 {
        let min = self.min_cadence_ms();
        if interval_ms > min { interval_ms } else { min }
    }

    const fn bit(self) -> u8 {
        1 << self as u8
    }
}

/// A set of [`Task`]s, iterated in turn order.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct TaskSet(u8);

impl TaskSet {
    pub const EMPTY: Self = Self(0);

    #[must_use]
    pub const fn with(self, task: Task) -> Self {
        Self(self.0 | task.bit())
    }

    #[must_use]
    pub const fn contains(self, task: Task) -> bool {
        self.0 & task.bit() != 0
    }

    #[must_use]
    pub const fn is_empty(self) -> bool {
        self.0 == 0
    }

    pub fn iter(self) -> impl Iterator<Item = Task> {
        Task::ALL.into_iter().filter(move |task| self.contains(*task))
    }
}

impl FromIterator<Task> for TaskSet {
    fn from_iter<I: IntoIterator<Item = Task>>(tasks: I) -> Self {
        tasks.into_iter().fold(Self::EMPTY, Self::with)
    }
}

/// A store's place in the spread: its next regular tick and its interval. Only [`Schedule::spread`]
/// makes one, so a continuation time or a store with maintenance off can't be passed by mistake.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Tick {
    at_ms: u64,
    every_ms: u64,
}

/// One store's maintenance timing in this process.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Schedule {
    /// The shortest nonzero interval any handle asked for (capped), or 0 if every handle asked for
    /// none. Never lengthens while the store lives, as `max_connections` never shrinks.
    interval_ms: u64,
    /// A handle with its own clock is open (decision 15): the thread can only use wall time, and would
    /// remove entries that clock still calls live. Lasts until the store retires and is revived, when
    /// no such handle can still be open.
    vetoed: bool,
    /// The next regular tick, on the store's grid. Meaningless while `interval_ms` is 0.
    regular_ms: u64,
    /// The next turn: the regular tick, or sooner to continue a turn cut short.
    next_ms: u64,
    /// The grid tick of the turn [`Schedule::begin`] started, for [`Schedule::finish`] to stamp.
    turn_tick_ms: Option<u64>,
    /// The tick each task last finished on, by [`Task`] index.
    last_tick_ms: [Option<u64>; Task::ALL.len()],
}

impl Schedule {
    /// A new store's schedule, asked for by its first handle at `now_ms`. `others` are the other
    /// stores' [`Schedule::spread`], for placement.
    #[must_use]
    pub fn new(now_ms: u64, interval_ms: u64, others: impl IntoIterator<Item = Tick>) -> Self {
        let interval_ms = interval_ms.min(MAX_INTERVAL_MS);
        let first = if interval_ms == 0 { 0 } else { place(now_ms, interval_ms, others) };
        Self {
            interval_ms,
            vetoed: false,
            regular_ms: first,
            next_ms: first,
            turn_tick_ms: None,
            last_tick_ms: [None; Task::ALL.len()],
        }
    }

    /// Whether this store gets maintenance at all in this process.
    #[must_use]
    pub const fn is_on(&self) -> bool {
        self.interval_ms != 0 && !self.vetoed
    }

    #[must_use]
    pub const fn interval_ms(&self) -> u64 {
        self.interval_ms
    }

    /// When the next turn is due, or None if maintenance is off.
    #[must_use]
    pub const fn next_ms(&self) -> Option<u64> {
        if self.is_on() { Some(self.next_ms) } else { None }
    }

    /// This store's place in the spread, for placing another store, or None if maintenance is off.
    #[must_use]
    pub const fn spread(&self) -> Option<Tick> {
        if self.is_on() { Some(Tick { at_ms: self.regular_ms, every_ms: self.interval_ms }) } else { None }
    }

    /// Another handle joined at `now_ms`, asking for `interval_ms` (0: none). `others` are the other
    /// stores' spread, used only if this store had no interval yet: it's then placed as a new store
    /// is. A shorter interval than the store's wins and keeps the store's phase: its next regular
    /// tick steps back by whole new intervals to within one new interval of now, never later than it
    /// was and never sooner than one new interval from now.
    pub fn join(&mut self, now_ms: u64, interval_ms: u64, others: impl IntoIterator<Item = Tick>) {
        let interval_ms = interval_ms.min(MAX_INTERVAL_MS);
        if interval_ms == 0 || (self.interval_ms != 0 && interval_ms >= self.interval_ms) {
            return;
        }
        if self.interval_ms == 0 {
            let first = place(now_ms, interval_ms, others);
            self.interval_ms = interval_ms;
            self.regular_ms = first;
            self.next_ms = first;
            return;
        }
        let soonest = now_ms.saturating_add(interval_ms);
        self.interval_ms = interval_ms;
        if self.regular_ms > soonest {
            self.regular_ms = soonest + (self.regular_ms - soonest) % interval_ms;
        }
        self.next_ms = self.next_ms.min(self.regular_ms);
    }

    /// A handle with its own clock joined: no background maintenance for this store in this process
    /// while it's live.
    pub fn veto(&mut self) {
        self.vetoed = true;
    }

    /// The store was revived after retiring, so no handle from before is open: the veto ends. The
    /// store keeps its place; if its ticks went by while it was retired, its next turn is the next
    /// regular tick.
    pub fn revive(&mut self, now_ms: u64) {
        self.vetoed = false;
        if self.next_ms <= now_ms {
            self.skip(now_ms);
        }
    }

    /// Starts a turn at `now_ms`, if one is due: returns the tasks to run, after which the caller must
    /// call [`Schedule::finish`]. None means no turn now; the schedule has already moved on if one was
    /// due but had nothing to run.
    pub fn begin(&mut self, now_ms: u64) -> Option<TaskSet> {
        if !self.is_on() || now_ms < self.next_ms {
            return None;
        }
        self.advance_past(now_ms);
        // The grid tick this turn belongs to: the last one at or before now.
        let tick = self.regular_ms - self.interval_ms;
        let due: TaskSet = Task::ALL
            .into_iter()
            .filter(|task| {
                self.last_tick_ms[*task as usize]
                    .is_none_or(|last| tick.saturating_sub(last) >= task.cadence(self.interval_ms))
            })
            .collect();
        // Until `finish`, the next turn is the next regular tick, so a caller that never finishes
        // can't spin.
        self.next_ms = self.regular_ms;
        if due.is_empty() {
            return None;
        }
        self.turn_tick_ms = Some(tick);
        Some(due)
    }

    /// The turn [`Schedule::begin`] started finished the tasks in `done`, at `now_ms`. With
    /// `cut_short`, it stopped before finishing the rest of what was due: they run again soon.
    pub fn finish(&mut self, now_ms: u64, done: TaskSet, cut_short: bool) {
        let Some(tick) = self.turn_tick_ms.take() else { return };
        for task in done.iter() {
            self.last_tick_ms[task as usize] = Some(tick);
        }
        self.advance_past(now_ms);
        self.next_ms =
            if cut_short { now_ms.saturating_add(CONTINUE_AFTER_MS).min(self.regular_ms) } else { self.regular_ms };
    }

    /// No turn for this store at `now_ms` (it's retired, or the gate refused): the next turn is the
    /// next regular tick.
    pub fn skip(&mut self, now_ms: u64) {
        self.turn_tick_ms = None;
        self.advance_past(now_ms);
        self.next_ms = self.regular_ms;
    }

    /// Moves the regular tick to the first one after `now_ms`, skipping any a suspended process (or a
    /// retired store) slept through.
    fn advance_past(&mut self, now_ms: u64) {
        if self.interval_ms != 0 && self.regular_ms <= now_ms {
            let missed = (now_ms - self.regular_ms) / self.interval_ms + 1;
            self.regular_ms = self.regular_ms.saturating_add(missed.saturating_mul(self.interval_ms));
        }
    }
}

/// The first tick for a new store at `now_ms` on `interval_ms` (nonzero, capped): the middle of the
/// widest gap between the other stores' regular ticks in the window `[now + interval, now + 2 ×
/// interval)`, or the window's start when there are none. Each store's ticks are projected forward by
/// its own interval, so a store on a shorter interval may tick more than once in the window (up to
/// [`MAX_TICKS_PER_STORE`]; one ticking more often is ignored). The window wraps around: a gap can run
/// from its last tick to its first plus one window.
fn place(now_ms: u64, interval_ms: u64, others: impl IntoIterator<Item = Tick>) -> u64 {
    debug_assert!(interval_ms != 0 && interval_ms <= MAX_INTERVAL_MS);
    let start = now_ms.saturating_add(interval_ms);
    let end = start.saturating_add(interval_ms);
    // Offsets into the window of every tick that falls in it.
    let mut ticks: Vec<u64> = Vec::new();
    for Tick { at_ms, every_ms } in others {
        if every_ms == 0 || interval_ms / every_ms > MAX_TICKS_PER_STORE {
            continue;
        }
        // The first tick at or after `start`.
        let first = if at_ms >= start {
            Some(at_ms)
        } else {
            (start - at_ms).div_ceil(every_ms).checked_mul(every_ms).and_then(|step| at_ms.checked_add(step))
        };
        let Some(mut tick) = first else { continue };
        while tick < end {
            ticks.push(tick - start);
            let Some(next) = tick.checked_add(every_ms) else { break };
            tick = next;
        }
    }
    if ticks.is_empty() {
        return start;
    }
    ticks.sort_unstable();
    ticks.dedup();
    // The widest gap, wrapping from the last tick back to the first. Ties go to the earliest.
    let mut best = (0, 0);
    for (i, &tick) in ticks.iter().enumerate() {
        let next = ticks.get(i + 1).copied().unwrap_or(ticks[0] + interval_ms);
        let gap = next - tick;
        if gap > best.0 {
            best = (gap, tick);
        }
    }
    let (gap, from) = best;
    start + (from + gap / 2) % interval_ms
}

#[cfg(test)]
mod tests {
    use super::*;

    const S: u64 = 1_000;

    /// xorshift64*: deterministic, so a failure reproduces (as in `key.rs`).
    struct Rng(u64);
    impl Rng {
        fn next(&mut self) -> u64 {
            self.0 ^= self.0 >> 12;
            self.0 ^= self.0 << 25;
            self.0 ^= self.0 >> 27;
            self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
        }
        fn below(&mut self, n: u64) -> u64 {
            self.next() % n
        }
    }

    fn spread_of(stores: &[Schedule]) -> Vec<Tick> {
        stores.iter().filter_map(Schedule::spread).collect()
    }

    /// Opens `n` stores at `now`, one after another, as a process opening its caches at startup does.
    fn open_together(now: u64, interval: u64, n: usize) -> Vec<Schedule> {
        let mut stores: Vec<Schedule> = Vec::new();
        for _ in 0..n {
            let others = spread_of(&stores);
            stores.push(Schedule::new(now, interval, others));
        }
        stores
    }

    fn ticks(stores: &[Schedule]) -> Vec<u64> {
        let mut ticks: Vec<u64> = stores.iter().filter_map(Schedule::next_ms).collect();
        ticks.sort_unstable();
        ticks
    }

    /// A whole turn that finishes everything it was given.
    fn turn(schedule: &mut Schedule, now: u64) -> Option<TaskSet> {
        let due = schedule.begin(now)?;
        schedule.finish(now, due, false);
        Some(due)
    }

    #[test]
    fn four_caches_on_a_minute_take_turns_a_quarter_apart() {
        // Michael's example: 60 s and four caches sweep at 0:00, 0:15, 0:30 and 0:45 of the minute.
        let stores = open_together(0, 60 * S, 4);
        assert_eq!(ticks(&stores), [60 * S, 75 * S, 90 * S, 105 * S]);
    }

    #[test]
    fn eight_stores_on_the_default_are_spread_evenly() {
        let stores = open_together(5 * S, DEFAULT_INTERVAL_MS, 8);
        let ticks = ticks(&stores);
        let gaps: Vec<u64> = ticks.windows(2).map(|w| w[1] - w[0]).collect();
        assert!(gaps.iter().all(|&g| g == DEFAULT_INTERVAL_MS / 8), "{ticks:?}");
        assert_eq!(ticks[0], 5 * S + DEFAULT_INTERVAL_MS, "the first turn is one interval after opening");
    }

    #[test]
    fn a_store_opened_later_takes_the_widest_gap() {
        let mut stores = open_together(0, 60 * S, 2); // 60 and 90
        // Opened at 100: its window is [160, 220). The others tick at 180 and 210 there, so the gaps
        // are 180 -> 210 and 210 -> 240 (wrapping to 180 + 60), both 30 s. The tie goes to the earlier
        // one: its middle, 195.
        stores.push(Schedule::new(100 * S, 60 * S, spread_of(&stores)));
        assert_eq!(stores[2].next_ms(), Some(195 * S));
        // A fourth, at 100 too: ticks at 180, 195, 210, so the widest gap wraps, 210 -> 240, and its
        // middle, 225, is past the window's end: it wraps to 165.
        stores.push(Schedule::new(100 * S, 60 * S, spread_of(&stores)));
        assert_eq!(stores[3].next_ms(), Some(165 * S));
    }

    #[test]
    fn stores_on_other_intervals_are_projected_into_the_window() {
        // A store on 10 s ticks six times in a 60 s window; the new one lands between two of them.
        let fast = Schedule::new(0, 10 * S, []);
        let slow = Schedule::new(0, 60 * S, fast.spread());
        let next = slow.next_ms().unwrap();
        assert!((60 * S..120 * S).contains(&next));
        assert_eq!(next % (10 * S), 5 * S, "halfway between the fast store's ticks: {next}");
    }

    #[test]
    fn placement_uses_the_regular_tick_not_a_continuation() {
        let mut first = Schedule::new(0, 60 * S, []);
        let due = first.begin(60 * S).unwrap();
        first.finish(60 * S, due, true); // continues at 61 s, regular ticks stay at 120, 180, ...
        assert_eq!(first.next_ms(), Some(61 * S));
        let second = Schedule::new(60 * S, 60 * S, first.spread());
        // Its window is [120, 180): the first store ticks at 120, so the middle of the gap is 150.
        assert_eq!(second.next_ms(), Some(150 * S));
    }

    #[test]
    fn no_turn_before_the_first_tick_and_every_task_on_it() {
        let mut schedule = Schedule::new(0, 60 * S, []);
        assert_eq!(schedule.begin(59 * S), None);
        assert_eq!(schedule.begin(60 * S), Some(Task::ALL.into_iter().collect()));
    }

    #[test]
    fn each_task_runs_on_its_own_cadence() {
        let mut schedule = Schedule::new(0, 60 * S, []);
        turn(&mut schedule, 60 * S).unwrap();
        assert_eq!(schedule.next_ms(), Some(120 * S));
        // A minute later only expiry and the checkpoint are due; the pool trim needs five minutes and
        // the folder sweep an hour.
        assert_eq!(turn(&mut schedule, 120 * S), Some([Task::Expire, Task::Checkpoint].into_iter().collect()));
        for minute in 3..=6 {
            let due = turn(&mut schedule, minute * 60 * S).unwrap();
            assert_eq!(due.contains(Task::TrimPool), minute == 6, "minute {minute}");
            assert!(!due.contains(Task::ValueDirs));
        }
        for minute in 7..=60 {
            turn(&mut schedule, minute * 60 * S).unwrap();
        }
        assert!(turn(&mut schedule, 61 * 60 * S).unwrap().contains(Task::ValueDirs), "an hour after its first run");
    }

    #[test]
    fn a_late_wake_never_skips_the_next_tick() {
        // The review's case: 40 ms late, then 5 ms late. Measured from the start times, 59.965 s would
        // look short of the 60 s cadence and expiry would run every other interval.
        let mut schedule = Schedule::new(0, 60 * S, []);
        turn(&mut schedule, 60_040).unwrap();
        let due = turn(&mut schedule, 120_005).unwrap();
        assert!(due.contains(Task::Expire) && due.contains(Task::Checkpoint));
    }

    #[test]
    fn a_turn_cut_short_continues_soon_and_the_next_tick_still_runs() {
        let mut schedule = Schedule::new(0, 60 * S, []);
        // Expiry ran out of budget: nothing finished.
        let due = schedule.begin(60 * S).unwrap();
        schedule.finish(60 * S, TaskSet::EMPTY, true);
        assert_eq!(schedule.next_ms(), Some(61 * S));
        assert_eq!(turn(&mut schedule, 61 * S), Some(due), "everything is still due");
        assert_eq!(schedule.next_ms(), Some(120 * S), "back on the regular tick");
        // The continuation's work counts for the 60 s tick, so the 120 s tick runs expiry again.
        assert!(turn(&mut schedule, 120 * S).unwrap().contains(Task::Expire));
    }

    #[test]
    fn a_continuation_never_runs_past_the_regular_tick() {
        let mut schedule = Schedule::new(0, 60 * S, []);
        let _ = schedule.begin(119_500).unwrap();
        schedule.finish(119_500, TaskSet::EMPTY, true);
        assert_eq!(schedule.next_ms(), Some(120 * S));
    }

    #[test]
    fn ticks_a_suspended_process_slept_through_are_skipped() {
        let mut schedule = Schedule::new(0, 60 * S, []);
        turn(&mut schedule, 1_000 * S).unwrap();
        assert_eq!(schedule.next_ms(), Some(1_020 * S), "the next tick in the original phase");
    }

    #[test]
    fn a_due_store_with_nothing_to_run_moves_on() {
        // Every task ran on this tick; a begin before the next tick (as for a pending continuation)
        // finds nothing, and the schedule must still move on rather than stay due.
        let mut schedule = Schedule::new(0, 60 * S, []);
        turn(&mut schedule, 60 * S).unwrap();
        schedule.next_ms = 70 * S;
        assert_eq!(schedule.begin(70 * S), None);
        assert_eq!(schedule.next_ms(), Some(120 * S));
    }

    #[test]
    fn skip_moves_a_due_store_to_its_next_tick() {
        let mut schedule = Schedule::new(0, 60 * S, []);
        schedule.skip(200 * S);
        assert_eq!(schedule.next_ms(), Some(240 * S));
        assert_eq!(schedule.begin(200 * S), None);
    }

    #[test]
    fn the_shortest_interval_any_handle_asked_for_wins_and_keeps_the_phase() {
        let mut schedule = Schedule::new(0, DEFAULT_INTERVAL_MS, []); // ticks at 300, 600, ...
        schedule.join(10 * S, 0, []);
        schedule.join(10 * S, 600 * S, []);
        assert_eq!(schedule.interval_ms(), DEFAULT_INTERVAL_MS, "0 and a longer interval change nothing");
        schedule.join(10 * S, 60 * S, []);
        assert_eq!(schedule.interval_ms(), 60 * S);
        // 300 stepped back by whole minutes to within a minute of 70: 120 keeps the phase.
        assert_eq!(schedule.next_ms(), Some(120 * S));
    }

    #[test]
    fn a_shorter_interval_never_moves_the_tick_later() {
        let mut schedule = Schedule::new(0, 60 * S, []);
        schedule.join(55 * S, 30 * S, []);
        assert_eq!(schedule.next_ms(), Some(60 * S), "85 s would be later than the tick it had");
    }

    #[test]
    fn a_shorter_interval_keeps_a_spread_apart() {
        // Eight stores spread on 300 s, then each joined with 60 s (a second FanoutCache handle):
        // they stay apart instead of all landing on 70 s.
        let mut stores = open_together(0, DEFAULT_INTERVAL_MS, 8);
        for store in &mut stores {
            store.join(10 * S, 60 * S, []);
        }
        let ticks = ticks(&stores);
        let mut distinct = ticks.clone();
        distinct.dedup();
        assert_eq!(distinct.len(), 8, "{ticks:?}");
    }

    #[test]
    fn off_until_a_handle_asks_then_placed_like_a_new_store() {
        let mut stores: Vec<Schedule> = (0..4).map(|_| Schedule::new(0, 0, [])).collect();
        assert!(stores.iter().all(|s| !s.is_on() && s.spread().is_none()));
        let mut off = Schedule::new(0, 0, []);
        assert_eq!(off.begin(u64::MAX), None);
        for i in 0..stores.len() {
            let others = spread_of(&stores);
            stores[i].join(10 * S, 60 * S, others);
        }
        assert_eq!(ticks(&stores), [70 * S, 85 * S, 100 * S, 115 * S]);
    }

    #[test]
    fn a_handle_with_its_own_clock_vetoes_maintenance_until_a_revive() {
        let mut schedule = Schedule::new(0, 60 * S, []);
        schedule.veto();
        schedule.join(1, 10 * S, []);
        assert!(!schedule.is_on());
        assert_eq!(schedule.begin(1_000 * S), None);
        assert_eq!(schedule.next_ms(), None);
        assert_eq!(schedule.spread(), None, "a vetoed store takes no part in the spread");
        schedule.revive(1_000 * S);
        assert!(schedule.is_on());
        assert!(schedule.next_ms().unwrap() > 1_000 * S, "no turn straight away");
    }

    #[test]
    fn huge_and_tiny_intervals_never_overflow_or_stall() {
        // A huge interval meant as "never" is capped at a day.
        let mut huge = Schedule::new(1, u64::MAX, [Tick { at_ms: 60 * S, every_ms: 60 * S }]);
        assert_eq!(huge.interval_ms(), MAX_INTERVAL_MS);
        huge.join(1, u64::MAX - 1, []);
        // A day-long store beside 30 stores on 1 ms: the dense ones are ignored, so this is instant.
        let dense: Vec<Tick> = (0..30).map(|i| Tick { at_ms: i, every_ms: 1 }).collect();
        let started = std::time::Instant::now();
        let placed = Schedule::new(0, MAX_INTERVAL_MS, dense);
        assert!(started.elapsed() < std::time::Duration::from_millis(50));
        assert_eq!(placed.next_ms(), Some(MAX_INTERVAL_MS));
        // Near the end of the clock.
        let mut late = Schedule::new(u64::MAX - 10, 60 * S, [Tick { at_ms: u64::MAX - 5, every_ms: 60 * S }]);
        let _ = late.begin(u64::MAX);
        late.skip(u64::MAX);
        // A 1 ms interval: the window is one slot.
        let one = Schedule::new(0, 1, [Tick { at_ms: 1, every_ms: 1 }]);
        assert_eq!(one.next_ms(), Some(1));
    }

    #[test]
    fn cadences_follow_the_interval_above_their_minimum() {
        assert_eq!(Task::Expire.cadence(DEFAULT_INTERVAL_MS), DEFAULT_INTERVAL_MS);
        assert_eq!(Task::TrimPool.cadence(60 * S), 300 * S);
        assert_eq!(Task::TrimPool.cadence(600 * S), 600 * S);
        assert_eq!(Task::ValueDirs.cadence(DEFAULT_INTERVAL_MS), 3_600 * S);
        assert_eq!(Task::ValueDirs.cadence(7_200 * S), 7_200 * S);
    }

    /// N stores opened at the same moment on one interval: every first tick is in the window, all
    /// distinct, and no two closer than `interval / (2N)` (midpoints fill in powers of two, so
    /// `interval / N` is too strict).
    #[test]
    fn stores_opened_together_are_spread_within_bounds() {
        let mut r = Rng(0x9e37_79b9_7f4a_7c15);
        for _ in 0..300 {
            let interval = 1_000 + r.below(MAX_INTERVAL_MS);
            let now = r.below(1 << 40);
            let n = 1 + r.below(40);
            let stores = open_together(now, interval.min(MAX_INTERVAL_MS), usize::try_from(n).unwrap());
            let interval = interval.min(MAX_INTERVAL_MS);
            let ticks = ticks(&stores);
            assert!(ticks.iter().all(|&t| (now + interval..now + 2 * interval).contains(&t)), "{ticks:?}");
            let min_gap = ticks.windows(2).map(|w| w[1] - w[0]).min().unwrap_or(interval);
            assert!(min_gap + 1 >= interval / (2 * n), "n={n} interval={interval} gaps down to {min_gap}");
        }
    }

    /// `place` with arbitrary others (mixed intervals, past and future ticks): always in the window.
    #[test]
    fn placement_is_always_in_the_window() {
        let mut r = Rng(0x2545_f491_4f6c_dd1d);
        for _ in 0..2_000 {
            let interval = 1 + r.below(MAX_INTERVAL_MS);
            let now = r.below(1 << 50);
            let others: Vec<Tick> = (0..r.below(30))
                .map(|_| Tick { at_ms: r.below(1 << 51), every_ms: r.below(MAX_INTERVAL_MS + 1) })
                .collect();
            let tick = place(now, interval, others);
            assert!((now + interval..now + 2 * interval).contains(&tick));
        }
    }

    /// A simulated thread waking up to 100 ms late, with random cut-short turns: every regular tick
    /// from the first turn to the last runs expiry exactly once (on the tick or its continuation), the
    /// next turn is always in the future, and the grid keeps its phase.
    #[test]
    fn a_simulated_thread_keeps_every_tick() {
        let mut r = Rng(0xd1b5_4a32_d192_ed03);
        for _ in 0..200 {
            let interval = 1_000 * (1 + r.below(600));
            let mut schedule = Schedule::new(0, interval, []);
            let phase = schedule.regular_ms % interval;
            let mut expired_on: Vec<u64> = Vec::new();
            let mut now = 0;
            for _ in 0..400 {
                now = schedule.next_ms().unwrap() + r.below(100);
                let Some(due) = schedule.begin(now) else { continue };
                let tick = schedule.turn_tick_ms.unwrap();
                let cut = r.below(5) == 0;
                // A cut turn finishes nothing; the continuation reruns it.
                let done = if cut { TaskSet::EMPTY } else { due };
                if done.contains(Task::Expire) {
                    expired_on.push(tick);
                }
                now += r.below(60);
                schedule.finish(now, done, cut);
                assert!(schedule.next_ms().unwrap() > now);
                assert_eq!(schedule.regular_ms % interval, phase, "the grid kept its phase");
            }
            let first = expired_on[0];
            let last = *expired_on.last().unwrap();
            let expected: Vec<u64> = (0..=(last - first) / interval).map(|k| first + k * interval).collect();
            assert_eq!(expired_on, expected, "interval {interval}, ended at {now}");
        }
    }
}
