"""After a deploy across an on-disk format change: `python -m app.deploy_cleanup`.

Run it once every container from the previous release has stopped (in Compose:
`docker compose run --rm --no-deps web python -m app.deploy_cleanup`, after `up -d`), never at
worker startup. remove_old_formats() doesn't check whether an older worker still uses the files.
With nothing old on disk it does nothing, so running it after every deploy is harmless.

It removes the old release's job queue and sessions with everything else: the new release can't
read them. Drain the queue before the deploy (stop the web containers, then wait for
`python -m app.jobs` in the old worker container to print "0 queued, 0 running"), and expect
everyone to sign in again. The Docker recipe has the steps.
"""

from __future__ import annotations

import logging
import logging.config

from app.caching import open_caches
from app.logging_config import LOGGING
from app.settings import Settings

log = logging.getLogger('app')


def main(settings: Settings | None = None) -> list[str]:
    settings = settings or Settings.from_env()
    caches = open_caches(settings.root)
    removed: list[str] = []
    try:
        # The registry covers its own caches; state caches are opened directly, so each one is cleaned too.
        for name, paths in caches.registry.remove_old_formats().items():
            if paths is None:
                log.error('could not clean cache %s (see the error above)', name)
            removed += paths or []
        for cache in caches.state().values():
            removed += cache.remove_old_formats()
    finally:
        caches.close()
    for path in removed:
        log.info('removed %s', path)
    log.info('removed %d old-format file(s) or directories', len(removed))
    return removed


if __name__ == '__main__':
    logging.config.dictConfig(LOGGING)
    main()
