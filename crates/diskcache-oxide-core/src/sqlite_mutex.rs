//! SQLite's mutexes, supplied by this crate through `SQLITE_CONFIG_MUTEX` so that a contended one
//! spins briefly before it parks (plan 12, step 5a). Every read takes SQLite's per-file
//! shared-memory mutex twice, and it's held across a `fcntl` call, so threads sharing one store
//! collide on it; a short spin keeps a waiter off the kernel for a hold of a microsecond or two.
//!
//! The mutexes are still pthread mutexes, taken by the same SQLite call sites, so fork behavior is
//! unchanged. SQLite's own pthread methods are `SQLITE_PRIVATE` in the amalgamation, so this is a
//! full implementation of all nine methods, not a wrapper.
//!
//! The methods must be installed before SQLite initializes, and SQLite initializes itself on first
//! use (every rusqlite open does it). [`ensure_installed`] does both under one `OnceLock`, and every
//! open in both crates goes through `crate::sqlite::open*`, which calls it first. A direct open
//! racing the install is heap corruption, not a fallback (see `sqlite::tests::every_open_goes_through_the_entry_point`).
//!
//! Nothing here may panic, allocate through SQLite (`sqlite3_malloc` takes the MEM static mutex) or
//! use `Box` on the allocation path (it aborts on OOM, where SQLite expects null). It never calls
//! `sqlite3_shutdown` or `SQLITE_CONFIG_GETMUTEX`.
//!
//! One of two modules in core that may use `unsafe` (see `stack::tests::only_these_modules_allow_unsafe`).

#![allow(unsafe_code)]
#![warn(clippy::undocumented_unsafe_blocks)]

use std::alloc::{self, Layout};
use std::cell::UnsafeCell;
use std::ffi::c_int;
use std::hint;
use std::mem::MaybeUninit;
use std::ptr;
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

use rusqlite::ffi;

/// The total number of `spin_loop` hints a contended enter spends before it blocks. A budget of
/// hints, not of tries: the gaps between tries grow 1, 2, 4, ... up to [`MAX_GAP`], so a try count
/// would hide how long the spin really is (on x86 a `pause` is roughly 100-140 cycles).
///
/// Chosen by plan 12 step 5's sweep over 0/256/1,024/4,096 (`bench/results/spin-sweep-2026-11-05.md`):
/// within 3% of the best budget wherever spinning helps, without 4,096's loss when oversubscribed.
/// Measured on arm64 (the Mac and a Linux VM); the x86-64 run is still to come. Not used at 2 or
/// fewer CPUs (see [`MIN_SPIN_CPUS`]).
const SPIN_HINTS: u32 = 1024;

/// The fewest CPUs at which a contended enter spins. Below it the budget is 0, today's plain
/// blocking. In the Linux VM at a 2-CPU quota (`--cpus 2`), every budget was 13-16% slower than none
/// for four threads on one shared `Cache`, and slower at eight too: the spinners used up the
/// quota, which then throttled the holder. A spinner on one CPU only delays the holder. At 3 and 4
/// CPUs four threads gain from the spin, so the cutoff is 3; eight threads on those quotas lose,
/// which is oversubscription, not the CPU count (`bench/results/spin-sweep-2026-11-05.md`).
const MIN_SPIN_CPUS: usize = 3;

/// The spin budget for a host with `cpus` CPUs (`available_parallelism()`, which on Linux honors
/// a cgroup quota). An unknown count keeps the spin.
fn budget_for(cpus: Option<usize>) -> u32 {
    if cpus.is_some_and(|n| n < MIN_SPIN_CPUS) { 0 } else { SPIN_HINTS }
}

/// The longest gap between two tries, in hints.
const MAX_GAP: u32 = 64;

