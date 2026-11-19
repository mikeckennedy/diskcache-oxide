"""Every public name has a page in the API reference, so a new export can't ship undocumented.

The reference is the `reference:` section of great-docs.yml, which lists each documented name on its own
`- Name` line under a section's `contents:`. PyYAML isn't a test dependency, so the names are read with a
pattern rather than a parser.
"""

from __future__ import annotations

import re
from pathlib import Path

import diskcache_oxide

CONFIG = Path(__file__).resolve().parents[2] / 'great-docs.yml'
# Exported on purpose but not a reference page of its own.
NOT_IN_REFERENCE = {'__version__'}
# Public names the package loads on first use (a module `__getattr__`), kept out of `__all__` so
# `import *` works without their optional dependency. The reference may list them.
LAZY = {'DjangoCache'}


def reference_names() -> set[str]:
    text = CONFIG.read_text(encoding='utf-8')
    body = text.index('\n', text.index('\nreference:') + 1) + 1
    end = re.search(r'^\S', text[body:], re.MULTILINE)  # the next top-level key, if any
    section = text[body : body + end.start()] if end else text[body:]
    return set(re.findall(r'^ +- (\w+)\s*$', section, re.MULTILINE))


def test_reference_section_found() -> None:
    assert len(reference_names()) > 20


def test_every_export_is_in_the_reference() -> None:
    missing = sorted(set(diskcache_oxide.__all__) - reference_names() - NOT_IN_REFERENCE)
    assert not missing, f'exported but not in the reference of great-docs.yml: {missing}'


def test_reference_lists_only_exports() -> None:
    unknown = sorted(reference_names() - set(diskcache_oxide.__all__) - LAZY)
    assert not unknown, f'in the reference of great-docs.yml but not exported: {unknown}'


def test_lazy_names_are_not_exports() -> None:
    assert not LAZY & set(diskcache_oxide.__all__)


def test_lazy_names_are_in_the_reference() -> None:
    missing = sorted(LAZY - reference_names())
    assert not missing, f'loaded lazily but not in the reference of great-docs.yml: {missing}'
