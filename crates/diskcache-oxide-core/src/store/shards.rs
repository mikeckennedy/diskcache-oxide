//! Opening and closing a `FanoutCache`'s shards as one (plan 13, sections 3 and 4).
//!
//! A `FanoutCache` is N ordinary stores. What's specific to it lives here: every shard opens under
//! the caller's one deadline with its share of the connection and size budgets, a shard that fails
//! releases the ones opened before it, and a close releases every shard under one gate level with
//! one checkpoint budget, so neither waits N times the budget behind a stuck fork. The manifest is
//! the caller's: it opens it (`fanout::open_manifest`) before any shard.

use std::borrow::Borrow;
use std::sync::Arc;
use std::time::{Duration, Instant};

use super::{DEFAULT_MAX_CONNECTIONS, OpenOptions, RELEASE_BUDGET, Requested, Role, Store};
use crate::fanout::{Manifest, shard_dirname};
use crate::gate::{GATE, SharedGuard};
use crate::schema::MIN_SIZE_LIMIT;
use crate::{Error, Result, lockwait};

impl Store {
    /// The total `max_connections` a `FanoutCache` with `shards` shards gets when none is asked
    /// for: one per shard, and never fewer than a plain cache's [`DEFAULT_MAX_CONNECTIONS`]. So the
    /// default 8 shards keep D-023's memory bound (about `8 × sqlite_cache_size`) unchanged.
    #[must_use]
    pub fn default_total_connections(shards: u32) -> u64 {
        DEFAULT_MAX_CONNECTIONS.max(u64::from(shards))
    }

    /// Opens every shard of the `FanoutCache` whose root manifest is `manifest`, under `deadline`.
    ///
    /// `base` is the root's settings: `directory` is the root, and `max_connections`,
    /// `requested.size_limit` and `default_size_limit` are totals for the whole cache, split here
    /// (see `split_connections` and `split_size_limit`). Every other setting applies to each
    /// shard as is; `role` is set per shard. If shard k fails to open, shards `0..k` are released
    /// before its error is returned, without waiting (the open has used its budget). A timeout's
    /// number is left for the caller, as for [`Store::open_until`].
    pub fn open_shards(base: &OpenOptions, manifest: Manifest, deadline: Instant) -> Result<Vec<Arc<Self>>> {
        let count = manifest.shards();
        let connections = split_connections(base.max_connections, count)?;
        let size_limit =
            base.requested.size_limit.map(|total| split_size_limit("size_limit", total, count)).transpose()?;
        let default_size_limit = split_size_limit("the default size_limit", base.default_size_limit, count)?;
        let mut opened: Vec<Arc<Self>> = Vec::with_capacity(connections.len());
        for (index, max_connections) in (0..count).zip(connections) {
            let options = OpenOptions {
                directory: base.directory.join(shard_dirname(index as usize)),
                requested: Requested { size_limit, ..base.requested },
                max_connections,
                role: Role::Shard { index, count },
                default_size_limit,
                ..base.clone()
            };
            match Self::open_until(&options, deadline) {
                Ok(store) => opened.push(store),
                Err(error) => {
                    release_all(&opened, GATE.try_enter_shared());
                    return Err(error);
                }
            }
        }
        Ok(opened)
    }

    /// Releases one handle on each of `stores`, as `FanoutCache.close()` does: with
    /// `checkpoint_budget`, the last handle on each store first checkpoints a large WAL (as
    /// [`Store::checkpoint_on_close`]), all of them sharing that one budget; then every release runs
    /// under one gate level, waited for at most `RELEASE_BUDGET` (1 s) in all. Behind a fork that
    /// outlasts it, every release is left for the drain at its end. Never fails.
    ///
    /// A store reached after the checkpoint budget is spent still gets its one try, with no wait.
    pub fn close_all(stores: &[&Arc<Self>], checkpoint_budget: Option<Duration>) {
        close_all_within(stores, checkpoint_budget, RELEASE_BUDGET);
    }
}

/// [`Store::close_all`] with the release budget as a parameter, for tests.
pub(super) fn close_all_within(stores: &[&Arc<Store>], checkpoint_budget: Option<Duration>, release_budget: Duration) {
    if let Some(budget) = checkpoint_budget {
        let deadline = lockwait::deadline_after(Instant::now(), budget);
        for store in stores {
            store.checkpoint_on_close(deadline.saturating_duration_since(Instant::now()));
        }
    }
    let gate = GATE.enter_shared_until(lockwait::deadline_after(Instant::now(), release_budget));
    release_all(stores, gate);
}