const FIRST_STATIC: c_int = ffi::SQLITE_MUTEX_STATIC_MAIN;
const LAST_STATIC: c_int = ffi::SQLITE_MUTEX_STATIC_VFS3;
/// SQLite's static mutexes, ids 2 to 13 in 3.54.
const STATIC_COUNT: usize = (LAST_STATIC - FIRST_STATIC + 1) as usize;
// A static id a future SQLite adds would get null from `alloc`, so a libsqlite3-sys bump that moves
// these fails to build instead of misbehaving.
const _: () = assert!(FIRST_STATIC == 2 && LAST_STATIC == 13 && STATIC_COUNT == 12);

/// What [`ensure_installed`] achieved (see [`status`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MutexStatus {
    /// SQLite initialized with these methods: our `xMutexInit` ran.
    Installed,
    /// SQLite refused the config (`SQLITE_MISUSE`) because it was already initialized, so it keeps
    /// its own (correct, unspun) pthread mutexes. Safe only if that initialization had finished;
    /// nothing in these crates can start one elsewhere.
    TooLate,
    /// SQLite was initialized concurrently with the install; the process's SQLite state is unsound.
    /// The config was accepted (so SQLite wasn't initialized yet) but our `xMutexInit` never ran: an
    /// initialization outside the entry point started before the config, copied SQLite's default
    /// methods and handed out default static mutexes, and finished after it. Two mutex
    /// implementations now coexist, which ends in heap corruption. The Python module init (plan 12
    /// step 5, build-order step 2) must refuse to run on it: raise at import, or abort.
    Raced,
    /// `sqlite3_initialize` failed (or SQLite refused the methods with something other than
    /// MISUSE). Not terminal: callers go on, and a later `sqlite3_initialize` (any open runs one)
    /// uses our methods, since the config stays in place; [`status`] then reports `Installed`.
    InitFailed,
}

impl MutexStatus {
    /// The name the Python test hook reports.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Installed => "installed",
            Self::TooLate => "too_late",
            Self::Raced => "raced",
            Self::InitFailed => "init_failed",
        }
    }
}

/// The result of the one install attempt.
static ATTEMPT: OnceLock<MutexStatus> = OnceLock::new();

/// Installs the methods and initializes SQLite, once per process, and returns [`status`]. A
/// concurrent caller waits until the first one is done, so no open through `crate::sqlite::open*`
/// can reach SQLite between the config and the initialize.
///
/// A fork while another thread is inside the install leaves the child's `OnceLock` running forever,
/// so the child's first open hangs (SQLite's own init mutex had the same hazard). Installing at
/// import, before any thread can fork, closes it.
pub fn ensure_installed() -> MutexStatus {
    ATTEMPT.get_or_init(install);
    status().unwrap_or(MutexStatus::InitFailed)
}

/// The live status, or `None` before [`ensure_installed`] first ran. `InitFailed` becomes
/// `Installed` once a later initialization has run our `xMutexInit`.
#[must_use]
pub fn status() -> Option<MutexStatus> {
    let attempt = *ATTEMPT.get()?;
    Some(match attempt {
        MutexStatus::InitFailed if INSTALLED.load(Ordering::Acquire) => MutexStatus::Installed,
        other => other,
    })
}

/// Set by our `xMutexInit`, so it's true only if SQLite really initialized with these methods.
static INSTALLED: AtomicBool = AtomicBool::new(false);

/// [`SPIN_HINTS`], or 0 at 2 or fewer CPUs ([`budget_for`]). Read once at install.
static SPIN_BUDGET: AtomicU32 = AtomicU32::new(0);

/// The spin budget in force, for the sweep's harness to confirm its override took. Only in a sweep
/// build (`--cfg dco_spin_sweep`).
#[cfg(dco_spin_sweep)]
#[doc(hidden)]
#[must_use]
pub fn sweep_spin_budget() -> u32 {
    SPIN_BUDGET.load(Ordering::Relaxed)
}

