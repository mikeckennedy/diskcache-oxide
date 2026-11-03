"""A value too deep for the thread's C stack, in a process of its own, since the failure this guards
against kills the interpreter. Run from test_stack.py.

    python -m tests.unit.stack_scenarios <scenario> <directory>

Exits 0 if every check passed; otherwise prints the failure to stderr and exits 1. A fatal signal or a
`Py_FatalError` is the bug itself, and shows up as any other exit code.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from diskcache_oxide import Cache, _core

SMALL_STACK = max(64 * 1024, os.sysconf('SC_THREAD_STACK_MIN'))  # glibc on aarch64 refuses under 128 KiB
WATCHDOG = 40.0
MSGPACK_MODE = 3
SERIALIZERS = ('msgpack', 'json')


def nested(wrap: Callable[[Any], Any], depth: int) -> Any:
    value = None
    for _ in range(depth):
        value = wrap(value)
    return value


# The deepest each shape gets within the encoder's limit, where a tuple counts twice. The last ends in
# a hash: building a set hashes the whole tuple chain, with the decoder's frames already gone.
VALUES = {
    'list': nested(lambda v: [v], 120),
    'dict': nested(lambda v: {'k': v}, 120),
    'dollar-dict': nested(lambda v: {'$k': v}, 60),
    'tuple': nested(lambda v: (v,), 60),
    'set-of-tuples': {nested(lambda v: (v,), 59)},
}


def on_a_small_thread(work: Callable[[], None]) -> None:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            work()
        except BaseException as exc:
            errors.append(exc)

    threading.stack_size(SMALL_STACK)
    thread = threading.Thread(target=run)
    thread.start()
    threading.stack_size(0)
    thread.join()
    if errors:
        raise errors[0]


def raises_recursion_error(call: Callable[[], object]) -> bool:
    try:
        call()
    except RecursionError:
        return True
    return False


def the_deepest_row_a_reader_accepts_is_a_recursion_error(directory: Path) -> None:
    """Twice the encoder's limit, the deepest the msgpack reader takes. It's a good value, so it mustn't
    read as corrupt. 240 levels can't fit in what a small thread has left past CPython's margins."""
    blob = b'\x91' * 240 + b'\xc0'
    assert _core._decode(MSGPACK_MODE, blob) == nested(lambda v: [v], 240)

    def work() -> None:
        assert raises_recursion_error(lambda: _core._decode(MSGPACK_MODE, blob)), 'it fit after all'

    on_a_small_thread(work)


def reads_on_a_small_stack_keep_the_row(directory: Path) -> None:
    for serializer in SERIALIZERS:
        for name, value in VALUES.items():
            where = directory / serializer / name
            plain = Cache(where, serializer=serializer)
            durable = Cache(where, serializer=serializer, durable=True)
            assert plain.set('k', value) is True
            results: dict[str, Any] = {}

            def work() -> None:
                results['durable get'] = durable.get('k', 'default')
                results['durable pop'] = durable.pop('k', 'default')
                if results['durable pop'] == 'default':
                    assert not raises_recursion_error(lambda: durable.get('k', 'default'))
                    try:
                        results['plain get'] = plain.get('k', 'default')
                    except RecursionError:
                        results['plain get'] = 'RecursionError'

            on_a_small_thread(work)
            print(
                serializer,
                name,
                sorted(k for k, v in results.items() if v in ('default', 'RecursionError')),
            )
            # Never compared on the small thread: `==` on a deep value needs the stack too.
            assert results['durable get'] in ('default', value), (serializer, name)
            if results['durable pop'] == 'default':
                assert plain.get('k') == value, (
                    serializer,
                    name,
                    'a pop that failed removed the row',
                )
                assert results['plain get'] in ('RecursionError', value), (
                    serializer,
                    name,
                )
            else:
                assert results['durable pop'] == value, (serializer, name)
                assert plain.get('k', 'gone') == 'gone', (serializer, name)
            plain.close()
            durable.close()


def writes_on_a_small_stack_store_or_raise(directory: Path) -> None:
    """The constructor loaded the codec's types, so this thread needn't import anything."""
    for serializer in SERIALIZERS:
        for name, value in VALUES.items():
            for is_durable in (False, True):
                where = directory / serializer / name / str(is_durable)
                cache = Cache(where, serializer=serializer, durable=is_durable)
                results: dict[str, Any] = {}

                def work() -> None:
                    results['small'] = cache.set('small', 1)
                    try:
                        results['deep'] = cache.set('deep', value)
                    except RecursionError:
                        results['deep'] = 'RecursionError'

                on_a_small_thread(work)
                assert results['small'] is True, (serializer, name)
                assert results['deep'] in (True, 'RecursionError'), (
                    serializer,
                    name,
                    results['deep'],
                )
                if results['deep'] is True:
                    assert cache.get('deep') == value, (serializer, name)
                else:
                    assert cache.get('deep', 'missing') == 'missing', (serializer, name)
                cache.close()


def every_stack_position_survives(directory: Path) -> None:
    """Runs each codec path at every depth down to where the interpreter itself refuses to go further:
    wherever the stack runs out, it's an ordinary exception, and it never gets past the guard."""
    cases = []
    for serializer in SERIALIZERS:
        cache = Cache(directory / serializer, serializer=serializer, durable=True)
        for name, value in VALUES.items():
            mode, payload = _core._encode(value, serializer=serializer)
            cases.append((serializer, name, value, mode, payload, cache))
            assert cache.set(name, value) is True

    levels = [0]
    outcomes = {'value': 0, 'RecursionError': 0, 'miss': 0}

    def attempt(call: Callable[[], object]) -> None:
        try:
            got = call()
        except RecursionError:
            outcomes['RecursionError'] += 1
        else:
            outcomes['miss' if got == 'default' else 'value'] += 1

    def down(level: int) -> None:
        levels[0] = level
        for serializer, name, value, mode, payload, cache in cases:
            attempt(lambda: _core._encode(value, serializer=serializer))
            attempt(lambda: _core._decode(mode, payload))
            attempt(lambda: cache.get(name, 'default'))
        # Through a C call, so each level uses C stack: Python-to-Python calls don't.
        list(map(down, [level + 1]))

    def work() -> None:
        try:
            down(0)
        except RecursionError:
            pass

    threading.stack_size(512 * 1024)
    thread = threading.Thread(target=work)
    thread.start()
    threading.stack_size(0)
    thread.join()
    print('levels', levels[0], outcomes)
    assert levels[0] > 50, 'barely went down the stack'
    assert outcomes['RecursionError'] > 0, "the walk never reached the guard, so it didn't test it"
    assert outcomes['value'] > 0
    for serializer, name, value, _mode, _payload, cache in cases:
        assert cache.get(name) == value, (
            serializer,
            name,
            'a failed read removed the row',
        )
    for cache in {id(case[5]): case[5] for case in cases}.values():
        cache.close()


SCENARIOS: dict[str, Callable[[Path], None]] = {
    'the-deepest-row': the_deepest_row_a_reader_accepts_is_a_recursion_error,
    'reads': reads_on_a_small_stack_keep_the_row,
    'writes': writes_on_a_small_stack_store_or_raise,
    'every-position': every_stack_position_survives,
}


if __name__ == '__main__':
    faulthandler.enable()
    faulthandler.dump_traceback_later(WATCHDOG, exit=True)
    try:
        SCENARIOS[sys.argv[1]](Path(sys.argv[2]))
    except BaseException:
        traceback.print_exc()
        sys.exit(1)
