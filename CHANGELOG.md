# Changelog

All notable changes to diskcache-oxide are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Nothing has been released yet: the `0.0.1` on PyPI is a name placeholder with no code, and `0.1.0.dev1` was yanked.

## [Unreleased]

### Added

- A type registry (D-041, plan 16): `register_type(cls, name=..., encode=..., decode=...)` tells the codec how to store a type it doesn't know, without pickle. A dataclass needs only a name: its fields are stored and it is rebuilt with `cls(**fields)`, so defaults and `__post_init__` run. Registrations are process-wide (`diskcache_oxide.registry`, a `TypeRegistry`), or per cache with `Cache(types=TypeRegistry())`, consulted first. A registered value is stored as a tagged map in both serializers; a reader without the registration gets `ForeignValueError` and the row stays, so nothing is ever imported by name. Once the encoder has used a registration for any part of a value, that value is never pickled. `unregister_type()` and `registered_types()` complete the surface; `NullCache` and `Caches.register` accept `types=`.

### Changed

- Large values' files now live one folder deep (`aa/<name>.val`, at most 256 folders per cache) instead of two (65,536), and empty folders are removed: by the process's last close of a cache (those untouched for 30 seconds, for at most 100 ms) and by `clear()`. Before, no folder was ever removed, so a long-lived cache filled all 65,536 with empty folders, about 257 MiB on ext4 and 2 GiB for a default `FanoutCache` (D-042, plan 17; a `FanoutCache`'s shards don't sweep yet, so each keeps at most 256). This is a new on-disk format, version 2: **delete existing caches when upgrading**, with every worker stopped. Opening a version-1 cache raises `FormatMismatchError`, which a `Caches` registry reports as a `NullCache`.
- The sweep that runs at a cache's last close and after `clear()` also removes a value folder's temp files that no writer has touched for an hour: the file a process killed mid-write leaves behind, which nothing removed before (plan 19). A young temp file and any `.val` are left alone.
- A large write after a cache's folder was deleted under a running process now raises `OSError` instead of re-creating the folder tree (D-043).
- A stored file name that isn't one oxide writes is no longer turned into a path: the value reads as a miss and its row is removed (D-013), so a damaged row can't make a read or delete reach outside the cache folder.

- A `Caches` registry no longer turns a sharded cache whose folder has another shard count than `register(..., shards=n)` into a `NullCache`. It opens the folder with the count on disk, logs a `WARNING` saying how to apply `n` (delete the folder with every worker stopped), and logs one `ERROR` per process with a constant message so an error tracker groups the reports. `n` applies only to a new folder. A `size_limit` or `max_connections` that fits `n` but is too small for the folder's count is still a reported `NullCache`. A direct `FanoutCache(path, shards=n)` still raises `LayoutMismatchError` (plan 15).

- Inside `FanoutCache.transact(key=...)`, calls over every shard now raise `ValueError` on the thread that holds the transaction, as calls for other shards' keys already did: `len()`, `volume()`, `iter()`, `iterkeys()` (and `next()` on an iterator made before the block), `clear()`, `expire()`, `cull()` and `checkpoint()`. They used to run inside the transaction on its shard and outside it on the others. Other threads and a plain `Cache`'s transaction are unaffected (D-040).

### Documentation

- A hand-written agent skill (`skills/diskcache-oxide/SKILL.md`) replaces the generated one, which only listed API names. It carries the rules an agent needs: `str` keys and exact built-in values, what `NullCache.add()` and `durable=True` do to a job claim, when a `FanoutCache` helps and when it doesn't, what `transact()` joins and refuses, and a decision table for the modes and the exceptions. It is published at `skill.md` and under `.well-known/`.
- A new guide page, Errors and what to do about them, covers every exception the library raises, what it means and the fix, and the log lines from `durable=True`, `on_decode_error='miss'` and `Caches`. It is linked from the index, the README, Getting started, Switching from diskcache and Caches.
- The status is current everywhere: `FanoutCache` and `transact()` are no longer listed as planned, the version is `0.1.0.dev2`, the remaining gates before production and `0.1.0` are named, and the install instructions say why `pip install diskcache-oxide` isn't the way in yet. The index's "Where to next" names every guide page, and each README section links to the page that expands it.
- Docstrings gain Examples sections (`Cache`, `FanoutCache`, `NullCache`, `Caches`, the protocols and the main methods), and the module docstring names the entry points, the exception hierarchy and the logger.
- The landing page, README and the opening of every guide page now tell the story instead of listing the facts: the afternoon every app reaches for a cache, the child process that waits a minute for a lock a ghost holds, the one checkout lane that `FanoutCache` turns into eight, the 3 a.m. full disk that `durable=True` turns into a miss. Every number and behaviour claim is unchanged; the reference tables, the agent skill and the API docstrings stay plain.
- `llms-full.txt` now shows the full docstrings and typed signatures from `_core.pyi` for the compiled classes, instead of the one-line docstrings and bare signatures of the extension. The docs build fails when `llms.txt`, `llms-full.txt` or any published copy of the skill is missing or isn't the hand-written file.

### Fixed

- The lock-free stack that holds work deferred behind a fork (handle releases, transaction ends) stored a raw pointer that, after a freed node's address was reused by a new one, could carry the freed node's provenance: harmless on the machine, undefined behavior in Rust's model, found by Miri in the sanitizer pass (plan 14, lane A). It now stores addresses and rebuilds pointers with exposed provenance. No observable behavior changes.
- A closed handle used again (it rejoins its store, as diskcache reopens lazily) no longer leaves one extra file descriptor open on the database while another handle keeps the store live. It used to open a pin on the database file and close it again, and SQLite's unix VFS defers that close while any connection in the process holds a lock on the file, which every pooled connection does in WAL mode, so the descriptor stayed until the pool emptied (the next fork, or the last close). The soak found it on a `FanoutCache` when a `set` in flight on one thread met a `close()` on another (a new handle had just been opened on the same root): one shard went to 49 file descriptors against D-037's budget of 48 (D-017).

- A `FanoutCache` root deleted while its shards were opening is no longer re-created by a shard open, without a manifest and with shards of the old count, which left the cache unopenable until the folder was deleted again. The open now fails with `OSError` (a reported `NullCache` in a registry), and the next open creates the root properly.
- `ruff format --check .`, which `scripts/check.sh` runs, failed on the generated Markdown companions under `site/`, whose code blocks ruff 0.17 reformats. The built site is now excluded in `ruff.toml`, as its comment already intended.

[Unreleased]: https://github.com/mikeckennedy/diskcache-oxide/commits/main
