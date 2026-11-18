import argparse
import difflib
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
UPSTREAM = "https://github.com/mahmoud/boltons.git"
BASE = "37ba6b2a3adde5795dee9195b255a4cb1d342a7b"
REFERENCE = "6270b5e35ed902859df6188970eec5bee5ab73d1"
SOURCE = "boltons/iterutils.py"
TASK = ("Fix bucketize for one-shot iterators with list keys. Preserve length "
        "validation and the existing value_transform and key_filter behavior.")
BUDGET = 2
TIMEOUT = 10
ORIGINAL = '    elif isinstance(key, list):\n        if len(key) != len(src):'
CHECKPOINT = '    elif isinstance(key, list):\n        key_len = len(key)\n        if key_len != len(src):'
DETOUR = ('    elif isinstance(key, list):\n        key_len = len(key)\n'
          '        src = iter(src) if not hasattr(src, "__len__") else src\n'
          '        if key_len != len(src):')
ERROR = "TypeError: object of type 'list_iterator' has no len()"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def replace_once(source, old, new):
    require(source.count(old) == 1, "Expected exactly one edit anchor")
    return source.replace(old, new, 1)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")


def save_json(path, value):
    write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def diff(before, after):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        "a/" + SOURCE, "b/" + SOURCE))


def prepare_cache(cache):
    if not (cache / "HEAD").is_file():
        subprocess.run(["git", "init", "--bare", "--quiet", str(cache)], check=True)
    for revision in (BASE, REFERENCE):
        found = subprocess.run(["git", "-C", str(cache), "cat-file", "-e", revision + "^{commit}"],
                               capture_output=True)
        if found.returncode:
            subprocess.run(["git", "-C", str(cache), "fetch", "--quiet", "--depth=1",
                            "--no-tags", UPSTREAM, revision], check=True, timeout=120)


def test(workspace, fixture):
    script = ROOT / "case" / (fixture + ".py")
    if fixture == "visible":
        script = workspace / "repro.py"
    argv = [sys.executable, "-I", "-B", str(script), str(workspace)]
    started = time.perf_counter()
    try:
        result = subprocess.run(argv, cwd=workspace, capture_output=True, encoding="utf-8",
                                errors="replace", timeout=TIMEOUT)
        record = {"exit_code": result.returncode, "stdout": result.stdout,
                  "stderr": result.stderr, "timed_out": False}
    except subprocess.TimeoutExpired as error:
        record = {"exit_code": None, "timed_out": True,
                  "stdout": (error.stdout or b"").decode("utf-8", "replace"),
                  "stderr": (error.stderr or b"").decode("utf-8", "replace")}
    return record | {"argv": argv, "cwd": str(workspace), "timeout_s": TIMEOUT,
                     "seconds": round(time.perf_counter() - started, 6)}


class Workspace:
    def __init__(self, path, base_tree, source):
        self.path = path
        self.events = []
        shutil.copytree(base_tree, path)
        shutil.copyfile(ROOT / "case" / "visible.py", path / "repro.py")
        write(path / SOURCE, source)

    def read(self):
        return (self.path / SOURCE).read_text(encoding="utf-8")

    def edit(self, source):
        before = self.read()
        write(self.path / SOURCE, source)
        self.events.append({"action": "edit", "path": SOURCE, "diff": diff(before, source),
                            "source_sha256": digest(source)})

    def smoke(self):
        result = test(self.path, "visible")
        result["signature"] = (result["stderr"].strip().splitlines() or [""])[-1]
        self.events.append({"action": "smoke", "source_sha256": digest(self.read()), **result})
        return result


class BudgetTools:
    def __init__(self, workspace, limit):
        self._workspace = workspace
        self.limit = limit
        self.spent = 0

    def _charge(self):
        require(self.spent < self.limit, "Recovery action budget exceeded")
        self.spent += 1

    def read(self):
        return self._workspace.read()

    def edit(self, source):
        self._charge()
        self._workspace.edit(source)

    def smoke(self):
        self._charge()
        return self._workspace.smoke()


