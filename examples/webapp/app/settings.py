"""Settings, from the environment so the same code runs in Docker, in tests and on a laptop.

diskcache-oxide itself reads no environment variables; this app does, for its own settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # One directory the app owns: cache/ for the durable registry, state/ for sessions, claims and
    # the job queue, db/ for this demo's stand-in database. See the "recommended setup" recipe.
    root: Path = Path('/var/lib/my-app')
    # How long the stand-in database takes to answer, so a cache hit is obvious.
    load_delay: float = 0.4
    # The hourly job claim and the rate-limit windows, in seconds. Tests shorten them.
    claim_period: int = 3600
    rate_window: int = 60
    search_limit: int = 20
    signin_limit: int = 5
    # /admin/caches answers only on loopback unless this is set.
    admin_anywhere: bool = False
    # Mark the session cookie Secure (sent over HTTPS only). Turn it on wherever the app is served over
    # HTTPS; it's off by default only so the demo works on http://127.0.0.1.
    secure_cookies: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            root=Path(env.get('EXAMPLE_ROOT', str(cls.root))),
            load_delay=float(env.get('EXAMPLE_LOAD_DELAY', cls.load_delay)),
            claim_period=int(env.get('EXAMPLE_CLAIM_PERIOD', cls.claim_period)),
            rate_window=int(env.get('EXAMPLE_RATE_WINDOW', cls.rate_window)),
            search_limit=int(env.get('EXAMPLE_SEARCH_LIMIT', cls.search_limit)),
            signin_limit=int(env.get('EXAMPLE_SIGNIN_LIMIT', cls.signin_limit)),
            admin_anywhere=env.get('EXAMPLE_ADMIN_ANYWHERE', '') == '1',
            secure_cookies=env.get('EXAMPLE_SECURE_COOKIES', '') == '1',
        )