/// The spin sweep's overrides (plan 12 step 5, build-order step 2), compiled in only under
/// `RUSTFLAGS="--cfg dco_spin_sweep"`, a benchmark build. A shipped build has no environment knob:
/// there these return the built-in values.
mod sweep {
    use std::ffi::c_int;

    /// The spin budget: `DCO_SPIN_HINTS` if set and a number, otherwise `default`.
    #[cfg(dco_spin_sweep)]
    pub(super) fn budget(default: u32) -> u32 {
        std::env::var("DCO_SPIN_HINTS").ok().and_then(|v| v.parse().ok()).unwrap_or(default)
    }

    #[cfg(not(dco_spin_sweep))]
    pub(super) fn budget(default: u32) -> u32 {
        default
    }

    /// The pthread type of a FAST mutex: the default, or glibc's adaptive (spinning) type when the
    /// sweep sets `DCO_SPIN_ADAPTIVE=1`. Read once.
    #[cfg(all(dco_spin_sweep, target_os = "linux", target_env = "gnu"))]
    pub(super) fn fast_kind() -> Option<c_int> {
        static ADAPTIVE: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
        ADAPTIVE
            .get_or_init(|| std::env::var("DCO_SPIN_ADAPTIVE").is_ok_and(|v| v == "1"))
            .then_some(libc::PTHREAD_MUTEX_ADAPTIVE_NP)
    }

    #[cfg(not(all(dco_spin_sweep, target_os = "linux", target_env = "gnu")))]
    pub(super) fn fast_kind() -> Option<c_int> {
        None
    }
}

fn install() -> MutexStatus {
    let budget = budget_for(std::thread::available_parallelism().ok().map(std::num::NonZero::get));
    SPIN_BUDGET.store(sweep::budget(budget), Ordering::Relaxed);
    // SAFETY: SQLITE_CONFIG_MUTEX takes one `sqlite3_mutex_methods*`, which SQLite copies; METHODS
    // is a 'static of exactly that type. Config only reads SQLite's global config before init, and
    // it's called once, under the OnceLock, before any connection this crate opens.
    let config = unsafe { ffi::sqlite3_config(ffi::SQLITE_CONFIG_MUTEX, &raw const METHODS) };
    // SAFETY: takes no arguments; safe to call at any time and from any thread. It's inside the same
    // OnceLock as the config, so no open of ours can initialize SQLite between the two.
    let init = unsafe { ffi::sqlite3_initialize() };
    if config == ffi::SQLITE_MISUSE {
        return MutexStatus::TooLate;
    }
    if config != ffi::SQLITE_OK || init != ffi::SQLITE_OK {
        return MutexStatus::InitFailed;
    }
    // Config accepted means our methods are in SQLite's global config; if our `xMutexInit` still
    // didn't run, another initialization had already copied the defaults (see `Raced`).
    if INSTALLED.load(Ordering::Acquire) { MutexStatus::Installed } else { MutexStatus::Raced }
}

static METHODS: ffi::sqlite3_mutex_methods = ffi::sqlite3_mutex_methods {
    xMutexInit: Some(mutex_init),
    xMutexEnd: Some(mutex_end),
    xMutexAlloc: Some(mutex_alloc),
    xMutexFree: Some(mutex_free),
    xMutexEnter: Some(mutex_enter),
    xMutexTry: Some(mutex_try),
    xMutexLeave: Some(mutex_leave),
    // SQLite calls these only inside `assert()`, which this build (no SQLITE_DEBUG, so NDEBUG)
    // compiles out. Supplied anyway, so a `-DSQLITE_DEBUG` build doesn't call a null pointer: debug
    // builds check the owner fields, release builds answer 1 to both, as SQLite's TSAN path does.
    xMutexHeld: Some(mutex_held),
    xMutexNotheld: Some(mutex_notheld),
};