/// Releases a handle on each of `stores` under `gate`, or, with none (a fork is pending), defers
/// each release to the drain at the end of that fork.
fn release_all<S: Borrow<Arc<Store>>>(stores: &[S], gate: Option<SharedGuard<'_>>) {
    match gate {
        Some(gate) => stores.iter().for_each(|store| store.borrow().release_under(&gate)),
        None => stores.iter().for_each(|store| store.borrow().defer_release()),
    }
}

/// Splits a `FanoutCache`'s total connection cap over `shards`: `total / shards` each, and the
/// first `total % shards` get one more. Each shard's cap stays local to its own store, which is the
/// point of sharding; a hot shard then has its share, and `max_connections` raises it. Refuses a
/// total below the shard count, which would leave a shard with none.
pub(crate) fn split_connections(total: u64, shards: u32) -> Result<Vec<u64>> {
    let n = u64::from(shards);
    if total < n {
        return Err(Error::InvalidSetting(format!(
            "max_connections must be at least the shard count ({shards}), got {total}: every shard needs a connection"
        )));
    }
    Ok((0..n).map(|i| total / n + u64::from(i < total % n)).collect())
}

/// Splits a total `size_limit` over `shards`, floored: each shard culls on its own, at its share.
/// Each share must still reach [`MIN_SIZE_LIMIT`] (D-005), so the total must be at least `shards`
/// MiB. `what` names the setting in the error.
pub(crate) fn split_size_limit(what: &str, total: i64, shards: u32) -> Result<i64> {
    let share = total / i64::from(shards);
    if share < MIN_SIZE_LIMIT {
        return Err(Error::InvalidSetting(format!(
            "{what} must be at least {} bytes for {shards} shards ({MIN_SIZE_LIMIT} each), got {total}",
            MIN_SIZE_LIMIT * i64::from(shards)
        )));
    }
    Ok(share)
}

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::sync::atomic::Ordering;

    use super::*;
    use crate::fanout;
    use crate::open::Limits;
    use crate::schema::DEFAULT_SIZE_LIMIT;
    use crate::store::{DEFAULT_CACHE_PAGES, DEFAULT_WAL_SIZE_LIMIT};
    use crate::testutil::TempDir;

    const T: Duration = Duration::from_secs(5);

    fn base(root: &Path) -> OpenOptions {
        OpenOptions {
            directory: root.to_path_buf(),
            timeout: T,
            requested: Requested::default(),
            max_connections: DEFAULT_MAX_CONNECTIONS,
            cache_pages: DEFAULT_CACHE_PAGES,
            wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
            auto_checkpoint: false,
            role: Role::Plain,
            default_size_limit: DEFAULT_SIZE_LIMIT,
        }
    }

    fn open(options: &OpenOptions, shards: Option<u32>) -> Result<Vec<Arc<Store>>> {
        let manifest = fanout::open_manifest(&options.directory, shards)?;
        Store::open_shards(options, manifest, lockwait::deadline_after(Instant::now(), T))
    }

    fn close(stores: &[Arc<Store>]) {
        Store::close_all(&stores.iter().collect::<Vec<_>>(), None);
    }

    #[test]
    fn connections_split_evenly_with_the_remainder_first() {
        assert_eq!(split_connections(8, 8).unwrap(), [1; 8]);
        assert_eq!(split_connections(10, 8).unwrap(), [2, 2, 1, 1, 1, 1, 1, 1]);
        assert_eq!(split_connections(16, 8).unwrap(), [2; 8]);
        assert_eq!(split_connections(8, 3).unwrap(), [3, 3, 2]);
        assert_eq!(split_connections(5, 1).unwrap(), [5]);
        let err = split_connections(7, 8).unwrap_err();
        assert!(matches!(&err, Error::InvalidSetting(m) if m.contains("(8)") && m.contains("got 7")), "{err:?}");
    }

    #[test]
    fn the_default_total_is_eight_or_one_per_shard() {
        assert_eq!(Store::default_total_connections(1), 8);
        assert_eq!(Store::default_total_connections(8), 8);
        assert_eq!(Store::default_total_connections(64), 64);
    }

    #[test]
    fn size_limits_split_floored_and_at_least_a_mebibyte_each() {
        assert_eq!(split_size_limit("size_limit", 80 << 20, 8).unwrap(), 10 << 20);
        assert_eq!(split_size_limit("size_limit", (9 << 20) + 7, 8).unwrap(), ((9 << 20) + 7) / 8);
        assert_eq!(split_size_limit("size_limit", 8 << 20, 8).unwrap(), 1 << 20);
        let err = split_size_limit("size_limit", (8 << 20) - 1, 8).unwrap_err();
        assert!(matches!(&err, Error::InvalidSetting(m) if m.contains("8388608")), "{err:?}");
        // The default total divides for every valid count.
        assert_eq!(split_size_limit("d", DEFAULT_SIZE_LIMIT, fanout::MAX_SHARDS).unwrap(), 4 << 20);
    }

    #[test]
    fn a_default_root_gets_eight_shards_one_connection_each_and_a_gibibyte_in_all() {
        let tmp = TempDir::new("shards-default");
        let stores = open(&base(tmp.path()), None).unwrap();
        assert_eq!(stores.len(), 8);
        for (i, store) in stores.iter().enumerate() {
            assert_eq!(store.directory(), tmp.path().join(format!("{i:03}")));
            assert_eq!(store.max_connections.load(Ordering::Acquire), 1);
            assert_eq!(store.role, Role::Shard { index: u32::try_from(i).unwrap(), count: 8 });
        }
        let total: i64 = stores.iter().map(|s| s.cached_limits().size_limit).sum();
        assert_eq!(total, DEFAULT_SIZE_LIMIT, "the default size_limit is divided, not given to every shard");
        close(&stores);
    }

    #[test]
    fn explicit_totals_are_split_and_a_reopen_keeps_them() {
        let tmp = TempDir::new("shards-explicit");
        let options = OpenOptions {
            max_connections: 10,
            requested: Requested { size_limit: Some(80 << 20), cull_limit: Some(3) },
            ..base(tmp.path())
        };
        let stores = open(&options, Some(8)).unwrap();
        let caps: Vec<u64> = stores.iter().map(|s| s.max_connections.load(Ordering::Acquire)).collect();
        assert_eq!(caps, [2, 2, 1, 1, 1, 1, 1, 1]);
        assert!(stores.iter().all(|s| s.cached_limits() == Limits { size_limit: 10 << 20, cull_limit: 3 }));
        close(&stores);
        let reopened = open(&base(tmp.path()), None).unwrap();
        assert!(reopened.iter().all(|s| s.cached_limits() == Limits { size_limit: 10 << 20, cull_limit: 3 }));
        close(&reopened);
    }

    #[test]
    fn budgets_that_leave_a_shard_short_are_refused_before_any_shard_exists() {
        let tmp = TempDir::new("shards-short");
        let too_few = OpenOptions { max_connections: 7, ..base(tmp.path()) };
        assert!(matches!(open(&too_few, Some(8)), Err(Error::InvalidSetting(_))));
        let too_small =
            OpenOptions { requested: Requested { size_limit: Some(7 << 20), cull_limit: None }, ..base(tmp.path()) };
        assert!(matches!(open(&too_small, Some(8)), Err(Error::InvalidSetting(_))));
        assert!(!tmp.path().join("000").exists(), "no shard was created");
    }

    #[test]
    fn a_shard_that_fails_to_open_releases_the_shards_before_it() {
        let tmp = TempDir::new("shards-fail");
        let root = tmp.path();
        let manifest = fanout::open_manifest(root, Some(4)).unwrap();
        // Shards 0 and 1 are already open elsewhere in this process, so the failed open joins them.
        let held: Vec<Arc<Store>> = (0..2)
            .map(|index| {
                let directory = root.join(shard_dirname(index as usize));
                Store::open(&OpenOptions { directory, role: Role::Shard { index, count: 4 }, ..base(root) }).unwrap()
            })
            .collect();
        // Shard 2's directory belongs to a plain cache, which a shard never adopts.
        Store::open(&base(&root.join("002"))).unwrap().release_handle();
        let err = Store::open_shards(&base(root), manifest, lockwait::deadline_after(Instant::now(), T)).unwrap_err();
        assert!(matches!(err, Error::RoleMismatch { .. }), "{err:?}");
        for store in &held {
            assert_eq!(store.handles(), 1, "the failed open released the handle it took on {:?}", store.directory());
        }
        assert!(!root.join("003").exists(), "nothing past the failure was opened");
        close(&held);
    }

    #[test]
    fn close_all_releases_every_shard() {
        let tmp = TempDir::new("shards-close");
        let stores = open(&base(tmp.path()), Some(4)).unwrap();
        Store::close_all(&stores.iter().collect::<Vec<_>>(), Some(Duration::from_millis(100)));
        for store in &stores {
            assert_eq!(store.handles(), 0);
            assert!(store.is_retired());
        }
    }
}
