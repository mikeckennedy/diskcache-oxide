"""A deliberately slow stand-in for a database, so the cache has something to save."""

import os
import time
from collections import Counter

LOAD_DELAY = float(os.environ.get('EXAMPLE_LOAD_DELAY', '0.2'))

EPISODES = {
    1: 'Rust under Python',
    2: 'Caching across processes',
    3: 'Free-threaded Python in production',
}

# How many times each thing was loaded in this process: the tests read it to see the cache work.
loads: Counter[str] = Counter()


def load_episode(number: int) -> dict[str, object]:
    loads[f'episode:{number}'] += 1
    time.sleep(LOAD_DELAY)
    return {'number': number, 'title': EPISODES[number]}


def episode_list() -> list[dict[str, object]]:
    loads['episode_list'] += 1
    time.sleep(LOAD_DELAY)
    return [{'number': n, 'title': title} for n, title in EPISODES.items()]


def compute_stats() -> dict[str, int]:
    loads['stats'] += 1
    time.sleep(LOAD_DELAY)
    return {'episodes': len(EPISODES), 'characters': sum(len(title) for title in EPISODES.values())}
