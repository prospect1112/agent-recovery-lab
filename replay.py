import argparse
import ast
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from itertools import combinations
from pathlib import Path

from model_executor import DeepSeek


ROOT = Path(__file__).resolve().parent
CASES = ROOT / "cases"
ROUTES = {
    "continue": ("stalled", "full"),
    "restart": ("stalled", "brief"),
    "rollback": ("checkpoint", "brief"),
    "rollback-full": ("checkpoint", "full"),
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def replace_once(source, old, new):
    require(old and source.count(old) == 1, "Expected exactly one edit anchor")
    return source.replace(old, new, 1)


def digest(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")


def save_json(path, value):
    pending = path.with_suffix(".tmp")
    write(pending, json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    pending.replace(path)


def patch_text(case, before, after):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        "a/" + case["source"], "b/" + case["source"]))


def git_env():
    env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    return env


def populate(case, cache, offline):
    if not (cache / "HEAD").is_file():
        require(not offline, f"Offline cache missing: {cache}")
        subprocess.run(["git", "init", "--bare", "--quiet", str(cache)], check=True, env=git_env())
    for revision in (case["base"], case["reference"]):
        found = subprocess.run(["git", "-C", str(cache), "cat-file", "-e", revision + "^{commit}"],
                               capture_output=True, env=git_env())
        if found.returncode:
            require(not offline, f"Offline cache missing {revision} in {cache}")
            subprocess.run(["git", "-C", str(cache), "fetch", "--quiet", "--depth=1", "--no-tags",
                            case["upstream"], revision], check=True, timeout=120, env=git_env())


def materialize(case, cache, destination):
    archive = subprocess.check_output(["git", "-C", str(cache), "archive", case["base"]],
                                      timeout=120, env=git_env())
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(destination, filter="data")


def source_function(case, source):
    body = ast.parse(source).body
    function = next(item for item in body if isinstance(item, ast.FunctionDef)
                    and item.name == case["function"])
    return "".join(source.splitlines(keepends=True)[function.lineno - 1:function.end_lineno])


def reference_source(case, cache, baseline):
    upstream = subprocess.check_output(["git", "-C", str(cache), "show",
                                        case["reference"] + ":" + case["source"]],
                                       env=git_env()).decode("utf-8")
    original, fixed = (source_function(case, source) for source in (baseline, upstream))
    if "reference_bounds" in case:
        start, end = case["reference_bounds"]
        original, fixed = (block[block.index(start):block.index(end)] for block in (original, fixed))
    return replace_once(baseline, original, fixed)


class TestRunner:
    def __init__(self, timeout, image=None):
        self.timeout = timeout
        self.image = image

    def run(self, workspace, evaluator=None):
        started = time.perf_counter()
        container = None
        if self.image:
            container = "recovery-" + uuid.uuid4().hex
            command = ["docker", "run", "--pull=never", "--rm", "--name", container,
                       "--network=none", "--read-only", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges", "--user=65534:65534",
                       "--memory=256m", "--cpus=1", "--pids-limit=32", "--workdir=/work",
                       "--mount", f"type=bind,src={workspace},dst=/work,readonly"]
            if evaluator:
                command += ["--mount", f"type=bind,src={evaluator},dst=/evaluator,readonly"]
            command += [self.image, "python", "-I", "-B",
                        "/evaluator/held_out.py" if evaluator else "/work/repro.py", "/work"]
        else:
            script = evaluator / "held_out.py" if evaluator else workspace / "repro.py"
            command = [sys.executable, "-I", "-B", str(script), str(workspace)]
        captured = {}

        def drain(channel, stream):
            tail = b""
            with stream:
                while chunk := stream.read(8192):
                    tail = (tail + chunk)[-32768:]
            captured[channel] = tail.decode("utf-8", "replace")

        with subprocess.Popen(command, cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            readers = [threading.Thread(target=drain, args=(name, getattr(process, name)))
                       for name in ("stdout", "stderr")]
            for reader in readers:
                reader.start()
            try:
                code = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                code = None
                process.kill()
                process.wait()
                if container:
                    subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=10)
            finally:
                for reader in readers:
                    reader.join()
        record = {"exit_code": code, "timed_out": code is None, **captured}
        return record | {"argv": command, "seconds": round(time.perf_counter() - started, 6)}


class Workspace:
    def __init__(self, path, base_tree, case, source, runner):
        self.path, self.case, self.runner = path, case, runner
        self.events = []
        shutil.copytree(base_tree, path)
        shutil.copyfile(case["dir"] / "visible.py", path / "repro.py")
        write(path / case["source"], source)

    def read(self):
        return (self.path / self.case["source"]).read_text(encoding="utf-8")

    def edit(self, source):
        before = self.read()
        write(self.path / self.case["source"], source)
        self.events.append({"action": "edit", "diff": patch_text(self.case, before, source),
                            "source_sha256": digest(source)})

    def smoke(self):
        result = self.runner.run(self.path)
        result["signature"] = (result["stderr"].strip().splitlines() or [""])[-1]
        self.events.append({"action": "smoke", "source_sha256": digest(self.read()), **result})
        return result


def verify(folder, base_tree, case, baseline, candidate, runner, evaluator):
    workspace = Workspace(folder / "verifier", base_tree, case, baseline, runner)
    patch_path = folder / "candidate.patch"
    patch = patch_text(case, baseline, candidate)
    write(patch_path, patch)
    if patch:
        env = git_env()
        env["GIT_CEILING_DIRECTORIES"] = str(workspace.path.parent)
        subprocess.run(["git", "apply", str(patch_path)], cwd=workspace.path, env=env,
                       check=True, capture_output=True, timeout=10)
    require(workspace.read() == candidate, "Reapplied patch does not reproduce candidate")
    visible = workspace.smoke()
    hidden = runner.run(workspace.path, evaluator)
    groups = None
    if visible["timed_out"] or hidden["timed_out"]:
        status = "timeout"
    elif hidden["exit_code"] not in (0, 1) or visible["exit_code"] not in (0, 1):
        status = "error"
    else:
        try:
            groups = json.loads(hidden["stdout"].splitlines()[-1])
            require(set(groups) == {"FailToPass", "PassToPass"}, "Wrong result groups")
            require(all(0 <= group["failed"] <= group["total"] and group["total"] > 0
                        for group in groups.values()), "Invalid result counts")
            require(hidden["exit_code"] == int(any(group["failed"] for group in groups.values())),
                    "Held-out exit status and counts disagree")
            status = "pass" if visible["exit_code"] == hidden["exit_code"] == 0 else "fail"
        except (ValueError, TypeError, KeyError, IndexError, RuntimeError):
            status = "error"
            groups = None
    return {"status": status, "visible": visible, "held_out": hidden, "groups": groups}


def check_controls(controls):
    original, fixed = (controls[name] for name in ("unpatched", "upstream_fix"))
    require(original["status"] == "fail" and original["visible"]["exit_code"] == 1
            and original["groups"] and fixed["status"] == "pass", "Invalid test controls")
    for name, expected_failed in (("FailToPass", True), ("PassToPass", False)):
        baseline, reference = original["groups"][name], fixed["groups"][name]
        require(baseline["total"] == reference["total"]
                and baseline["failed"] == (baseline["total"] if expected_failed else 0),
                f"Invalid baseline behavior in {name}")


def mock_run(case, workspace, route, reference, limit):
    edits = case.get("mock_edits", {})
    actions = 0
    if route in case.get("mock_reference_routes", ()):
        workspace.edit(reference)
        actions += 1
    elif route in edits:
        workspace.edit(replace_once(workspace.read(), *edits[route]))
        actions += 1
    while actions < limit:
        result = workspace.smoke()
        actions += 1
        if result["timed_out"]:
            return {"actions": actions, "stop": "timeout", "api_cost_usd": 0}
        if result["exit_code"] == 0:
            return {"actions": actions, "stop": "submitted", "api_cost_usd": 0}
    return {"actions": actions, "stop": "budget_exhausted", "api_cost_usd": 0}


def run_case(case, cache, folder, args, runner, model, repetition, case_index):
    folder.mkdir()
    base_tree = folder / "base"
    materialize(case, cache, base_tree)
    baseline = (base_tree / case["source"]).read_text(encoding="utf-8")
    reference = reference_source(case, cache, baseline)
    evaluator = folder / "evaluator"
    evaluator.mkdir()
    shutil.copyfile(case["dir"] / "held_out.py", evaluator / "held_out.py")
    shutil.copyfile(ROOT / "heldout.py", evaluator / "heldout.py")
    prefix = Workspace(folder / "prefix", base_tree, case, baseline, runner)
    states = [baseline]
    for edit in case["prefix_edits"]:
        prefix.edit(replace_once(prefix.read(), *edit))
        states.append(prefix.read())
    checkpoint = args.checkpoint if args.checkpoint is not None else len(states) - 2
    require(0 <= checkpoint < len(states) - 1, "Checkpoint must precede the stalled state")
    failures = [prefix.smoke(), prefix.smoke()]
    require(all(item["exit_code"] == 1 and item["signature"] for item in failures)
            and failures[0]["signature"] == failures[1]["signature"], "Stall fixture drifted")
    save_json(folder / "prefix.json", {"checkpoint_after_action": checkpoint, "events": prefix.events})
    starts = {"stalled": states[-1], "checkpoint": states[checkpoint]}
    for state, source in starts.items():
        write(folder / (state + ".patch"), patch_text(case, baseline, source))
    controls = {name: verify(folder / name, base_tree, case, baseline, source, runner, evaluator)
                for name, source in (("unpatched", baseline), ("upstream_fix", reference))}
    check_controls(controls)
    brief = {"task": case["task"], "visible_failure": failures[-1]["signature"], "repeat_count": 2,
             "visible_test": (case["dir"] / "visible.py").read_text(encoding="utf-8")}
    history = []
    for event in prefix.events:
        item = {key: event[key] for key in ("action", "diff", "signature", "exit_code") if key in event}
        for channel in ("stdout", "stderr"):
            if channel in event:
                item[channel] = event[channel].replace(str(prefix.path), "<workspace>")[-2000:]
        history.append(item)
    contexts = {"brief": brief, "full": brief | {"history": history}}
    routes = {}
    names = list(args.routes)
    offset = (repetition + case_index) % len(names)
    for route in names[offset:] + names[:offset]:
        state, context_kind = ROUTES[route]
        context = contexts[context_kind]
        branch = folder / route
        workspace = Workspace(branch / "executor", base_tree, case, starts[state], runner)
        save_json(branch / "context.json", context)
        start = time.perf_counter()
        if model:
            usage = model.run(workspace, context, args.budget, args.output_tokens,
                              lambda: source_function(case, workspace.read()), replace_once)
        else:
            usage = mock_run(case, workspace, route, reference, args.budget)
        elapsed = time.perf_counter() - start
        verdict = verify(branch, base_tree, case, baseline, workspace.read(), runner, evaluator)
        if verdict["groups"] and any(group["total"] != controls["unpatched"]["groups"][name]["total"]
                                     for name, group in verdict["groups"].items()):
            verdict["status"] = "error"
        routes[route] = {"start_sha256": digest(starts[state]), "context_sha256": digest(json.dumps(context)),
                         "recovery_seconds": round(elapsed, 6), "action_limit": args.budget,
                         "events": workspace.events, **usage, **verdict}
    report = {"case": case["dir"].name, "base": case["base"], "reference": case["reference"],
              "repetition": repetition, "controls": controls, "routes": routes}
    save_json(folder / "report.json", report)
    return report


def succeeded(entry):
    return entry["status"] == "pass" and entry["stop"] not in ("error", "timeout")


def summarize(reports, routes):
    summary = {}
    for route in routes:
        entries = [report["routes"][route] for report in reports]
        passes = sum(map(succeeded, entries))
        visible = [entry for entry in entries if entry["visible"]["exit_code"] == 0
                   and entry["status"] in ("pass", "fail") and entry["stop"] not in ("error", "timeout")]
        false_fixes = sum(entry["status"] == "fail" for entry in visible)
        costs = [entry["api_cost_usd"] for entry in entries]
        total_cost = sum(costs) if all(cost is not None for cost in costs) else None
        summary[route] = {"trials": len(entries), "successes": passes, "success_rate": passes / len(entries),
                          "visible_passes_with_verdict": len(visible), "false_fixes": false_fixes,
                          "false_fix_rate": false_fixes / len(visible) if visible else None,
                          "timeouts": sum("timeout" in (entry["status"], entry["stop"]) for entry in entries),
                          "errors": sum("error" in (entry["status"], entry["stop"]) for entry in entries),
                          "mean_actions": statistics.mean(entry["actions"] for entry in entries),
                          "mean_recovery_seconds": statistics.mean(entry["recovery_seconds"] for entry in entries),
                          "total_cost_usd": total_cost,
                          "cost_per_success_usd": total_cost / passes if total_cost is not None and passes else None}
    return summary


def paired_outcomes(reports, routes):
    comparisons = {}
    for first, second in combinations(routes, 2):
        counts = {"first_only": 0, "second_only": 0, "both": 0, "neither": 0}
        for report in reports:
            first_pass, second_pass = (succeeded(report["routes"][name]) for name in (first, second))
            key = ("both" if second_pass else "first_only") if first_pass else (
                "second_only" if second_pass else "neither")
            counts[key] += 1
        comparisons[f"{first} vs {second}"] = counts
    return comparisons


def main():
    parser = argparse.ArgumentParser(description="Run a paired replay of pinned, non-security Python bugs.")
    available = sorted(path.name for path in CASES.iterdir() if (path / "case.json").is_file())
    parser.add_argument("--case", action="append", choices=available)
    parser.add_argument("--routes", nargs="+", choices=ROUTES, default=["continue", "restart", "rollback"])
    parser.add_argument("--checkpoint", type=int)
    parser.add_argument("--budget", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", type=Path, default=ROOT / ".runs")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--session", type=Path, help="Resume completed case/repetition pairs in this directory.")
    parser.add_argument("--executor", choices=("mock", "deepseek"), default="mock")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--output-tokens", type=int, default=1024, help="Total generated tokens per route.")
    parser.add_argument("--docker-image", help="Local trusted Linux Python image; required for DeepSeek.")
    parser.add_argument("--prices", type=float, nargs=3, metavar=("CACHE_HIT", "CACHE_MISS", "OUTPUT"),
                        help="USD per million tokens; omit to report cost as unknown.")
    args = parser.parse_args()
    require(min(args.budget, args.repeat, args.timeout, args.output_tokens) > 0, "Limits must be positive")
    require(len(set(args.routes)) == len(args.routes), "Routes must be unique")
    require(not (args.offline and args.executor != "mock" and not args.prepare_only),
            "Offline mode does not allow model requests")
    args.out = args.out.resolve()
    cache_root = args.cache.resolve() if args.cache else args.out / "cache"
    case_names = args.case or available
    require(len(set(case_names)) == len(case_names), "Cases must be unique")
    cases = []
    for name in case_names:
        directory = CASES / name
        case = json.loads((directory / "case.json").read_text(encoding="utf-8")) | {"dir": directory}
        require(all(re.fullmatch("[0-9a-f]{40}", case[key]) for key in ("base", "reference")),
                "Revisions must be full Git commit hashes")
        cache = cache_root / (name + ".git")
        populate(case, cache, args.offline)
        cases.append((case, cache))
    if args.prepare_only:
        print(f"Prepared {len(cases)} case caches in {cache_root}")
        return
    if args.checkpoint is not None:
        require(all(0 <= args.checkpoint < len(case["prefix_edits"]) for case, _ in cases),
                "Checkpoint must precede the stalled state in every selected case")
    require(args.executor == "mock" or args.docker_image, "DeepSeek requires an isolated Docker runner")
    image = None
    if args.docker_image:
        image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}",
                                         args.docker_image], text=True, timeout=30).strip()
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", image), "Could not pin Docker image")
    runner = TestRunner(args.timeout, image)
    model = DeepSeek(args.model, args.prices) if args.executor == "deepseek" else None
    args.out.mkdir(parents=True, exist_ok=True)
    session = args.session.resolve() if args.session else Path(tempfile.mkdtemp(prefix="batch-", dir=args.out))
    session.mkdir(parents=True, exist_ok=True)
    settings = {key: getattr(args, key) for key in ("budget", "timeout", "repeat", "routes", "executor",
                                                    "checkpoint", "model", "output_tokens", "prices")}
    fixtures = [Path(__file__), ROOT / "heldout.py", ROOT / "model_executor.py"]
    fixtures += [case["dir"] / name for case, _ in cases for name in ("case.json", "visible.py", "held_out.py")]
    settings |= {"cases": case_names, "image": image, "python": sys.version.split()[0],
                 "fixtures": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in fixtures}}
    manifest = session / "settings.json"
    if manifest.exists():
        require(settings == json.loads(manifest.read_text(encoding="utf-8")), "Resume settings do not match")
    else:
        save_json(manifest, settings)
    reports = []
    for repetition in range(args.repeat):
        for case_index, (case, cache) in enumerate(cases):
            name = f"{case['dir'].name}-{repetition + 1:03}"
            completed = session / (name + ".json")
            if completed.exists():
                report = json.loads(completed.read_text(encoding="utf-8"))
            else:
                trial = session / (name + "-" + uuid.uuid4().hex[:8])
                report = run_case(case, cache, trial, args, runner, model, repetition, case_index)
                report["artifacts"] = trial.name
                save_json(completed, report)
            reports.append(report)
            print(name, " ".join(f"{route}={entry['status']}/{entry['stop']}"
                                 for route, entry in report["routes"].items()))
    summary = summarize(reports, args.routes)
    save_json(session / "summary.json", {"executor": args.executor, "routes": summary,
                                        "paired_outcomes": paired_outcomes(reports, args.routes)})
    for name, stats in summary.items():
        print(f"{name}: {stats['successes']}/{stats['trials']} success; "
              f"{stats['false_fixes']}/{stats['visible_passes_with_verdict']} false fixes among visible passes")
    print(f"Report: {session / 'summary.json'}")


if __name__ == "__main__":
    main()
