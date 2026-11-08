//! Two first opens of one directory racing in `Store::open_store` (plan 20, part B): the open that
//! loses the registration hands its connection to the winner's store and opens no pin, so nothing
//! is closed beside the winner's live pool (where SQLite's unix VFS would defer the close, D-017).
//! A thread is parked at [`after_prepare`], between its `prepare_creating` and its first registry
//! take, while another thread opens the directory.

use std::sync::mpsc;

use super::*;
use crate::schema::DB_FILENAME;
use crate::testutil::{TempDir, fds_on};

const T: Duration = Duration::from_secs(5);

fn options(dir: &Path, max_connections: u64) -> OpenOptions {
    OpenOptions {
        directory: dir.to_path_buf(),
        timeout: T,
        requested: Requested::default(),
        max_connections,
        cache_pages: DEFAULT_CACHE_PAGES,
        wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
        auto_checkpoint: false,
        role: Role::Plain,
        default_size_limit: crate::schema::DEFAULT_SIZE_LIMIT,
    }
}

/// A hook run with the directory being opened.
type Hook = Box<dyn FnOnce(&Path)>;

thread_local! {
    /// Run once by the next `Store::open_store` slow path on this thread, after `prepare_creating`.
    static AFTER_PREPARE: std::cell::RefCell<Option<Hook>> = const { std::cell::RefCell::new(None) };
}

/// The test hook `Store::open_store` calls; does nothing unless a test armed it on this thread.
pub(super) fn after_prepare(directory: &Path) {
    if let Some(hook) = AFTER_PREPARE.with(|h| h.borrow_mut().take()) {
        hook(directory);
    }
}

/// Opens `dir` on a new thread that parks at [`after_prepare`], runs `meanwhile` while it's parked,
/// then lets it finish. Returns the parked open's result and `meanwhile`'s.
fn parked_open<R>(dir: &Path, max_connections: u64, meanwhile: impl FnOnce() -> R) -> (Result<Arc<Store>>, R) {
    let (parked_tx, parked_rx) = mpsc::channel::<()>();
    let (go_tx, go_rx) = mpsc::channel::<()>();
    let opts = options(dir, max_connections);
    let parked = std::thread::spawn(move || {
        AFTER_PREPARE.with(|h| {
            *h.borrow_mut() = Some(Box::new(move |_: &Path| {
                parked_tx.send(()).unwrap();
                go_rx.recv().unwrap();
            }));
        });
        Store::open(&opts)
    });
    parked_rx.recv_timeout(T).expect("the open never reached the hook");
    let result = meanwhile();
    go_tx.send(()).unwrap();
    (parked.join().unwrap(), result)
}

/// A database for `dir` whose store is gone from the registry: opened, released and dropped.
fn existing_database(dir: &Path) {
    let store = Store::open(&options(dir, DEFAULT_MAX_CONNECTIONS)).unwrap();
    store.release_handle();
    let weak = Arc::downgrade(&store);
    drop(store);
    assert!(weak.upgrade().is_none(), "the first store is still alive");
}

fn idle(store: &Store) -> usize {
    let gate = GATE.enter_shared_blocking();
    store.pool.idle(&gate)
}

fn checked_out(store: &Store) -> usize {
    let gate = GATE.enter_shared_blocking();
    store.pool.checked_out(&gate)
}

fn release(stores: &[&Arc<Store>]) {
    for store in stores {
        store.release_handle();
    }
}

/// The race both ways it starts: on an existing database (the loser's id read before its connect
/// finds the file) and on a brand-new directory (it finds nothing, and the file appears at its open).
fn the_loser_joins_and_hands_over_its_connection(existing: bool) {
    let _forks = crate::testutil::one_global_fork_at_a_time();
    let tmp = TempDir::new(if existing { "open-race-existing" } else { "open-race-new" });
    if existing {
        existing_database(tmp.path());
    }
    let (parked, winner) =
        parked_open(tmp.path(), DEFAULT_MAX_CONNECTIONS, || Store::open(&options(tmp.path(), DEFAULT_MAX_CONNECTIONS)));
    let (parked, winner) = (parked.unwrap(), winner.unwrap());
    assert!(Arc::ptr_eq(&parked, &winner), "the parked open made a second store for the file");
    assert_eq!(winner.settled_handles(), 2);
    assert_eq!(idle(&winner), 2, "the loser's connection wasn't added to the pool");
    assert_eq!(checked_out(&winner), 0);
    // The store's pin and two pooled connections: nothing pending beside the pool.
    let db = tmp.path().join(DB_FILENAME);
    assert_eq!(fds_on(&db), 3, "a descriptor was closed beside the live pool and left pending");
    release(&[&parked, &winner]);
}

#[test]
fn a_racing_open_of_an_existing_database_joins_the_winner_and_leaves_no_pending_descriptor() {
    the_loser_joins_and_hands_over_its_connection(true);
}

