"""A stand-in for a real database: episodes in a JSON file, deliberately slow to read.

Every worker process shares the file, so an edit in one is seen by all. Writes replace the file
atomically. It's here so the cache has something slow to save you from; nothing about it is a
recommendation.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Episode:
    number: int
    title: str
    guest: str
    published: str  # ISO date
    minutes: int
    tags: tuple[str, ...]
    description: str


SEED = [
    Episode(
        512,
        'Free-threaded Python, one year in',
        'Mara Lindqvist',
        '2026-11-06',
        64,
        ('threads', 'cpython'),
        'A year after 3.14t shipped, which libraries kept up, which ones still re-enable the GIL on import, '
        'and what changed for apps that run many threads in one process.',
    ),
    Episode(
        511,
        'SQLite as an application file format',
        'Dev Okafor',
        '2026-10-30',
        58,
        ('sqlite', 'storage'),
        'Why so many apps ship a SQLite file instead of a folder of JSON, and what the write-ahead log buys you.',
    ),
    Episode(
        510,
        'Shipping Rust extensions with maturin',
        'Lena Brandt',
        '2026-10-23',
        71,
        ('rust', 'packaging'),
        'Wheels for every platform from one repository, and the free-threaded builds that came along with them.',
    ),
    Episode(
        509,
        'Caching that survives a deploy',
        'Tomás Ruiz',
        '2026-10-16',
        49,
        ('caching', 'deploys'),
        'Warm caches across restarts, rolling deploys and the occasional on-disk format change.',
    ),
    Episode(
        508,
        'Async web apps without the footguns',
        'Priya Nair',
        '2026-10-09',
        55,
        ('asyncio', 'web'),
        'What belongs on the event loop, what goes to a thread, and how to tell which is which.',
    ),
    Episode(
        507,
        'What pickle gets wrong',
        'Sam Whitfield',
        '2026-10-02',
        47,
        ('security', 'serialization'),
        'Pickle runs code on load. Here is what to store instead, and how to keep your own types.',
    ),
    Episode(
        506,
        'Gunicorn, uvicorn and the pre-fork model',
        'Inês Duarte',
        '2026-09-25',
        62,
        ('servers', 'deploys'),
        'Forks, spawns and worker recycling, and what each one means for state held in a process.',
    ),
    Episode(
        505,
        'Rate limits that hold across workers',
        'Kofi Mensah',
        '2026-09-18',
        44,
        ('web', 'limits'),
        'A shared counter per client per window, with no Redis, and the boundary burst it allows.',
    ),
    Episode(
        504,
        'Logging that reaches you at 3 a.m.',
        'Hana Sato',
        '2026-09-11',
        52,
        ('ops', 'logging'),
        'Handlers, dictConfig and the library logger that a default config quietly silences.',
    ),
    Episode(
        503,
        'Docker volumes, owners and UIDs',
        'Ruth Abara',
        '2026-09-04',
        46,
        ('docker', 'deploys'),
        'Named volumes, bind mounts and the PermissionError that comes from a UID that changed.',
    ),
]


class Store:
    def __init__(self, directory: Path, delay: float) -> None:
        self.path = directory / 'episodes.json'
        self.delay = delay
        directory.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({e.number: e for e in SEED}, exclusive=True)

    def all(self) -> list[Episode]:
        time.sleep(self.delay)  # the slow part a cache saves you from
        return sorted(self._read().values(), key=lambda e: e.number, reverse=True)

    def get(self, number: int) -> Episode | None:
        time.sleep(self.delay)
        return self._read().get(number)

    def update(self, number: int, *, title: str, description: str, tags: tuple[str, ...], minutes: int) -> Episode:
        episodes = self._read()
        episode = replace(episodes[number], title=title, description=description, tags=tags, minutes=minutes)
        episodes[number] = episode
        self._write(episodes)
        return episode

    def _read(self) -> dict[int, Episode]:
        episodes = {}
        for row in json.loads(self.path.read_text()):
            episode = Episode(
                number=int(row['number']),
                title=str(row['title']),
                guest=str(row['guest']),
                published=str(row['published']),
                minutes=int(row['minutes']),
                tags=tuple(str(t) for t in row['tags']),
                description=str(row['description']),
            )
            episodes[episode.number] = episode
        return episodes

    def _write(self, episodes: dict[int, Episode], *, exclusive: bool = False) -> None:
        data = json.dumps([asdict(e) for e in episodes.values()], indent=1)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix='.tmp')
        with os.fdopen(fd, 'w') as f:
            f.write(data)
        if exclusive:
            try:
                os.link(tmp, self.path)  # first creator wins; the others keep its file
            except FileExistsError:
                pass
            os.unlink(tmp)
        else:
            os.replace(tmp, self.path)