/// One SQLite mutex. SQLite only ever sees a pointer to it, as `*mut sqlite3_mutex`.
#[repr(C)]
struct SpinMutex {
    raw: UnsafeCell<libc::pthread_mutex_t>,
    /// The holding thread's token (0 when free) and its recursion depth. Atomics, because the
    /// statics are shared between threads and a non-holder reads them in `held`.
    #[cfg(debug_assertions)]
    owner: std::sync::atomic::AtomicUsize,
    #[cfg(debug_assertions)]
    depth: AtomicU32,
}

// SAFETY: a pthread mutex is made to be shared between threads, and it is never moved once
// initialized (statics live in place, dynamic ones on the heap until freed). The debug fields are
// atomics.
unsafe impl Sync for SpinMutex {}

impl SpinMutex {
    /// A static one: `PTHREAD_MUTEX_INITIALIZER`, non-recursive, like SQLite's own statics.
    const fn new_static() -> Self {
        Self {
            raw: UnsafeCell::new(libc::PTHREAD_MUTEX_INITIALIZER),
            #[cfg(debug_assertions)]
            owner: std::sync::atomic::AtomicUsize::new(0),
            #[cfg(debug_assertions)]
            depth: AtomicU32::new(0),
        }
    }

    fn raw(&self) -> *mut libc::pthread_mutex_t {
        self.raw.get()
    }
}

/// Never freed: `free` recognizes them by address and leaves them alone.
static STATICS: [SpinMutex; STATIC_COUNT] = [const { SpinMutex::new_static() }; STATIC_COUNT];

/// Dynamic mutexes allocated so far (debug builds), for the install test.
#[cfg(debug_assertions)]
static DYNAMIC_ALLOCATIONS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// How many dynamic mutexes this process has allocated (debug builds only). Not part of the API.
#[cfg(debug_assertions)]
#[doc(hidden)]
#[must_use]
pub fn dynamic_allocations() -> usize {
    DYNAMIC_ALLOCATIONS.load(Ordering::Relaxed)
}

fn is_static(p: *const SpinMutex) -> bool {
    let start = STATICS.as_ptr().addr();
    let end = start + size_of_val(&STATICS);
    (start..end).contains(&p.addr())
}

/// Allocates and initializes a dynamic mutex, or returns null (on OOM or a pthread failure), as
/// SQLite expects.
fn new_dynamic(kind: Option<c_int>) -> *mut ffi::sqlite3_mutex {
    let layout = Layout::new::<SpinMutex>();
    // SAFETY: `SpinMutex` has a non-zero size, so the layout is valid for `alloc`.
    #[expect(clippy::cast_ptr_alignment, reason = "allocated with `SpinMutex`'s own layout")]
    let p = unsafe { alloc::alloc(layout) }.cast::<SpinMutex>();
    if p.is_null() {
        return ptr::null_mut();
    }
    // SAFETY: `p` is a fresh, aligned allocation for one `SpinMutex`, reachable from nowhere else.
    // The pthread mutex is initialized in place (POSIX doesn't allow initializing a copy), and the
    // debug fields are written through raw pointers, so no reference to uninitialized memory exists.
    let ok = unsafe {
        #[cfg(debug_assertions)]
        {
            (&raw mut (*p).owner).write(std::sync::atomic::AtomicUsize::new(0));
            (&raw mut (*p).depth).write(AtomicU32::new(0));
        }
        init_pthread_mutex((&raw mut (*p).raw).cast::<libc::pthread_mutex_t>(), kind)
    };
    if !ok {
        // SAFETY: `p` came from `alloc` with this layout and holds no initialized pthread mutex.
        unsafe { alloc::dealloc(p.cast(), layout) };
        return ptr::null_mut();
    }
    #[cfg(debug_assertions)]
    DYNAMIC_ALLOCATIONS.fetch_add(1, Ordering::Relaxed);
    p.cast()
}