#[test]
fn a_racing_first_open_of_a_new_directory_joins_the_winner_and_leaves_no_pending_descriptor() {
    the_loser_joins_and_hands_over_its_connection(false);
}

#[test]
fn a_connection_handed_to_a_pool_at_its_idle_cap_is_closed_and_one_asking_for_more_is_kept() {
    let _forks = crate::testutil::one_global_fork_at_a_time();
    for (asks, kept) in [(1, false), (2, true)] {
        let tmp = TempDir::new("open-race-cap");
        existing_database(tmp.path());
        let (parked, winner) = parked_open(tmp.path(), asks, || Store::open(&options(tmp.path(), 1)));
        let (parked, winner) = (parked.unwrap(), winner.unwrap());
        assert!(Arc::ptr_eq(&parked, &winner));
        let db = tmp.path().join(DB_FILENAME);
        if kept {
            assert_eq!(idle(&winner), 2, "a handle asking for two connections lost its own");
            // The pin and two pooled connections.
            assert_eq!(fds_on(&db), 3);
        } else {
            assert_eq!(idle(&winner), 1, "the pool grew past its idle cap");
            // The pin, the pooled connection, and the surplus one, whose close SQLite defers beside
            // the live pool (D-017's documented case).
            assert_eq!(fds_on(&db), 3);
        }
        assert!(winner.write(T, |_, _| Ok(())).is_ok(), "the store doesn't work");
        release(&[&parked, &winner]);
    }
}

/// Renames a valid, prepared database over `dir`'s, as another program replacing the file would.
fn replace_database(dir: &Path) {
    let other = TempDir::new("open-race-replacement");
    existing_database(other.path());
    std::fs::rename(other.path().join(DB_FILENAME), dir.join(DB_FILENAME)).unwrap();
}

#[test]
fn a_racing_open_whose_connection_is_on_a_replaced_file_joins_but_doesnt_hand_it_over() {
    let _forks = crate::testutil::one_global_fork_at_a_time();
    let tmp = TempDir::new("open-race-replaced");
    existing_database(tmp.path());
    let (parked, winner) = parked_open(tmp.path(), DEFAULT_MAX_CONNECTIONS, || {
        replace_database(tmp.path());
        Store::open(&options(tmp.path(), DEFAULT_MAX_CONNECTIONS))
    });
    let (parked, winner) = (parked.unwrap(), winner.unwrap());
    assert!(Arc::ptr_eq(&parked, &winner), "the parked open didn't join the store on the new file");
    let db = std::fs::metadata(tmp.path().join(DB_FILENAME)).unwrap();
    assert_eq!(winner.file_id, (db.dev(), db.ino()));
    assert_eq!(idle(&winner), 1, "a connection on the old file went into the new file's pool");
    release(&[&parked, &winner]);
}

#[test]
fn an_open_whose_database_is_replaced_before_its_pin_fails_instead_of_seeding_from_the_old_file() {
    let _forks = crate::testutil::one_global_fork_at_a_time();
    let tmp = TempDir::new("open-race-seed-replaced");
    existing_database(tmp.path());
    let (parked, ()) = parked_open(tmp.path(), DEFAULT_MAX_CONNECTIONS, || replace_database(tmp.path()));
    match parked {
        Err(Error::Io { source, .. }) => {
            assert!(source.to_string().contains("replaced while it was being opened"), "{source}");
        }
        other => panic!("expected the replaced file to fail the open, got {other:?}"),
    }
    let gate = GATE.enter_shared_blocking();
    let live = registry(&gate).iter().filter_map(Weak::upgrade).filter(|s| s.directory == tmp.path()).count();
    drop(gate);
    assert_eq!(live, 0, "the failed open registered a store");
    assert_eq!(fds_on(&tmp.path().join(DB_FILENAME)), 0, "the failed open left its pin open");
}

#[test]
fn a_racing_open_of_a_retired_store_joins_it_once_it_is_revived() {
    let _forks = crate::testutil::one_global_fork_at_a_time();
    let tmp = TempDir::new("open-race-retired");
    let first = Store::open(&options(tmp.path(), DEFAULT_MAX_CONNECTIONS)).unwrap();
    first.release_handle();
    assert!(first.is_retired());
    let (parked, winner) =
        parked_open(tmp.path(), DEFAULT_MAX_CONNECTIONS, || Store::open(&options(tmp.path(), DEFAULT_MAX_CONNECTIONS)));
    let (parked, winner) = (parked.unwrap(), winner.unwrap());
    assert!(Arc::ptr_eq(&winner, &first), "the winner didn't revive the retired store");
    assert!(Arc::ptr_eq(&parked, &first), "the parked open didn't join the revived store");
    assert!(!first.is_retired());
    assert_eq!(first.settled_handles(), 2);
    // The revive's pin and the two openers' connections, both pooled: nothing pending.
    assert_eq!(idle(&first), 2);
    assert_eq!(fds_on(&tmp.path().join(DB_FILENAME)), 3);
    release(&[&parked, &winner]);
}
