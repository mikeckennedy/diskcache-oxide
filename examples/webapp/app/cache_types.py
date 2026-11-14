"""Every type this app stores in a cache, registered once.

Import this module from every entry point (the web app, the worker, scripts) before touching a
cache. A process that hasn't registered a type can't read values of it: on a durable cache they're
misses with a warning, so the slow path runs every time. tests/test_types_logging_cleanup.py checks that the web
app and the worker register the same set.
"""

from __future__ import annotations

from diskcache_oxide import register_type, registered_types

from app.store import Episode

# A dataclass needs only a name: its fields are stored, and it's rebuilt with Episode(**fields).
# The name is the tag on disk, so it must never change once values are stored.
if 'example.Episode' not in registered_types():
    register_type(Episode, name='example.Episode')
