import json
import sys
import unittest


def run_groups(*groups):
    counts = {}
    for group in groups:
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(group)
        )
        failed = {getattr(test, "test_case", test).id()
                  for test, _ in result.failures + result.errors}
        counts[group.__name__] = {"total": result.testsRun, "failed": len(failed)}
    print(json.dumps(counts))
    sys.exit(any(group["failed"] for group in counts.values()))