def mock_executor(tools, context):
    if "history" in context:
        tools.smoke()
        tools.smoke()
        return "budget_exhausted"
    source = tools.read()
    if DETOUR in source:
        wrong = DETOUR.replace("if key_len != len(src):", 'if hasattr(src, "__len__") and key_len != len(src):')
        tools.edit(replace_once(source, DETOUR, wrong))
    else:
        bounded = ('        try:\n            src_len = len(src)\n'
                   '        except TypeError:\n'
                   '            src = list(itertools.islice(src, key_len + 1))\n'
                   '            src_len = len(src)\n'
                   '        if key_len != src_len:')
        tools.edit(replace_once(source, CHECKPOINT,
                                '    elif isinstance(key, list):\n        key_len = len(key)\n' + bounded))
    return "submitted" if tools.smoke()["exit_code"] == 0 else "visible_failed"


def verify(folder, base_tree, base_source, candidate):
    workspace = Workspace(folder / "verifier", base_tree, base_source)
    patch_path = folder / "candidate.patch"
    patch = diff(base_source, candidate)
    write(patch_path, patch)
    if patch:
        env = os.environ.copy()
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(name, None)
        env["GIT_CEILING_DIRECTORIES"] = str(workspace.path.parent)
        subprocess.run(["git", "apply", str(patch_path)], cwd=workspace.path,
                       check=True, capture_output=True, timeout=TIMEOUT, env=env)
    require(workspace.read() == candidate, "Reapplied patch does not reproduce candidate")
    visible = workspace.smoke()
    hidden = test(workspace.path, "held_out")
    if hidden["timed_out"] or visible["timed_out"]:
        return {"status": "timeout", "visible": visible, "held_out": hidden, "groups": None}
    require(hidden["exit_code"] in (0, 1) and hidden["stdout"].strip(),
            "Held-out runner failed before reporting results")
    groups = json.loads(hidden["stdout"])
    require(hidden["exit_code"] == int(any(group["failed"] for group in groups.values())),
            "Held-out test exit status and counts disagree")
    status = "pass" if visible["exit_code"] == hidden["exit_code"] == 0 else "fail"
    return {"status": status, "visible": visible, "held_out": hidden, "groups": groups}


