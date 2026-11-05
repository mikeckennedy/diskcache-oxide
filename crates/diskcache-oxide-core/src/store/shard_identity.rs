//! A shard's stamp is checked on both open paths, and a plain `Cache` and a `FanoutCache` refuse
//! each other's directories (plan 13, step 13b).
//!
//! The slow path checks the stamp in `open::prepare`; the fast path (a handle joining a live store)
//! never runs `prepare`, so `Store::join` checks the role the store recorded. Both must refuse, and
//! a refused open must leave the store's handle count where it was.

use super::*;
use crate::fanout::{self, MANIFEST_FILENAME};
use crate::schema::DB_FILENAME;
use crate::testutil::TempDir;

const T: Duration = Duration::from_secs(5);

fn options(dir: &Path, role: Role) -> OpenOptions {
    OpenOptions {
        directory: dir.to_path_buf(),
        timeout: T,
        requested: Requested::default(),
        max_connections: DEFAULT_MAX_CONNECTIONS,
        cache_pages: DEFAULT_CACHE_PAGES,
        wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
        auto_checkpoint: false,
        role,
        default_size_limit: crate::schema::DEFAULT_SIZE_LIMIT,
    }
}

fn shard(index: u32, count: u32) -> Role {
    Role::Shard { index, count }
}

/// Opens shards `0..count` under `root` the way a `FanoutCache` will: the manifest first.
fn open_root(root: &Path, count: u32) -> Vec<Arc<Store>> {
    fanout::open_manifest(root, Some(count)).unwrap();
    (0..count)
        .map(|i| {
            let dir = root.join(fanout::shard_dirname(i as usize));
            Store::open(&options(&dir, shard(i, count))).unwrap()
        })
        .collect()
}

fn refused(result: Result<Arc<Store>>, says: &str) {
    match result {
        Err(err @ Error::RoleMismatch { .. }) => assert!(err.to_string().contains(says), "{err}"),
        Err(other) => panic!("expected a role mismatch, got {other:?}"),
        Ok(_) => panic!("expected a role mismatch, got a store"),
    }
}

#[test]
fn a_plain_cache_on_a_live_shard_is_refused_through_the_fast_path() {
    let tmp = TempDir::new("shard-fast-plain");
    let shards = open_root(tmp.path(), 2);
    let dir = tmp.path().join("001");
    assert_eq!(shards[1].handles(), 1);
    // The store is live, so this open is `lookup` and `join`, with no `prepare`.
    refused(Store::open(&options(&dir, Role::Plain)), "open the FanoutCache's root directory");
    assert_eq!(shards[1].handles(), 1, "the refused handle was released");
    // Explicit settings take join's writing branch; the check comes before it.
    refused(
        Store::open(&OpenOptions {
            requested: Requested { size_limit: Some(9 << 20), cull_limit: None },
            max_connections: 64,
            ..options(&dir, Role::Plain)
        }),
        "open the FanoutCache's root directory",
    );
    assert_eq!(shards[1].handles(), 1);
    assert_eq!(shards[1].max_connections.load(Ordering::Acquire), DEFAULT_MAX_CONNECTIONS, "nothing was raised");
    assert_ne!(shards[1].cached_limits().size_limit, 9 << 20, "nothing was written");
    // The error names the path this caller typed, even when it spells the directory differently.
    let spelled = tmp.path().join("000").join("..").join("001");
    let err = Store::open(&options(&spelled, Role::Plain)).unwrap_err();
    assert!(err.to_string().contains(&*spelled.to_string_lossy()), "{err}");
    assert_eq!(shards[1].handles(), 1);
    // The right role still joins the same store.
    let again = Store::open(&options(&dir, shard(1, 2))).unwrap();
    assert!(Arc::ptr_eq(&again, &shards[1]));
    assert_eq!(shards[1].handles(), 2);
}

#[test]
fn a_shard_with_another_index_or_count_is_refused_through_the_fast_path() {
    let tmp = TempDir::new("shard-fast-count");
    let shards = open_root(tmp.path(), 2);
    let dir = tmp.path().join("000");
    refused(Store::open(&options(&dir, shard(1, 2))), "opened as shard 1 of 2");
    refused(Store::open(&options(&dir, shard(0, 4))), "opened as shard 0 of 4");
    assert_eq!(shards[0].handles(), 1);
}