/// Initializes the pthread mutex at `raw`, of type `kind` (`None` is the default attribute). False
/// if any pthread call fails, in which case `raw` holds no initialized mutex.
///
/// # Safety
///
/// `raw` must be valid for writes, aligned, not an initialized mutex, and never moved afterwards.
unsafe fn init_pthread_mutex(raw: *mut libc::pthread_mutex_t, kind: Option<c_int>) -> bool {
    let Some(kind) = kind else {
        // SAFETY: the caller's contract; a null attribute means the default (non-recursive) type.
        return unsafe { libc::pthread_mutex_init(raw, ptr::null()) } == 0;
    };
    let mut attr = MaybeUninit::<libc::pthread_mutexattr_t>::uninit();
    // SAFETY: `attr` is a local, valid for writes; init is what makes it a valid attribute.
    if unsafe { libc::pthread_mutexattr_init(attr.as_mut_ptr()) } != 0 {
        return false;
    }
    // SAFETY: `attr` was initialized above, and `raw` is the caller's contract.
    let ok = unsafe {
        libc::pthread_mutexattr_settype(attr.as_mut_ptr(), kind) == 0
            && libc::pthread_mutex_init(raw, attr.as_ptr()) == 0
    };
    // A failed destroy leaks nothing that matters: the mutex, if made, doesn't depend on `attr`.
    // SAFETY: `attr` was initialized above and is destroyed once.
    let _ = unsafe { libc::pthread_mutexattr_destroy(attr.as_mut_ptr()) };
    ok
}

/// One try. True if the mutex is now held by this thread.
fn try_lock(m: &SpinMutex) -> bool {
    // SAFETY: `m.raw()` is an initialized pthread mutex that outlives this call.
    unsafe { libc::pthread_mutex_trylock(m.raw()) == 0 }
}

/// The mutex behind a pointer SQLite hands back.
///
/// # Safety
///
/// `p` must be non-null and have come from `mutex_alloc`, and not be freed yet.
unsafe fn mutex<'a>(p: *mut ffi::sqlite3_mutex) -> &'a SpinMutex {
    // SAFETY: the caller's contract: `p` points to a live `SpinMutex`, which is only ever shared.
    unsafe { &*p.cast::<SpinMutex>() }
}

extern "C" fn mutex_init() -> c_int {
    INSTALLED.store(true, Ordering::Release);
    ffi::SQLITE_OK
}

extern "C" fn mutex_end() -> c_int {
    ffi::SQLITE_OK
}

extern "C" fn mutex_alloc(id: c_int) -> *mut ffi::sqlite3_mutex {
    match id {
        ffi::SQLITE_MUTEX_FAST => new_dynamic(sweep::fast_kind()),
        ffi::SQLITE_MUTEX_RECURSIVE => new_dynamic(Some(libc::PTHREAD_MUTEX_RECURSIVE)),
        FIRST_STATIC..=LAST_STATIC => usize::try_from(id - FIRST_STATIC)
            .ok()
            .and_then(|i| STATICS.get(i))
            .map_or(ptr::null_mut(), |m| ptr::from_ref(m).cast_mut().cast()),
        _ => ptr::null_mut(),
    }
}

unsafe extern "C" fn mutex_free(p: *mut ffi::sqlite3_mutex) {
    let m = p.cast::<SpinMutex>();
    if m.is_null() || is_static(m) {
        return;
    }
    // SAFETY: SQLite frees a dynamic mutex once, when nobody holds or uses it, and it came from
    // `new_dynamic`, so it's an initialized pthread mutex in an allocation with this layout.
    unsafe {
        libc::pthread_mutex_destroy((*m).raw());
        alloc::dealloc(m.cast(), Layout::new::<SpinMutex>());
    }
}

unsafe extern "C" fn mutex_enter(p: *mut ffi::sqlite3_mutex) {
    // SAFETY: SQLite passes only mutexes it got from `mutex_alloc` and hasn't freed.
    let m = unsafe { mutex(p) };
    if !try_lock(m) && !spin(m) {
        // SAFETY: an initialized pthread mutex. Like SQLite's own, the result isn't checked: the
        // only failures are EINVAL (not a mutex) and EDEADLK (an error-checking type, never made).
        unsafe { libc::pthread_mutex_lock(m.raw()) };
    }
    #[cfg(debug_assertions)]
    entered(m);
}