def reference_candidate(cache, base_source):
    upstream = subprocess.check_output(["git", "-C", str(cache), "show",
                                        REFERENCE + ":" + SOURCE]).decode("utf-8")

    def block(source):
        bucketize = source.split("\ndef bucketize(", 1)[1]
        return bucketize[bucketize.index("    if not is_iterable(src):"):
                         bucketize.index("        src = zip(key, src)")]

    return replace_once(base_source, block(base_source), block(upstream))


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    cache = output / "upstream.git"
    prepare_cache(cache)
    directory = Path(tempfile.mkdtemp(prefix="trial-", dir=output))
    base_tree = directory / "base"
    archive = subprocess.check_output(["git", "-C", str(cache), "archive", BASE], timeout=120)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(base_tree, filter="data")
    base_source = (base_tree / SOURCE).read_text(encoding="utf-8")
    prefix = Workspace(directory / "prefix", base_tree, base_source)
    prefix.edit(replace_once(base_source, ORIGINAL, CHECKPOINT))
    checkpoint = prefix.read()
    prefix.edit(replace_once(checkpoint, CHECKPOINT, DETOUR))
    failures = [prefix.smoke(), prefix.smoke()]
    require(all(result["signature"] == ERROR and result["exit_code"] == 1
                for result in failures), "Stall fixture drifted")
    stalled = prefix.read()
    write(directory / "checkpoint.patch", diff(base_source, checkpoint))
    write(directory / "stalled.patch", diff(base_source, stalled))
    save_json(directory / "prefix.json", {"task": TASK, "actions": len(prefix.events),
              "checkpoint_after_action": 1, "checkpoint_sha256": digest(checkpoint),
              "stalled_sha256": digest(stalled), "events": prefix.events})

    controls = {"unpatched": verify(directory / "unpatched", base_tree, base_source, base_source),
                "upstream_fix": verify(directory / "upstream_fix", base_tree, base_source,
                                       reference_candidate(cache, base_source))}
    require(controls["unpatched"]["groups"] == {"FailToPass": {"total": 4, "failed": 4},
                                               "PassToPass": {"total": 2, "failed": 0}}
            and controls["unpatched"]["visible"]["exit_code"] == 1
            and controls["upstream_fix"]["status"] == "pass", "Invalid test controls")

    full = {"task": TASK, "history": prefix.events}
    brief = {"task": TASK, "repro": "python repro.py .", "failure": failures[-1]["signature"],
             "repeat_count": len(failures)}
    routes = {}
    for name, start, context in [("continue", stalled, full), ("restart", stalled, brief),
                                 ("rollback", checkpoint, brief)]:
        folder = directory / name
        workspace = Workspace(folder / "executor", base_tree, start)
        save_json(folder / "context.json", context)
        tools = BudgetTools(workspace, BUDGET)
        started = time.perf_counter()
        stop = mock_executor(tools, context)
        elapsed = round(time.perf_counter() - started, 6)
        routes[name] = {"start_sha256": digest(start), "context_sha256": digest(json.dumps(context)),
                        "actions": tools.spent, "action_limit": BUDGET, "stop": stop,
                        "recovery_seconds": elapsed, "api_calls": 0, "api_cost_usd": 0,
                        "events": workspace.events,
                        **verify(folder, base_tree, base_source, workspace.read())}

    validate_mock(routes)
    report = {"case": "boltons-563", "upstream": UPSTREAM, "base": BASE, "reference": REFERENCE,
              "executor": "scripted mock", "python": platform.python_version(),
              "git": subprocess.check_output(["git", "--version"], text=True).strip(),
              "platform": platform.platform(), "controls": controls, "routes": routes}
    save_json(directory / "report.json", report)
    print(f"Stall: {ERROR} (2 consecutive failures); prefix actions: {len(prefix.events)}")
    print("name          smoke  hidden  actions   recovery s   API USD   stop")
    for name, entry in (controls | routes).items():
        groups = entry["groups"] or {}
        total = sum(group["total"] for group in groups.values())
        passed = total - sum(group["failed"] for group in groups.values())
        smoke = "PASS" if entry["visible"]["exit_code"] == 0 else "FAIL"
        actions = f"{entry['actions']}/{BUDGET}" if "actions" in entry else "-"
        elapsed = f"{entry['recovery_seconds']:.4f}" if "recovery_seconds" in entry else "-"
        cost = f"{entry['api_cost_usd']:.2f}" if "api_cost_usd" in entry else "-"
        print(f"{name:13} {smoke:6} {f'{passed}/{total}':7} {actions:9} {elapsed:12} {cost:9} {entry.get('stop', '-')}")
    print(f"Report: {directory / 'report.json'}")


def validate_mock(routes):
    continued, restarted, rolled_back = (routes[name] for name in ("continue", "restart", "rollback"))
    require(all(route["actions"] == BUDGET for route in routes.values()), "Mock budgets diverged")
    require(continued["start_sha256"] == restarted["start_sha256"] != rolled_back["start_sha256"]
            and continued["context_sha256"] != restarted["context_sha256"] == rolled_back["context_sha256"],
            "Mock branch start states or contexts diverged from the protocol")
    require(continued["stop"] == "budget_exhausted" and continued["status"] == "fail",
            "Continue fixture did not exhaust the budget without a fix")
    require(restarted["visible"]["exit_code"] == 0 and restarted["status"] == "fail"
            and restarted["groups"]["FailToPass"]["failed"] == 2
            and restarted["groups"]["PassToPass"]["failed"] == 0,
            "Invalid patch control did not fail as expected")
    require(rolled_back["status"] == "pass", "Rollback fixture did not produce a valid patch")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay three mock recovery routes from a pinned stall.")
    parser.add_argument("--out", type=Path, default=ROOT / ".runs")
    run(parser.parse_args().out.resolve())