#[test]
fn a_plain_cache_on_a_closed_shard_is_refused_through_the_slow_path() {
    let tmp = TempDir::new("shard-slow-plain");
    let shards = open_root(tmp.path(), 2);
    for store in &shards {
        store.release_handle();
    }
    // No live store, so this open runs `prepare`, which reads the stamp.
    refused(Store::open(&options(&tmp.path().join("000"), Role::Plain)), "open the FanoutCache's root directory");
    assert_eq!(shards[0].handles(), 0);
}

#[test]
fn a_renamed_shard_directory_is_refused() {
    let tmp = TempDir::new("shard-renamed");
    let root = tmp.path().join("root");
    for store in open_root(&root, 2) {
        store.release_handle();
    }
    std::fs::rename(root.join("000"), root.join("tmp")).unwrap();
    std::fs::rename(root.join("001"), root.join("000")).unwrap();
    std::fs::rename(root.join("tmp"), root.join("001")).unwrap();
    refused(Store::open(&options(&root.join("000"), shard(0, 2))), "this database is shard 1 of 2");
    refused(Store::open(&options(&root.join("001"), shard(1, 2))), "this database is shard 0 of 2");
}

#[test]
fn a_lost_manifest_recreated_with_another_count_is_refused_by_the_shard_stamps() {
    let tmp = TempDir::new("shard-lost-manifest");
    for store in open_root(tmp.path(), 4) {
        store.release_handle();
    }
    std::fs::remove_file(tmp.path().join(MANIFEST_FILENAME)).unwrap();
    assert_eq!(fanout::open_manifest(tmp.path(), Some(2)).unwrap().shards(), 2);
    refused(Store::open(&options(&tmp.path().join("000"), shard(0, 2))), "this database is shard 0 of 4");
    refused(Store::open(&options(&tmp.path().join("001"), shard(1, 2))), "this database is shard 1 of 4");
}

#[test]
fn a_shard_directory_a_plain_cache_created_is_never_adopted() {
    let tmp = TempDir::new("shard-adopt");
    let dir = tmp.path().join("000");
    let plain = Store::open(&options(&dir, Role::Plain)).unwrap();
    // Live (fast path), then closed (slow path).
    refused(Store::open(&options(&dir, shard(0, 2))), "belongs to a plain Cache");
    assert_eq!(plain.handles(), 1);
    plain.release_handle();
    refused(Store::open(&options(&dir, shard(0, 2))), "belongs to a plain Cache");
}

#[test]
fn a_plain_cache_on_a_fanout_root_is_refused_and_leaves_no_database() {
    let tmp = TempDir::new("shard-plain-root");
    fanout::open_manifest(tmp.path(), None).unwrap();
    refused(Store::open(&options(tmp.path(), Role::Plain)), "is a FanoutCache root");
    assert!(!tmp.path().join(DB_FILENAME).exists(), "the refused Cache must not create its database");
    // The root still opens as a FanoutCache afterwards.
    assert_eq!(fanout::open_manifest(tmp.path(), None).unwrap().shards(), fanout::DEFAULT_SHARDS);
}

#[test]
fn a_fanout_cache_on_a_plain_cache_directory_is_refused_and_writes_no_manifest() {
    let tmp = TempDir::new("shard-fanout-plain");
    let plain = Store::open(&options(tmp.path(), Role::Plain)).unwrap();
    for shards in [None, Some(4)] {
        let err = fanout::open_manifest(tmp.path(), shards).unwrap_err();
        assert!(matches!(err, Error::RoleMismatch { .. }), "{err:?}");
        assert!(err.to_string().contains("holds a plain Cache"), "{err}");
    }
    assert!(!tmp.path().join(MANIFEST_FILENAME).exists());
    // The plain cache is unharmed.
    plain.release_handle();
    let reopened = Store::open(&options(tmp.path(), Role::Plain)).unwrap();
    reopened.release_handle();
}

#[test]
fn a_directory_left_with_both_markers_is_refused_by_both_and_nothing_is_deleted() {
    let tmp = TempDir::new("shard-both");
    Store::open(&options(tmp.path(), Role::Plain)).unwrap().release_handle();
    std::fs::write(tmp.path().join(MANIFEST_FILENAME), fanout::Manifest::new(8).unwrap().render()).unwrap();
    refused(Store::open(&options(tmp.path(), Role::Plain)), "is a FanoutCache root");
    assert!(matches!(fanout::open_manifest(tmp.path(), None), Err(Error::RoleMismatch { .. })));
    assert!(tmp.path().join(MANIFEST_FILENAME).exists());
    assert!(tmp.path().join(DB_FILENAME).exists());
}