/// The spin between the first try and blocking: [`SPIN_BUDGET`] hints in all, in gaps of 1, 2, 4,
/// ... up to [`MAX_GAP`], with a try after each gap. No clock reads: one costs more than the spin.
fn spin(m: &SpinMutex) -> bool {
    let budget = SPIN_BUDGET.load(Ordering::Relaxed);
    let (mut spent, mut gap) = (0, 1);
    while spent < budget {
        let hints = gap.min(budget - spent);
        for _ in 0..hints {
            hint::spin_loop();
        }
        spent += hints;
        if try_lock(m) {
            return true;
        }
        gap = (gap * 2).min(MAX_GAP);
    }
    false
}

unsafe extern "C" fn mutex_try(p: *mut ffi::sqlite3_mutex) -> c_int {
    // SAFETY: SQLite passes only mutexes it got from `mutex_alloc` and hasn't freed.
    let m = unsafe { mutex(p) };
    if !try_lock(m) {
        return ffi::SQLITE_BUSY;
    }
    #[cfg(debug_assertions)]
    entered(m);
    ffi::SQLITE_OK
}

unsafe extern "C" fn mutex_leave(p: *mut ffi::sqlite3_mutex) {
    // SAFETY: SQLite passes only mutexes it got from `mutex_alloc` and hasn't freed.
    let m = unsafe { mutex(p) };
    #[cfg(debug_assertions)]
    leaving(m);
    // SAFETY: an initialized pthread mutex this thread holds (SQLite only leaves what it entered).
    unsafe { libc::pthread_mutex_unlock(m.raw()) };
}

/// A token unique to the calling thread while it lives: the address of a thread-local. Never 0.
#[cfg(debug_assertions)]
fn thread_token() -> usize {
    thread_local! {
        static TOKEN: u8 = const { 0 };
    }
    // A const-initialized thread-local with no destructor is never torn down, so this can't fail.
    TOKEN.with(|t| ptr::from_ref(t).addr())
}

/// Called with the mutex just taken by this thread.
#[cfg(debug_assertions)]
fn entered(m: &SpinMutex) {
    if m.depth.fetch_add(1, Ordering::Relaxed) == 0 {
        m.owner.store(thread_token(), Ordering::Relaxed);
    }
}

/// Called just before this thread releases the mutex. A failed `debug_assert!` here unwinds into an
/// `extern "C"` frame, which aborts the process: by design, since a leave by a non-owner means
/// SQLite's locking is already broken.
#[cfg(debug_assertions)]
fn leaving(m: &SpinMutex) {
    debug_assert_eq!(
        m.owner.load(Ordering::Relaxed),
        thread_token(),
        "an SQLite mutex left by a thread that doesn't hold it"
    );
    if m.depth.fetch_sub(1, Ordering::Relaxed) == 1 {
        m.owner.store(0, Ordering::Relaxed);
    }
}

#[cfg(debug_assertions)]
fn held_by_me(m: &SpinMutex) -> bool {
    m.depth.load(Ordering::Relaxed) > 0 && m.owner.load(Ordering::Relaxed) == thread_token()
}

#[cfg(debug_assertions)]
unsafe extern "C" fn mutex_held(p: *mut ffi::sqlite3_mutex) -> c_int {
    // SAFETY: SQLite passes only mutexes it got from `mutex_alloc` and hasn't freed.
    c_int::from(held_by_me(unsafe { mutex(p) }))
}

#[cfg(debug_assertions)]
unsafe extern "C" fn mutex_notheld(p: *mut ffi::sqlite3_mutex) -> c_int {
    // SAFETY: SQLite passes only mutexes it got from `mutex_alloc` and hasn't freed.
    c_int::from(!held_by_me(unsafe { mutex(p) }))
}

