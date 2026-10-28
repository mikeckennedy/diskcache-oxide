"""Unit tests of diskcache_oxide internals, through the private `_core` test hooks.

These run against oxide only, so they ignore `--backend` and use none of the conformance fixtures.
`HYPOTHESIS_EXAMPLES` sets the Hypothesis example count (default 1500; 30000 for a long run).
"""

import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    'unit',
    max_examples=int(os.environ.get('HYPOTHESIS_EXAMPLES', '1500')),
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile('unit')
