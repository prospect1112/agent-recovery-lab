import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from boltons.iterutils import bucketize

assert bucketize(iter([10, 20, 30]), key=["a", "b", "a"]) == {
    "a": [10, 30], "b": [20]
}
print("PASS: matched iterator and key list")
