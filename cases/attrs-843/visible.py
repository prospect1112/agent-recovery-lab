import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
import attr


@attr.s
class Parent:
    value: "int" = attr.ib()


@attr.s
class Child(Parent):
    extra: "str" = attr.ib()


attr.resolve_types(Parent)
attr.resolve_types(Child)
assert attr.fields(Child).extra.type is str, "child annotation was not resolved"
