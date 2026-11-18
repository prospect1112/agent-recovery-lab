import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from boltons.iterutils import bucketize


class FailToPass(unittest.TestCase):
    def test_empty_and_matching(self):
        self.assertEqual(bucketize(iter(()), key=[]), {})
        self.assertEqual(bucketize((value for value in "abc"), key=[1, 2, 1]),
                         {1: ["a", "c"], 2: ["b"]})

    def test_iterator_length_mismatches(self):
        for values, keys in [([1], [0, 1]), ([1, 2], [0])]:
            with self.subTest(values=values, keys=keys), self.assertRaises(ValueError):
                bucketize(iter(values), key=keys)

    def test_overrun_uses_at_most_one_extra_value(self):
        def guarded():
            yield 0
            yield 1
            yield 2
            self.fail("read past the first extra value")

        with self.assertRaises(ValueError):
            bucketize(guarded(), key=["a", "b"])

    def test_iterator_transform_and_filter(self):
        self.assertEqual(bucketize(iter([1, 2, 3]), key=["a", "b", "a"],
                                  value_transform=lambda value: value * 10,
                                  key_filter=lambda key: key == "a"), {"a": [10, 30]})


class PassToPass(unittest.TestCase):
    def test_sized_source(self):
        self.assertEqual(bucketize([1, 2, 3], key=["a", "b", "a"]),
                         {"a": [1, 3], "b": [2]})
        with self.assertRaises(ValueError):
            bucketize([1, 2], key=["a"])

    def test_callable_and_string_keys(self):
        self.assertEqual(bucketize(iter([0, 1, 2])), {False: [0], True: [1, 2]})
        self.assertEqual(bucketize(iter([1 + 2j, 1 + 3j]), key="real"),
                         {1.0: [1 + 2j, 1 + 3j]})


if __name__ == "__main__":
    groups = {}
    for group in (FailToPass, PassToPass):
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(group)
        )
        failed = {test.id().split(" (")[0] for test, _ in result.failures + result.errors}
        groups[group.__name__] = {"total": result.testsRun, "failed": len(failed)}
    print(json.dumps(groups))
    sys.exit(any(group["failed"] for group in groups.values()))
