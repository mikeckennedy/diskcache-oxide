# Changelog

All notable changes to diskcache-oxide are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Nothing has been released yet: the `0.0.1` on PyPI is a name placeholder with no code, and `0.1.0.dev1` was yanked.

## [Unreleased]

### Documentation

- A hand-written agent skill (`skills/diskcache-oxide/SKILL.md`) replaces the generated one, which only listed API names. It carries the rules an agent needs: `str` keys and exact built-in values, what `NullCache.add()` and `durable=True` do to a job claim, when a `FanoutCache` helps and when it doesn't, what `transact()` joins and refuses, and a decision table for the modes and the exceptions. It is published at `skill.md` and under `.well-known/`.
- A new guide page, Errors and what to do about them, covers every exception the library raises, what it means and the fix, and the log lines from `durable=True`, `on_decode_error='miss'` and `Caches`. It is linked from the index, the README, Getting started, Switching from diskcache and Caches.
- The status is current everywhere: `FanoutCache` and `transact()` are no longer listed as planned, the version is `0.1.0.dev2`, the remaining gates before production and `0.1.0` are named, and the install instructions say why `pip install diskcache-oxide` isn't the way in yet. The index's "Where to next" names every guide page, and each README section links to the page that expands it.
- Docstrings gain Examples sections (`Cache`, `FanoutCache`, `NullCache`, `Caches`, the protocols and the main methods), and the module docstring names the entry points, the exception hierarchy and the logger.
- `llms-full.txt` now shows the full docstrings and typed signatures from `_core.pyi` for the compiled classes, instead of the one-line docstrings and bare signatures of the extension. The docs build fails when `llms.txt`, `llms-full.txt` or any published copy of the skill is missing or isn't the hand-written file.

[Unreleased]: https://github.com/mikeckennedy/diskcache-oxide/commits/main
