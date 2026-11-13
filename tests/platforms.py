"""Platform skips, in one place so every Unix-only test says why (plan 25).

Windows has no `fork()`, no signals beyond a few console events, no `resource` or `fcntl`, no mode
bits and no reparenting; a test that needs one of those is marked `@unix_only('reason')`. A test that
fails on Windows for any other reason gets a fix or a decision, not this marker.
"""

from __future__ import annotations

import sys

import pytest

WINDOWS = sys.platform == 'win32'
# The conformance conftest counts a skip with this prefix as a marker's, not an unexpected one.
UNIX_ONLY_PREFIX = 'Unix only: '


def unix_only(reason: str) -> pytest.MarkDecorator:
    """Skip on Windows, saying which Unix facility the test needs."""
    return pytest.mark.skipif(WINDOWS, reason=f'{UNIX_ONLY_PREFIX}{reason}')
