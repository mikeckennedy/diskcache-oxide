# Example apps

Two small, complete web apps in the repository put diskcache-oxide to work the way these docs recommend, each with tests that start real worker processes and check the cache is shared across them. Each page below walks through one app's code.

**[The FastAPI example](fastapi.md).** A podcast archive on diskcache-oxide's own API: cache-aside fragments and memoized loads in a durable registry, sessions and a work queue in plain caches, tagged invalidation, rate limits that hold across workers, an hourly job one worker claims, a background worker, and Docker.

**[The Django example](django.md).** A Django project on the [DjangoCache](../reference/DjangoCache.md#diskcache_oxide.DjangoCache) backend: pages and sessions on two aliases, the per-view cache, a template fragment, cache sessions, `get_or_set`, a counter shared across workers, and gunicorn with four workers forked from one master.
