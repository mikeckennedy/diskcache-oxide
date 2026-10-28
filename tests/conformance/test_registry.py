"""Keep tests/DIVERGENCES.md and the tests' divergence references in step.

Unknown refs are rejected at collection time by conftest.py. This checks the other direction:
every test-visible divergence is referenced by at least one test. It also checks
KNOWN_COMMUNITY_DIFFERENCES against the tests and the "Community fork differences" section.
"""

import importlib
import re
from pathlib import Path

from tests.conformance.backends import KNOWN_COMMUNITY_DIFFERENCES
from tests.conformance.conftest import DIVERGENCES_FILE

TESTS_DIR = Path(__file__).parent
_ENTRY = re.compile(r'^##\s+(D-\d{3})\b.*?(?=^##\s|\Z)', re.MULTILINE | re.DOTALL)
_REF_IN_SOURCE = re.compile(r"""['"](D-\d{3})['"]""")
_COMMUNITY_SECTION = re.compile(r'^# Community fork differences\n(.*?)(?=^# |\Z)', re.MULTILINE | re.DOTALL)
_NODE_ID = re.compile(r'^(?P<path>[\w/]+\.py)::(?P<name>\w+)(?:\[(?P<param>[^\]]+)\])?$')


def test_every_test_visible_divergence_is_referenced() -> None:
    text = DIVERGENCES_FILE.read_text()
    test_visible = {m.group(1) for m in _ENTRY.finditer(text) if '- Kind: test-visible' in m.group(0)}
    referenced = {ref for path in TESTS_DIR.glob('*.py') for ref in _REF_IN_SOURCE.findall(path.read_text())}

    assert test_visible, 'no test-visible divergences parsed; has the DIVERGENCES.md format changed?'
    assert test_visible - referenced == set(), 'test-visible divergences with no test referencing them'


def test_every_community_difference_is_documented() -> None:
    section = _COMMUNITY_SECTION.search(DIVERGENCES_FILE.read_text())
    assert section, 'no "# Community fork differences" section in DIVERGENCES.md'
    documented = set(re.findall(r'D-\d{3}', section.group(1)))
    assert set(KNOWN_COMMUNITY_DIFFERENCES.values()) - documented == set(), 'community differences missing from DIVERGENCES.md'


def _param_ids(func: object) -> set[str]:
    ids: set[str] = set()
    for mark in getattr(func, 'pytestmark', []):
        if mark.name == 'parametrize':
            ids.update(v.id for v in mark.args[1] if getattr(v, 'id', None) is not None)
    return ids


def test_every_community_difference_names_an_existing_test() -> None:
    root = TESTS_DIR.parents[1]
    for node_id in KNOWN_COMMUNITY_DIFFERENCES:
        match = _NODE_ID.match(node_id)
        assert match, f'{node_id}: not a path::test[param] node id'
        path = root / match['path']
        assert path.is_file(), f'{node_id}: no such file'
        module = importlib.import_module('.'.join(Path(match['path']).with_suffix('').parts))
        func = getattr(module, match['name'], None)
        assert callable(func), f'{node_id}: no such test'
        if match['param'] is not None:
            assert match['param'] in _param_ids(func), f'{node_id}: no such parametrized case'
