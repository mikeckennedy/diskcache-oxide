//! Background maintenance (plan 28): one thread per process that does each open store's housekeeping
//! on a schedule, so a cache that's mostly read still frees its expired rows' space, gets back under
//! its size limit, keeps its write-ahead log small and loses its stray temporary files.
//!
//! [`schedule`] is the timing, as pure functions over plain values: when each store's turn comes, and
//! which tasks a turn runs. `turn` is one turn on one store.

pub mod schedule;
pub(crate) mod turn;

use std::sync::atomic::{AtomicU64, Ordering};

/// A store's maintenance state in this process that the turn needs between turns.
#[derive(Debug, Default)]
pub(crate) struct Upkeep {
    /// Turns running on this store (decision 13), as a counter tagged with the process generation:
    /// a forked child, which has no maintenance thread, reads a turn the parent had in flight as none.
    /// Raised only under the registry while the store is live, so the release that retires it,
    /// decided under the same lock, knows whether to wait.
    pub(crate) turns: crate::genlock::Slots,
    /// The claim time this process last wrote for each claim row ([`turn::Claim`]), as `f64` bits (0:
    /// none). A claim row holding exactly this value is this process's own, so a turn continuing its
    /// own work a second later doesn't mistake it for another process's.
    own_claims: [AtomicU64; 2],
}

impl Upkeep {
    pub(crate) fn own_claim(&self, claim: turn::Claim) -> Option<f64> {
        let bits = self.own_claims[claim as usize].load(Ordering::Acquire);
        (bits != 0).then(|| f64::from_bits(bits))
    }

    pub(crate) fn set_own_claim(&self, claim: turn::Claim, at: f64) {
        self.own_claims[claim as usize].store(at.to_bits(), Ordering::Release);
    }
}
