import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from heldout import run_groups
sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
import attr


class FailToPass(unittest.TestCase):
    def test_grandchild(self):
        @attr.s
        class Parent:
            item: "int" = attr.ib()

        @attr.s
        class Child(Parent):
            label: "str" = attr.ib()

        @attr.s
        class Grandchild(Child):
            enabled: "bool" = attr.ib()

        for cls in (Parent, Child, Grandchild):
            attr.resolve_types(cls)
        self.assertIs(attr.fields(Grandchild).enabled.type, bool)
        self.assertIs(attr.fields(Grandchild).label.type, str)

    def test_multiple_inheritance(self):
        @attr.s
        class Parent:
            value: "int" = attr.ib()

        @attr.s
        class Child(Parent, dict):
            name: "str" = attr.ib()

        attr.resolve_types(Parent)
        attr.resolve_types(Child)
        self.assertIs(attr.fields(Child).name.type, str)


class PassToPass(unittest.TestCase):
    def test_repeated_resolve_uses_cache(self):
        @attr.s
        class Direct:
            value: "int" = attr.ib()

        self.assertIs(attr.resolve_types(Direct), Direct)
        with patch("typing.get_type_hints", side_effect=AssertionError("uncached")):
            self.assertIs(attr.resolve_types(Direct), Direct)
        self.assertIs(attr.fields(Direct).value.type, int)


if __name__ == "__main__":
    run_groups(FailToPass, PassToPass)
