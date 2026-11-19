"""A `DiscoverRunner` that holds the run to the strict tables in `oxide_suite.tests`.

An unexpected success already fails a unittest run. This adds the other direction for skips, which
can't fail by themselves: a skip that isn't in `SKIPS`, or a test in `SKIPS` that ran instead of
skipping, fails the run too.
"""

from django.test.runner import DiscoverRunner
from django.test.utils import iter_test_cases

from oxide_suite import tests


class StrictRunner(DiscoverRunner):
    def run_suite(self, suite, **kwargs):
        # unittest drops each test from its suite once it has run, so list them first.
        self.cases = list(iter_test_cases(suite))
        return super().run_suite(suite, **kwargs)

    def suite_result(self, suite, result, **kwargs):
        failures = super().suite_result(suite, result, **kwargs)
        problems = tests.check_strict_tables(self.cases, result)
        for problem in problems:
            print(f'STRICT TABLES: {problem}', flush=True)
        if not problems:
            print(f'strict tables: {tests.table_summary(result)}', flush=True)
        return failures + len(problems)