/// Release builds keep no owner, so both answers are 1: an `assert()` on either always passes.
#[cfg(not(debug_assertions))]
extern "C" fn mutex_held(_p: *mut ffi::sqlite3_mutex) -> c_int {
    1
}

#[cfg(not(debug_assertions))]
extern "C" fn mutex_notheld(_p: *mut ffi::sqlite3_mutex) -> c_int {
    1
}

#[cfg(test)]
mod tests {
    use std::sync::Barrier;
    use std::sync::atomic::AtomicU64;
    use std::thread;

    use super::*;

    /// A mutex pointer handed to other threads in a test.
    #[derive(Clone, Copy)]
    struct Shared(*mut ffi::sqlite3_mutex);
    // SAFETY: the pointee is a `SpinMutex`, which is `Sync`, and each test frees it only after
    // joining every thread it shared it with.
    unsafe impl Send for Shared {}

    fn alloc(id: c_int) -> *mut ffi::sqlite3_mutex {
        let p = mutex_alloc(id);
        assert!(!p.is_null(), "alloc({id}) returned null");
        p
    }

    fn enter(p: Shared) {
        // SAFETY: `p` came from `alloc` and is freed only after every user is done.
        unsafe { mutex_enter(p.0) }
    }

    fn try_enter(p: Shared) -> c_int {
        // SAFETY: as for `enter`.
        unsafe { mutex_try(p.0) }
    }

    fn leave(p: Shared) {
        // SAFETY: as for `enter`, and the calling thread holds it.
        unsafe { mutex_leave(p.0) }
    }

    fn free(p: Shared) {
        // SAFETY: nobody holds or uses `p` any more.
        unsafe { mutex_free(p.0) }
    }

    /// From a thread that doesn't hold it.
    fn try_elsewhere(p: Shared) -> c_int {
        thread::spawn(move || {
            let rc = try_enter(p);
            if rc == ffi::SQLITE_OK {
                leave(p);
            }
            rc
        })
        .join()
        .unwrap()
    }

    /// Every open in this test binary goes through `sqlite::open*`, so the first one installed.
    #[test]
    fn the_methods_are_installed_in_this_process() {
        assert_eq!(ensure_installed(), MutexStatus::Installed);
        assert_eq!(status(), Some(MutexStatus::Installed));
        crate::sqlite::open_in_memory().unwrap();
        assert_eq!(status(), Some(MutexStatus::Installed));
    }

    #[test]
    fn no_spin_at_two_or_fewer_cpus() {
        assert_eq!(budget_for(Some(1)), 0);
        assert_eq!(budget_for(Some(2)), 0);
        assert_eq!(budget_for(Some(3)), SPIN_HINTS);
        assert_eq!(budget_for(Some(18)), SPIN_HINTS);
        assert_eq!(budget_for(None), SPIN_HINTS, "an unknown count keeps the spin");
    }

    #[test]
    fn each_static_id_is_one_mutex() {
        let ids = FIRST_STATIC..=LAST_STATIC;
        let first: Vec<_> = ids.clone().map(alloc).collect();
        let again: Vec<_> = ids.map(alloc).collect();
        assert_eq!(first, again);
        let mut distinct = first.clone();
        distinct.dedup();
        assert_eq!(distinct.len(), STATIC_COUNT);
        assert!(first.iter().all(|&p| is_static(p.cast())));
    }

    #[test]
    fn unknown_ids_get_null() {
        for id in [-1, LAST_STATIC + 1, 100] {
            assert!(mutex_alloc(id).is_null(), "{id}");
        }
    }

    #[test]
    fn freeing_a_static_is_a_no_op() {
        let p = Shared(alloc(ffi::SQLITE_MUTEX_STATIC_APP3));
        free(p);
        enter(p);
        assert_eq!(try_elsewhere(p), ffi::SQLITE_BUSY);
        leave(p);
        assert_eq!(try_elsewhere(p), ffi::SQLITE_OK);
    }

