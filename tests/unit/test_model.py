"""A stateful model test: random sequences of single-key calls against a dict with expiry times.

Hypothesis drives `set`, `add`, `get`, `pop`, `delete`, `touch`, `incr`, `expire`, `clear`, `iterkeys`,
committed and aborted transactions, a clock that jumps across expiry boundaries, and closing and
reopening the cache, over a few keys so the calls collide.
Values straddle the 64 KiB spill threshold, so inline and file-backed values replace one another.
After every step each key reads back as the model says. Keys are `str` and typed (an int and a tuple), so
the two kinds share every rule. Runs on `Cache` and on a `FanoutCache`.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import hypothesis.strategies as st
from diskcache_oxide import Cache, FanoutCache
from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

# Two str keys and two typed keys (D-004), so every rule meets both kinds and they collide.
KEY_LIST: list[Any] = ['a', 'b', 7, (7, None)]
KEYS = st.sampled_from(KEY_LIST)


def key_order(key: Any) -> tuple[int, Any]:
    """The engine's order for KEY_LIST (D-046): str keys, then ints, then tuples."""
    if isinstance(key, str):
        return (0, key)
    return (1, 0) if isinstance(key, int) else (2, 0)


SPILL = 64 * 1024
# Raw bytes are stored as they are, so these lengths sit on both sides of the spill threshold.
BYTES = st.builds(
    lambda n, b: bytes([b]) * n,
    st.sampled_from([0, 1, SPILL - 1, SPILL, SPILL + 1, 3 * SPILL]),
    st.integers(0, 255),
)
NUMBERS = st.integers(-(2**40), 2**40) | st.floats(-1e9, 1e9, allow_nan=False, allow_infinity=False)
VALUES = (
    BYTES
    | NUMBERS
    | st.text(max_size=20)
    # Long strings, built rather than drawn (Hypothesis can't draw text this long); 'é' is two bytes.
    | st.builds(lambda n, c: c * n, st.sampled_from([SPILL // 2, SPILL - 1, SPILL, SPILL + 1]), st.sampled_from('xé'))
    | st.lists(st.integers(-5, 5), max_size=4)
    | st.dictionaries(st.text(max_size=3), st.integers(-5, 5), max_size=3)
)
# None never expires; zero and negative are already expired, as in diskcache.
EXPIRES = st.none() | st.sampled_from([-1.0, 0.0, 0.5, 1.0, 2.0, 10.0])
STEPS = st.sampled_from([0.25, 0.5, 1.0, 1.5, 3.0, 20.0])
MISSING = object()


def same(a: Any, b: Any) -> bool:
    """Equal, and of the same type: an int must not come back a float, or bytes a str."""
    return type(a) is type(b) and a == b


class CacheModel(RuleBasedStateMachine):
    """`Cache` against a dict of key -> (value, expire_time), holding every stored row, live or expired."""

    def make(self, directory: Path) -> Any:
        return Cache(directory, clock=lambda: self.now)

    def transaction(self, key: Any) -> Any:
        return self.cache.transact()

    def __init__(self) -> None:
        super().__init__()
        self.root = Path(tempfile.mkdtemp(prefix='dco-model-'))
        self.now = 1_000_000.0
        self.model: dict[Any, tuple[Any, float | None]] = {}
        self.cache = self.make(self.root / 'c')

    def teardown(self) -> None:
        self.cache.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # -- the model's view ---------------------------------------------------------------------

    def live(self, key: Any) -> Any:
        value, expire_time = self.model.get(key, (MISSING, None))
        if value is MISSING or (expire_time is not None and expire_time <= self.now):
            return MISSING
        return value

    def expire_time(self, expire: float | None) -> float | None:
        return None if expire is None else self.now + expire

    # -- rules --------------------------------------------------------------------------------

    @rule(key=KEYS, value=VALUES, expire=EXPIRES)
    def set(self, key: Any, value: Any, expire: float | None) -> None:
        assert self.cache.set(key, value, expire=expire) is True
        self.model[key] = (value, self.expire_time(expire))

    @rule(key=KEYS, value=VALUES, expire=EXPIRES)
    def add(self, key: Any, value: Any, expire: float | None) -> None:
        added = self.live(key) is MISSING
        assert self.cache.add(key, value, expire=expire) is added
        if added:
            self.model[key] = (value, self.expire_time(expire))

    @rule(key=KEYS)
    def pop(self, key: Any) -> None:
        expected = self.live(key)
        got = self.cache.pop(key, MISSING)
        if expected is MISSING:
            assert got is MISSING, got
        else:
            assert same(got, expected), (got, expected)
            del self.model[key]  # An expired row isn't removed: it's a miss that stays for expire().

    @rule(key=KEYS)
    def delete(self, key: Any) -> None:
        present = self.live(key) is not MISSING
        assert self.cache.delete(key) is present
        if present:
            del self.model[key]

    @rule(key=KEYS, expire=EXPIRES)
    def touch(self, key: Any, expire: float | None) -> None:
        present = self.live(key) is not MISSING
        assert self.cache.touch(key, expire=expire) is present
        if present:
            self.model[key] = (self.model[key][0], self.expire_time(expire))

    @rule(key=KEYS, delta=st.integers(-1000, 1000))
    def incr(self, key: Any, delta: int) -> None:
        current = self.live(key)
        if current is MISSING:
            got = self.cache.incr(key, delta)
            assert same(got, delta), got
            self.model[key] = (delta, None)  # A missing key starts from 0 and gets no expiry.
        elif isinstance(current, (int, float)):
            got = self.cache.incr(key, delta)
            assert same(got, current + delta), (got, current, delta)
            self.model[key] = (current + delta, self.model[key][1])  # The expiry is kept.
        else:
            try:
                self.cache.incr(key, delta)
            except TypeError:
                pass
            else:
                raise AssertionError(f'incr of {type(current).__name__} did not raise TypeError')

    @rule(step=STEPS)
    def advance(self, step: float) -> None:
        self.now += step

    @rule()
    def expire(self) -> None:
        # As in diskcache, a read misses at `expire_time <= now` but expire() removes only
        # `expire_time < now`: an entry expiring exactly now is a miss that stays until a later
        # expire(). Only a frozen clock can see that.
        self.cache.expire()
        self.model = {k: (v, t) for k, (v, t) in self.model.items() if t is None or t >= self.now}
        assert len(self.cache) == len(self.model), 'expire() leaves exactly the unexpired entries'

    @rule()
    def clear(self) -> None:
        # Rows that expired before now may already be gone (every write sweeps a few), so the count
        # is bounded by the live rows below and every row the model holds above.
        live = sum(1 for k in self.model if self.live(k) is not MISSING)
        removed = self.cache.clear()
        assert live <= removed <= len(self.model), (removed, live, len(self.model))
        self.model = {}
        assert len(self.cache) == 0

    @rule(prefix=st.sampled_from([None, '', 'a', 'b', 'z']), reverse=st.booleans())
    def iterkeys(self, prefix: str | None, reverse: bool) -> None:
        keys: list[Any] = list(self.cache.iterkeys(prefix=prefix, reverse=reverse))
        assert keys == sorted(keys, key=key_order, reverse=reverse), keys
        # A prefix walks str keys only. Expired rows not yet removed are included, so: every live
        # key, and only keys the model holds.

        def matches(k: Any) -> bool:
            return prefix is None or (isinstance(k, str) and k.startswith(prefix))

        live = {k for k in self.model if matches(k) and self.live(k) is not MISSING}
        held = {k for k in self.model if matches(k)}
        assert live <= set(keys) <= held, (keys, live, held)

    @rule()
    def reversed_is_iteration_backwards(self) -> None:
        assert list(reversed(self.cache)) == list(self.cache)[::-1]

    @rule(key=KEYS, value=VALUES, delta=st.integers(-5, 5), abort=st.booleans())
    def transact(self, key: Any, value: Any, delta: int, abort: bool) -> None:
        # One key, so a FanoutCache's one-shard transaction covers every call inside it.
        class Abort(Exception):
            pass

        try:
            with self.transaction(key):
                self.cache.set(key, value)
                inside = self.cache.get(key, MISSING)
                assert same(inside, value), (inside, value)
                if isinstance(value, (int, float)):
                    self.cache.incr(key, delta)
                if abort:
                    raise Abort
        except Abort:
            pass  # Rolled back: the model is unchanged.
        else:
            stored = value + delta if isinstance(value, (int, float)) else value
            self.model[key] = (stored, None)

    @rule()
    def reopen(self) -> None:
        self.cache.close()
        self.cache = self.make(self.root / 'c')

    # -- after every step ---------------------------------------------------------------------

    @invariant()
    def every_key_reads_as_the_model_says(self) -> None:
        for key in KEY_LIST:
            expected = self.live(key)
            got = self.cache.get(key, MISSING)
            if expected is MISSING:
                assert got is MISSING, (key, got)
                assert key not in self.cache
            else:
                assert same(got, expected), (key, got, expected)
                assert key in self.cache


class FanoutCacheModel(CacheModel):
    """The same model on a FanoutCache, whose keys land on different shards."""

    def make(self, directory: Path) -> Any:
        return FanoutCache(directory, shards=4, clock=lambda: self.now)

    def transaction(self, key: Any) -> Any:
        return self.cache.transact(key=key)


# A tenth of the suite's example count: each example is up to 40 steps. HYPOTHESIS_EXAMPLES=30000 for a long run.
MODEL_EXAMPLES = int(os.environ.get('HYPOTHESIS_EXAMPLES', '1500')) // 10
MODEL_SETTINGS = settings(parent=settings.get_profile('unit'), max_examples=MODEL_EXAMPLES, stateful_step_count=40)
CacheModel.TestCase.settings = MODEL_SETTINGS
FanoutCacheModel.TestCase.settings = MODEL_SETTINGS
TestCacheModel = CacheModel.TestCase
TestFanoutCacheModel = FanoutCacheModel.TestCase
