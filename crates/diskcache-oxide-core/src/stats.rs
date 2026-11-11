//! Hit and miss counters for `stats()` (plan 24, release 2, decision 7): per process, in memory,
//! never written to the database, so turning them on never makes a read write.
//!
//! One [`Stats`] per database file per process, kept in a process-wide map by the file's identity,
//! so a cache opened per use (`with Cache(d) as c:`), or a `FanoutCache` whose shards were closed,
//! keeps its counts and its enable flag. The map is consulted only when a store is created, under
//! the fork gate, so a fork never inherits its lock held.
//!
//! A forked child starts at zero (decision 19), lazily: each counter set remembers the fork
//! generation it last counted in, and resets itself the first time it is used in a new one. So the
//! after-fork hook does nothing here.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError};

use crate::poison::FileId;

/// A database's counters in this process, on a cache line of their own: on 3.14t `get` is hot
/// across threads.
#[derive(Debug, Default)]
#[repr(align(128))]
pub struct Stats {
    enabled: AtomicBool,
    hits: AtomicU64,
    misses: AtomicU64,
    /// The fork generation these counts belong to (see the module docs).
    generation: AtomicU64,
}

impl Stats {
    /// A child forked since the last count starts from zero. In a child with several threads, one
    /// thread's first count can be lost to another's reset at that first lookup: harmless.
    fn sync(&self) {
        let now = crate::gate::generation();
        if self.generation.load(Ordering::Relaxed) != now && self.generation.swap(now, Ordering::AcqRel) != now {
            self.hits.store(0, Ordering::Relaxed);
            self.misses.store(0, Ordering::Relaxed);
        }
    }

    /// Counts one lookup, if counting is on: one relaxed load when it isn't.
    #[inline]
    pub fn record(&self, hit: bool) {
        if !self.enabled.load(Ordering::Relaxed) {
            return;
        }
        self.sync();
        let counter = if hit { &self.hits } else { &self.misses };
        counter.fetch_add(1, Ordering::Relaxed);
    }

    /// diskcache's `stats(enable, reset)`: the counts as they were, then reset if asked, then
    /// counting turned on or off.
    pub fn take(&self, enable: bool, reset: bool) -> (u64, u64) {
        self.sync();
        let counts = if reset {
            (self.hits.swap(0, Ordering::Relaxed), self.misses.swap(0, Ordering::Relaxed))
        } else {
            (self.hits.load(Ordering::Relaxed), self.misses.load(Ordering::Relaxed))
        };
        self.enabled.store(enable, Ordering::Relaxed);
        counts
    }
}

/// By the database's identity and its directory's canonical path: an inode can be reused for a new
/// database (Linux reuses them quickly), which must not inherit an old one's counts, so the path must
/// match too, as the store registry requires before it revives a store.
type ByFile = HashMap<(FileId, PathBuf), Arc<Stats>>;
static BY_FILE: Mutex<Option<ByFile>> = Mutex::new(None);

/// The counters of the database `id` in `canonical`, created at zero, off, on first use. Call with
/// the gate held.
pub(crate) fn for_file(id: FileId, canonical: &Path) -> Arc<Stats> {
    let mut map = BY_FILE.lock().unwrap_or_else(PoisonError::into_inner);
    Arc::clone(map.get_or_insert_with(HashMap::new).entry((id, canonical.to_path_buf())).or_default())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_only_while_enabled_and_returns_the_counts_before_a_reset() {
        let stats = Stats::default();
        stats.record(true);
        assert_eq!(stats.take(true, false), (0, 0), "off by default");
        stats.record(true);
        stats.record(true);
        stats.record(false);
        assert_eq!(stats.take(true, true), (2, 1));
        assert_eq!(stats.take(false, false), (0, 0));
        stats.record(true);
        assert_eq!(stats.take(false, false), (0, 0), "off again");
    }

    #[test]
    fn one_file_shares_one_set_of_counters() {
        let id = (u64::MAX, 4242);
        let here = Path::new("/tmp/dco-stats-a");
        let a = for_file(id, here);
        a.take(true, false);
        for_file(id, here).record(true);
        assert_eq!(a.take(true, true), (1, 0));
        assert!(!Arc::ptr_eq(&a, &for_file((u64::MAX, 4243), here)));
        // The same inode reused for a database somewhere else starts afresh.
        assert!(!Arc::ptr_eq(&a, &for_file(id, Path::new("/tmp/dco-stats-b"))));
    }

    #[test]
    fn a_new_fork_generation_starts_from_zero() {
        let stats = Stats::default();
        stats.take(true, false);
        stats.record(true);
        // As if this process had forked since: the counts belong to another generation.
        stats.generation.store(crate::gate::generation().wrapping_add(1), Ordering::Relaxed);
        assert_eq!(stats.take(true, false), (0, 0));
    }
}
