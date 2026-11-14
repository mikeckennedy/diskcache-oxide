# Docker and Compose

A cache is a directory, so in a container the only real question is where that directory lives. It needs to outlive the container (or a restart starts cold and logs everyone out), it needs to be shared by every container that should see the same cache (the web app and its background worker, say), and it needs to belong to the user the app runs as. One named volume does all three.

The [example app](https://github.com/mikeckennedy/diskcache-oxide/tree/main/examples/webapp) ships the files below, tested.


# The Compose file

One named volume, mounted at the same path in every container that shares the caches. The layout inside it is the [recommended setup](web-workers.md#the-recommended-setup): `cache/` for the durable registry, `state/` for sessions, claims and the job queue.

``` yaml
services:
  web:
    build: .
    command: gunicorn -c gunicorn.conf.py
    ports: ["127.0.0.1:8000:8000"]
    volumes: [appdata:/var/lib/my-app]
    restart: unless-stopped
    healthcheck:
      # Readiness, not liveness: a cache outage is what durable=True absorbs.
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/cache')"]
      interval: 10s
  worker:
    build: .
    command: python -m app.worker
    volumes: [appdata:/var/lib/my-app]
    restart: unless-stopped

volumes:
  appdata:
```

`gunicorn.conf.py` names the app factory (`wsgi_app = 'app.main:create_app()'`), the uvicorn worker class and the worker count, so each web worker builds its app, and opens its caches, itself.

Every container on the host that mounts `appdata` shares the same caches, with the same guarantees as processes on one machine: one writer at a time per cache, [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) claims and locks that hold across all of them, a queue that hands each job to one worker.


# The Dockerfile

``` dockerfile
FROM python:3.14-slim

# A fixed, non-root user and group. The UID must never change between image versions: cache directories
# are created owner-only (0700), so a container running as another UID can't open them.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --create-home app

# Create the mount point and give it to that user before switching to it. A new, empty named
# volume copies this directory's ownership the first time it's mounted.
RUN mkdir -p /var/lib/my-app && chown app:app /var/lib/my-app

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gunicorn.conf.py .
COPY app/ app/

USER app
ENV EXAMPLE_ROOT=/var/lib/my-app
CMD ["gunicorn", "-c", "gunicorn.conf.py"]
```

Use a Debian- or Ubuntu-based image such as `python:3.14-slim`. diskcache-oxide publishes wheels for glibc Linux, not for musl, so on Alpine pip would build it from source, which needs a Rust toolchain in the image.


# Who owns the directory

Almost every Docker problem with a cache is this one: `PermissionError: [Errno 13] Permission denied`. diskcache-oxide creates each cache directory `0700`, readable and writable only by the user that created it. That's deliberate (anyone who can write to a cache can change what your app reads), and it means:

- **Every container that shares the volume runs as the same UID.** Two images with different users, or one image whose UID changed between versions, can't open each other's caches.
- **The volume's root must be writable by that user.** A named volume takes the image's ownership of the mount point only when it's mounted empty for the first time, which is why the Dockerfile `chown`s it before `USER`. A volume created earlier by a root container stays root-owned: fix it once with `docker compose run --rm -u root web chown -R 10001:10001 /var/lib/my-app`.
- **Don't run one-off commands as root** against the volume (`docker exec -u root`, a root cron job in the container). A cache directory a root process creates is root's `0700`, and the app can't open it. Run admin scripts as the app's user.


## Mapping a host directory instead

A bind mount (`./data:/var/lib/my-app`) works too, and makes the files easy to see from the host. It doesn't copy ownership from the image, so create the host directory first and give it to the container's UID: `mkdir -p data && sudo chown 10001:10001 data`. If the host path doesn't exist, the Docker daemon creates it, owned by root, and the app gets `PermissionError`.

Keep the cache inside the containers: don't also open it from the host while containers use it. On Linux a bind mount is the host's own filesystem, but on macOS and Windows, Docker Desktop shares host folders through a virtualized filesystem, and we haven't tested SQLite's locking across that boundary. The named volume stays inside Docker's VM, where it's an ordinary local disk.


# Kubernetes

The same rules, with Kubernetes' names for the volumes:

- **`emptyDir`** is per pod and goes with the pod: fine for a pure cache that may start cold, not for sessions or queues.
- **A `hostPath` or a local PersistentVolume** is per node, shared by every pod scheduled there. They all need the same UID.
- **A StatefulSet with a ReadWriteOnce claim** gives each pod its own volume that survives restarts.
- **Never ReadWriteMany.** Those volumes are network filesystems (NFS, EFS, Azure Files), where SQLite's locks don't hold. diskcache-oxide can't detect that at open; the cache just corrupts. A cache is a per-machine thing: pods on different nodes don't share one, so keep shared state (sessions across nodes, say) in a database.
- **Ownership:** a PersistentVolume doesn't copy the image's ownership. The simplest fix is an initContainer that chowns only the mount's root to the app's UID. `securityContext.fsGroup` works too, but by default Kubernetes then makes everything on the volume group-writable on every mount, which undoes the caches' `0700`: if you use it, set `fsGroupChangePolicy: OnRootMismatch` and give the group to nothing else.
- **Probes:** point the readiness probe at a cache health check, never the liveness probe. A cache outage is what `durable=True` absorbs, and restarting the pod for it only adds a cold start. See [Monitoring](monitoring.md).


# How much disk

A volume needs room for:

- the sum of the caches' [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit)s, which eviction holds them near;
- one write-ahead log per cache, which checkpoints keep near `wal_size_limit` (64 MiB by default): a target, not a hard bound (see [Storage and WAL](../user-guide/storage-and-wal.md));
- the state caches, which with `eviction_policy='none'` aren't bounded by size: they hold what you put in until it expires, so give their entries an [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) and watch [volume()](../reference/Cache.volume.md#diskcache_oxide.Cache.volume).

A read-only root filesystem (`read_only: true`) works as long as the volume is writable, but give the container a writable `/tmp` as well (`tmpfs: [/tmp]`): SQLite writes temporary files there for large sorts and for the `VACUUM` that `check(fix=True)` runs, and `SQLITE_TMPDIR` can point it somewhere else.


# Backups

Caches need none: they can always start cold. Sessions and queues are state, and a cache's state is more than its database: values of 64 KiB or more live in value files beside it. Stop every container that writes, then copy each cache's whole directory (the database, its `-wal` file and the value folders), or the whole volume. A copy taken while the app runs can catch the database, its write-ahead log and the value files out of step.


# Deploys and format changes

Rolling and blue/green deploys that share the volume run old and new containers side by side for a while. A release that keeps the on-disk format needs nothing more: `docker compose up -d` and the caches stay warm.

Across a release that changes the format, old and new containers each use their own cache in the same directories (see [Upgrading across a format change](upgrading.md)). For a cache that's harmless: the new one starts cold. State is different, because the new release can't read the old release's queue or sessions, so drain the queue first:

``` bash
docker compose stop web                              # nothing new gets queued
docker compose exec worker python -m app.jobs        # repeat until it prints "0 queued, 0 running"
docker compose up -d                                 # the new release
docker compose run --rm --no-deps web python -m app.deploy_cleanup
```

Everyone signs in again after a format change, since the new release starts with empty sessions; [State that isn't really cache](upgrading.md#state-that-isnt-really-cache) has the alternatives. The last line removes the old caches. Run it after `up -d` has replaced every old container, never at worker startup: [remove_old_formats()](../reference/Cache.remove_old_formats.md#diskcache_oxide.Cache.remove_old_formats) doesn't check whether an old worker is still running. The example's `deploy_cleanup.py` calls it on the registry and on each state cache.