#[test]
fn a_plain_cache_racing_a_fanout_cache_for_one_root_never_both_succeed() {
    for round in 0..40 {
        let tmp = TempDir::new("shard-race");
        let root = tmp.path().join("root");
        let barrier = std::sync::Barrier::new(2);
        let (plain, fanout) = std::thread::scope(|s| {
            let plain = s.spawn(|| {
                barrier.wait();
                Store::open(&options(&root, Role::Plain))
            });
            let fanout = s.spawn(|| {
                barrier.wait();
                fanout::open_manifest(&root, None)
            });
            (plain.join().unwrap(), fanout.join().unwrap())
        });
        assert!(plain.is_err() || fanout.is_err(), "round {round}: both a Cache and a FanoutCache opened one root");
        for err in plain.as_ref().err().into_iter().chain(fanout.as_ref().err()) {
            assert!(matches!(err, Error::RoleMismatch { .. }), "round {round}: {err:?}");
        }
        if let Ok(store) = plain {
            store.release_handle();
        }
    }
}

/// A hook run with the directory being opened.
type Hook = Box<dyn FnOnce(&Path)>;

thread_local! {
    /// Run once by the next plain `Store::open_until` on this thread, between its store and its second
    /// manifest check, so a test can create the manifest exactly there.
    static AFTER_OPEN_STORE: std::cell::RefCell<Option<Hook>> = const { std::cell::RefCell::new(None) };
}

/// The test hook `Store::open_until` calls; does nothing unless a test armed it on this thread.
pub(super) fn after_open_store(directory: &Path) {
    if let Some(hook) = AFTER_OPEN_STORE.with(|h| h.borrow_mut().take()) {
        hook(directory);
    }
}

/// Arms [`after_open_store`] to write a manifest into the directory being opened.
fn write_manifest_after_open_store() {
    AFTER_OPEN_STORE.with(|h| {
        *h.borrow_mut() = Some(Box::new(|dir: &Path| {
            std::fs::write(dir.join(MANIFEST_FILENAME), fanout::Manifest::new(8).unwrap().render()).unwrap();
        }));
    });
}

#[test]
fn a_manifest_that_appears_after_the_store_opens_is_refused_and_the_joined_handle_released() {
    // No other test's fork pending: a refusal behind one defers its release, by design.
    let _forks = crate::testutil::one_global_fork_at_a_time();
    let tmp = TempDir::new("shard-post-check-join");
    let store = Store::open(&options(tmp.path(), Role::Plain)).unwrap();
    write_manifest_after_open_store();
    // The fast path: this open joins the live store (2 handles), then its second check refuses.
    refused(Store::open(&options(tmp.path(), Role::Plain)), "is a FanoutCache root");
    assert_eq!(store.handles(), 1, "the refused open released the handle it joined with");
    store.release_handle();
    assert_eq!(store.handles(), 0);
}

#[test]
fn a_manifest_that_appears_after_a_new_store_opens_is_refused_and_leaves_no_handle_counted() {
    // No other test's fork pending: a refusal behind one defers its release, by design.
    let _forks = crate::testutil::one_global_fork_at_a_time();
    let tmp = TempDir::new("shard-post-check-new");
    write_manifest_after_open_store();
    // The slow path: this open creates the store, then its second check refuses.
    refused(Store::open(&options(tmp.path(), Role::Plain)), "is a FanoutCache root");
    let gate = GATE.enter_shared_blocking();
    let live: Vec<Arc<Store>> =
        registry(&gate).iter().filter_map(Weak::upgrade).filter(|s| s.directory == tmp.path()).collect();
    drop(gate);
    assert!(live.iter().all(|s| s.handles() == 0 && s.is_retired()), "the refused open kept its handle");
    drop(live);
    // Once the manifest is gone the directory opens again, with exactly one handle counted.
    std::fs::remove_file(tmp.path().join(MANIFEST_FILENAME)).unwrap();
    let store = Store::open(&options(tmp.path(), Role::Plain)).unwrap();
    assert_eq!(store.handles(), 1);
    store.release_handle();
}
