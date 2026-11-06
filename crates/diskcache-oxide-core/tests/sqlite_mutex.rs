//! The mutex methods are installed before SQLite's first use (plan 12 step 5a). Its own test binary,
//! so it's its own process and nothing initialized SQLite first; one test, so nothing else in this
//! process runs before it.

// Two raw SQLite calls, neither of which initializes SQLite; the grep test in `sqlite.rs` allows them
// here by name.
#![allow(unsafe_code)]

use std::mem::MaybeUninit;
use std::path::PathBuf;
use std::time::Duration;

use diskcache_oxide_core::open::{Requested, Role};
use diskcache_oxide_core::schema::DEFAULT_SIZE_LIMIT;
use diskcache_oxide_core::sqlite;
use diskcache_oxide_core::sqlite_mutex::{self, MutexStatus};
use diskcache_oxide_core::store::{
    DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, OpenOptions, Store,
};
use rusqlite::ffi;

/// `PRAGMA compile_options` without `COMPILER=`, which names the host's C compiler. A change here is
/// a change to the shipped SQLite, so it should be deliberate.
const COMPILE_OPTIONS: &[&str] = &[
    "ATOMIC_INTRINSICS=1",
    "DEFAULT_AUTOVACUUM",
    "DEFAULT_CACHE_SIZE=-2000",
    "DEFAULT_FILE_FORMAT=4",
    "DEFAULT_FOREIGN_KEYS",
    "DEFAULT_JOURNAL_SIZE_LIMIT=-1",
    "DEFAULT_MEMSTATUS=0",
    "DEFAULT_MMAP_SIZE=0",
    "DEFAULT_PAGE_SIZE=4096",
    "DEFAULT_PCACHE_INITSZ=20",
    "DEFAULT_RECURSIVE_TRIGGERS",
    "DEFAULT_SECTOR_SIZE=4096",
    "DEFAULT_SYNCHRONOUS=2",
    "DEFAULT_WAL_AUTOCHECKPOINT=1000",
    "DEFAULT_WAL_SYNCHRONOUS=2",
    "DEFAULT_WORKER_THREADS=0",
    "DIRECT_OVERFLOW_READ",
    "ENABLE_API_ARMOR",
    "ENABLE_COLUMN_METADATA",
    "ENABLE_DBSTAT_VTAB",
    "ENABLE_FTS3",
    "ENABLE_FTS3_PARENTHESIS",
    "ENABLE_FTS5",
    "ENABLE_LOAD_EXTENSION",
    "ENABLE_RTREE",
    "HAVE_ISNAN",
    "MALLOC_SOFT_LIMIT=1024",
    "MAX_ATTACHED=10",
    "MAX_COLUMN=2000",
    "MAX_COMPOUND_SELECT=500",
    "MAX_DEFAULT_PAGE_SIZE=8192",
    "MAX_EXPR_DEPTH=1000",
    "MAX_FUNCTION_ARG=1000",
    "MAX_LENGTH=1000000000",
    "MAX_LIKE_PATTERN_LENGTH=50000",
    "MAX_MMAP_SIZE=0x7fff0000",
    "MAX_PAGE_COUNT=0xfffffffe",
    "MAX_PAGE_SIZE=65536",
    "MAX_SCHEMA=10000000",
    "MAX_SQL_LENGTH=1000000000",
    "MAX_TRIGGER_DEPTH=1000",
    "MAX_VARIABLE_NUMBER=32766",
    "MAX_VDBE_OP=250000000",
    "MAX_WORKER_THREADS=8",
    "MUTEX_PTHREADS",
    "SOUNDEX",
    "SYSTEM_MALLOC",
    "TEMP_STORE=1",
    "THREADSAFE=1",
    "USE_URI",
];

/// A fresh directory under the system temp dir, removed on drop.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn installed_before_the_first_connection() {
    assert_eq!(sqlite_mutex::status(), None, "nothing may install before the first open");
    #[cfg(debug_assertions)]
    assert_eq!(sqlite_mutex::dynamic_allocations(), 0);

    let dir = Scratch(std::env::temp_dir().join(format!("dco-sqlite-mutex-{}", std::process::id())));
    let store = Store::open(&OpenOptions {
        directory: dir.0.clone(),
        timeout: Duration::from_secs(5),
        requested: Requested::default(),
        max_connections: DEFAULT_MAX_CONNECTIONS,
        cache_pages: DEFAULT_CACHE_PAGES,
        wal_size_limit: DEFAULT_WAL_SIZE_LIMIT,
        auto_checkpoint: false,
        role: Role::Plain,
        default_size_limit: DEFAULT_SIZE_LIMIT,
    })
    .unwrap();
    assert_eq!(store.len(Duration::from_secs(5)).unwrap(), 0);

    assert_eq!(sqlite_mutex::status(), Some(MutexStatus::Installed));
    assert_eq!(sqlite_mutex::ensure_installed(), MutexStatus::Installed);
    #[cfg(debug_assertions)]
    assert!(sqlite_mutex::dynamic_allocations() > 0, "SQLite allocated no mutex through our methods");

    // Config is refused once SQLite is initialized, so it was initialized after ours was accepted.
    let mut methods = MaybeUninit::<ffi::sqlite3_mutex_methods>::zeroed();
    // SAFETY: GETMUTEX takes one `sqlite3_mutex_methods*` to write to; after initialization SQLite
    // returns MISUSE without touching it. Neither call initializes SQLite.
    let (getmutex, threadsafe) =
        unsafe { (ffi::sqlite3_config(ffi::SQLITE_CONFIG_GETMUTEX, methods.as_mut_ptr()), ffi::sqlite3_threadsafe()) };
    assert_eq!(getmutex, ffi::SQLITE_MISUSE);
    assert_eq!(threadsafe, 1);

    let options: Vec<String> =
        sqlite::compile_options().unwrap().into_iter().filter(|o| !o.starts_with("COMPILER=")).collect();
    assert_eq!(options, COMPILE_OPTIONS);
    drop(store);
}