    #[test]
    fn statics_are_recognized_by_address() {
        assert!(is_static(&raw const STATICS[0]));
        assert!(is_static(&raw const STATICS[STATIC_COUNT - 1]));
        let dynamic = alloc(ffi::SQLITE_MUTEX_FAST);
        assert!(!is_static(dynamic.cast()));
        free(Shared(dynamic));
    }

    #[test]
    fn a_recursive_mutex_nests() {
        let p = Shared(alloc(ffi::SQLITE_MUTEX_RECURSIVE));
        enter(p);
        enter(p);
        assert_eq!(try_enter(p), ffi::SQLITE_OK, "the holder can take it again");
        leave(p);
        leave(p);
        assert_eq!(try_elsewhere(p), ffi::SQLITE_BUSY, "still held once");
        leave(p);
        assert_eq!(try_elsewhere(p), ffi::SQLITE_OK);
        free(p);
    }

    #[test]
    fn try_on_a_held_fast_mutex_is_busy() {
        let p = Shared(alloc(ffi::SQLITE_MUTEX_FAST));
        assert_eq!(try_enter(p), ffi::SQLITE_OK);
        assert_eq!(try_elsewhere(p), ffi::SQLITE_BUSY);
        leave(p);
        assert_eq!(try_elsewhere(p), ffi::SQLITE_OK);
        free(p);
    }

    #[cfg(debug_assertions)]
    #[test]
    fn held_reports_the_owner_only() {
        let before = dynamic_allocations();
        let p = Shared(alloc(ffi::SQLITE_MUTEX_RECURSIVE));
        assert!(dynamic_allocations() > before);
        // SAFETY: `p` is live for the whole test.
        let (held, notheld) = (|| unsafe { mutex_held(p.0) }, || unsafe { mutex_notheld(p.0) });
        assert_eq!((held(), notheld()), (0, 1));
        enter(p);
        enter(p);
        assert_eq!((held(), notheld()), (1, 0));
        let elsewhere = thread::spawn(move || {
            let p = p;
            // SAFETY: as above.
            unsafe { (mutex_held(p.0), mutex_notheld(p.0)) }
        });
        assert_eq!(elsewhere.join().unwrap(), (0, 1));
        leave(p);
        assert_eq!(held(), 1, "still held once");
        leave(p);
        assert_eq!((held(), notheld()), (0, 1));
        free(p);
    }

    /// The spin budget `install()` sets, for tests that run without it (under Miri, `install()` is
    /// never reached, since SQLite is out of its reach). Natively it's already this on 3 or more CPUs.
    fn spin_on() {
        SPIN_BUDGET.store(SPIN_HINTS, Ordering::Relaxed);
    }

    /// Every increment is a plain load and store, so a lost one means two threads were inside at
    /// once. The spin path runs too, since the threads collide all the time and `spin_on` makes sure
    /// the budget isn't 0.
    #[test]
    fn contended_increments_lose_nothing() {
        spin_on();
        const THREADS: u64 = 8;
        const EACH: u64 = 20_000;
        for id in [ffi::SQLITE_MUTEX_FAST, ffi::SQLITE_MUTEX_RECURSIVE] {
            let p = Shared(alloc(id));
            let count = AtomicU64::new(0);
            let start = Barrier::new(usize::try_from(THREADS).unwrap());
            let (count_ref, start) = (&count, &start);
            thread::scope(|s| {
                for _ in 0..THREADS {
                    s.spawn(move || {
                        let count = count_ref;
                        start.wait();
                        for _ in 0..EACH {
                            enter(p);
                            count.store(count.load(Ordering::Relaxed) + 1, Ordering::Relaxed);
                            leave(p);
                        }
                    });
                }
            });
            assert_eq!(count.into_inner(), THREADS * EACH, "mutex id {id}");
            free(p);
        }
    }
}
