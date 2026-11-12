"""A stateful model test of the queue calls (plan 24, release 4): random pushes, pulls and peeks on
both sides of a few queues, nested and empty prefixes among them, with expiry, a clock that jumps,
`expire()`, and keys that sit inside a queue's range without being its items (a float, an ordinary
key sharing the prefix), which no call may ever take (D-054). After every step each queue's two ends
read as a deque model says.

Any write may sweep expired rows, so the next pushed number is predictable only while a queue holds
no expired item; otherwise the model checks it lies past every live item on that side.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

import hypothesis.strategies as st
from diskcache_oxide import Cache
from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

START = 500_000_000_000_000
PREFIXES: list[str | None] = [None, 'q', 'q-1', '']
QUEUES = st.sampled_from(PREFIXES)
SIDES = st.sampled_from(['front', 'back'])
VALUES = st.integers(-5, 5) | st.text(max_size=5) | st.builds(lambda n: b'x' * n, st.sampled_from([0, 70_000]))
EXPIRES = st.none() | st.sampled_from([0.5, 1.0, 5.0])
# Keys inside the queues' ranges that aren't their items.
INTRUDERS = st.sampled_from([5.5e14, True, 'q-9', 'q-1abc', 'q-1-9', '-12', 'q-0000000000000001'])


def number(prefix: str | None, key: Any) -> int:
    return key if prefix is None else int(key.rsplit('-', 1)[1])


class QueueModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.root = Path(tempfile.mkdtemp(prefix='dco-queue-model-'))
        self.now = 1_000_000.0
        self.cache = Cache(self.root / 'c', clock=lambda: self.now, eviction_policy='none')
        # Per queue: number -> (key, value, expire_time), every item that may still be stored.
        self.queues: dict[str | None, dict[int, tuple[Any, Any, float | None]]] = {p: {} for p in PREFIXES}

    def teardown(self) -> None:
        self.cache.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def live(self, prefix: str | None) -> list[tuple[int, Any, Any]]:
        items = self.queues[prefix]
        return sorted((n, k, v) for n, (k, v, t) in items.items() if t is None or t > self.now)

    @rule(prefix=QUEUES, side=SIDES, value=VALUES, expire=EXPIRES)
    def push(self, prefix: str | None, side: Literal['front', 'back'], value: Any, expire: float | None) -> None:
        items = self.queues[prefix]
        live = [n for n, *_ in self.live(prefix)]
        key = self.cache.push(value, prefix=prefix, side=side, expire=expire)
        n = number(prefix, key)
        if prefix is not None:
            assert key == f'{prefix}-{n:015d}', key
        else:
            assert type(key) is int, key
        if len(live) == len(items):  # Nothing expired: the number is exact.
            if not items:
                assert n == START, key
            else:
                assert n == (max(items) + 1 if side == 'back' else min(items) - 1), (key, sorted(items))
        elif live:
            assert n > max(live) if side == 'back' else n < min(live), (key, live)
        items.pop(n, None)  # A swept expired item's number can be reused.
        items[n] = (key, value, None if expire is None else self.now + expire)

    @rule(prefix=QUEUES, side=SIDES)
    def pull(self, prefix: str | None, side: Literal['front', 'back']) -> None:
        live = self.live(prefix)
        got = self.cache.pull(prefix=prefix, side=side)
        if not live:
            assert got == (None, None), got
            return
        n, key, value = live[0] if side == 'front' else live[-1]
        assert got == (key, value), (got, key, value)
        del self.queues[prefix][n]

    @rule(intruder=INTRUDERS)
    def intrude(self, intruder: Any) -> None:
        self.cache.set(intruder, 'not a queue item')

    @rule(step=st.sampled_from([0.25, 0.5, 1.0, 3.0]))
    def advance(self, step: float) -> None:
        self.now += step

    @rule()
    def expire(self) -> None:
        self.cache.expire()
        for items in self.queues.values():
            for n in [n for n, (_, _, t) in items.items() if t is not None and t < self.now]:
                del items[n]

    @invariant()
    def both_ends_read_as_the_model_says(self) -> None:
        for prefix in PREFIXES:
            live = self.live(prefix)
            front, back = self.cache.peek(prefix=prefix), self.cache.peek(prefix=prefix, side='back')
            if not live:
                assert front == back == (None, None), (prefix, front, back)
            else:
                assert front == live[0][1:], (prefix, front, live[0])
                assert back == live[-1][1:], (prefix, back, live[-1])


MODEL_EXAMPLES = int(os.environ.get('HYPOTHESIS_EXAMPLES', '1500')) // 10
QueueModel.TestCase.settings = settings(
    parent=settings.get_profile('unit'), max_examples=MODEL_EXAMPLES, stateful_step_count=40
)
TestQueueModel = QueueModel.TestCase
