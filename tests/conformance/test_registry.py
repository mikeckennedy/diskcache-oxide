"""Keep tests/DIVERGENCES.md and the tests' divergence references in step.

Unknown refs are rejected at collection time by conftest.py. This checks the other direction:
every test-visible divergence is referenced by at least one test.
"""

import re
from pathlib import Path

from tests.conformance.conftest import DIVERGENCES_FILE

TESTS_DIR = Path(__file__).parent
_ENTRY = re.compile(r'^##\s+(D-\d{3})\b.*?(?=^##\s|\Z)', re.MULTILINE | re.DOTALL)
_REF_IN_SOURCE = re.compile(r"""['"](D-\d{3})['"]""")


def test_every_test_visible_divergence_is_referenced() -> None:
    text = DIVERGENCES_FILE.read_text()
    test_visible = {m.group(1) for m in _ENTRY.finditer(text) if '- Kind: test-visible' in m.group(0)}
    referenced = {ref for path in TESTS_DIR.glob('*.py') for ref in _REF_IN_SOURCE.findall(path.read_text())}

    assert test_visible, 'no test-visible divergences parsed; has the DIVERGENCES.md format changed?'
    assert test_visible - referenced == set(), 'test-visible divergences with no test referencing them'
