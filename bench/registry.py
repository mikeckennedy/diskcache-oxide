"""A registered dataclass versus the by-hand conversion it replaces (plan 16, not a gate).

    python -m bench.registry [--rounds 20000]

One thread, a 20-field dataclass with a nested one, msgpack. The by-hand path is what an app writes
today: `dataclasses.asdict()` into `set()`, and `cls(**d)` after `get()`. The registered path is
`register_type(Episode, name=...)` and the object straight in and out. Numbers are per operation.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import tempfile
import time
from pathlib import Path

import diskcache_oxide
from diskcache_oxide import Cache


@dataclasses.dataclass
class Guest:
    id: int
    name: str
    url: str = ''


@dataclasses.dataclass
class Episode:
    id: int
    show_number: int
    title: str
    slug: str
    published: dt.datetime
    duration_seconds: int
    mp3_url: str
    mp3_bytes: int
    youtube_id: str
    description: str
    summary: str
    tags: list[str]
    sponsors: list[str]
    guest: Guest
    transcript_available: bool
    download_count: int
    rating: float
    season: int
    is_live: bool
    notes: str


def sample() -> Episode:
    return Episode(
        id=491,
        show_number=491,
        title='Rust and Python, together',
        slug='rust-and-python-together',
        published=dt.datetime(2026, 11, 6, 9, 30, tzinfo=dt.UTC),
        duration_seconds=3721,
        mp3_url='https://example.invalid/491.mp3',
        mp3_bytes=61_234_567,
        youtube_id='dQw4w9WgXcQ',
        description='A long description of the episode. ' * 8,
        summary='A summary. ' * 4,
        tags=['rust', 'python', 'performance'],
        sponsors=['Sentry', 'Posit'],
        guest=Guest(7, 'Sam', 'https://example.invalid/sam'),
        transcript_available=True,
        download_count=18_000,
        rating=4.8,
        season=12,
        is_live=False,
        notes='Show notes. ' * 6,
    )


def from_dict(d: dict) -> Episode:
    d = dict(d)
    d['guest'] = Guest(**d['guest'])
    return Episode(**d)


def timed(label: str, rounds: int, op) -> float:
    for _ in range(rounds // 10):
        op()
    start = time.perf_counter()
    for _ in range(rounds):
        op()
    per = (time.perf_counter() - start) / rounds
    print(f'{label:<42} {per * 1e6:8.2f} µs')
    return per


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--rounds', type=int, default=20_000)
    args = parser.parse_args()
    episode = sample()
    with tempfile.TemporaryDirectory() as tmp:
        by_hand = Cache(Path(tmp) / 'by-hand')
        registered = Cache(Path(tmp) / 'registered')
        diskcache_oxide.register_type(Guest, name='bench.Guest')
        diskcache_oxide.register_type(Episode, name='bench.Episode')

        print(f'{args.rounds} rounds, one thread, msgpack, 20-field dataclass with a nested one\n')
        timed('asdict() only', args.rounds, lambda: dataclasses.asdict(episode))
        timed('set(asdict(e))', args.rounds, lambda: by_hand.set('e', dataclasses.asdict(episode)))
        timed('set(e), registered', args.rounds, lambda: registered.set('e', episode))
        print()
        timed('get() -> dict only', args.rounds, lambda: by_hand.get('e'))
        timed('from_dict(get())', args.rounds, lambda: from_dict(by_hand.get('e')))
        timed('get(), registered', args.rounds, lambda: registered.get('e'))
        by_hand.close()
        registered.close()


if __name__ == '__main__':
    main()
