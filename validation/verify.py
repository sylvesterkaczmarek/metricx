import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

meta = json.loads((Path(__file__).parent / "case.json").read_text())
work = Path(sys.argv[1]).resolve()
evidence = Path(sys.argv[2]).resolve()
evidence.mkdir(parents=True, exist_ok=True)
python = sys.executable
is_cad = meta["repo"].endswith("cad-estate")
source_root = work / "src" if is_cad else work
env = dict(os.environ, PYTHONPATH=str(source_root))
summary = {"source_commit": meta["head"]}
fixed = {name: (work / name).read_bytes() for name in meta["sources"]}


def run(label, command, expected=0, cwd=work, run_env=None):
    with (evidence / (label + ".log")).open("w") as log:
        result = subprocess.run(command, cwd=cwd, env=run_env or env,
                                stdout=log, stderr=subprocess.STDOUT, timeout=240)
    output = (evidence / (label + ".log")).read_text(errors="replace")
    print(label, result.returncode, output[-500:], flush=True)
    assert result.returncode == expected, (label, result.returncode, output[-7000:])


def test(label, expected=0, path=None, cwd=work, run_env=None, coverage=False):
    command = [python, "-m", "pytest", str(path or meta["test"]), "-q",
               "--tb=short", "--junitxml=" + str(evidence / (label + ".xml"))]
    if is_cad:
        command += ["-W", "error::ResourceWarning"]
    if coverage:
        packages = ["cad_estate"] if is_cad else ["metricx23", "metricx24"]
        command += ["--cov=" + name for name in packages]
        command += ["--cov-branch", "--cov-report=json:" + str(evidence / "coverage.json")]
    run(label, command, expected, cwd, run_env)
    root = ET.parse(evidence / (label + ".xml")).getroot()
    counts = [sum(int(s.attrib.get(key, 0)) for s in root.iter("testsuite"))
              for key in ("tests", "failures", "errors", "skipped")]
    failures = sorted(t.attrib.get("classname", "") + "::" + t.attrib["name"]
                      for t in root.iter("testcase")
                      if t.find("failure") is not None or t.find("error") is not None)
    summary[label] = {"counts": counts, "failed_nodes": failures}
    print(label, counts, flush=True)
    assert counts == [meta["counts"][0], meta["baseline_failure_count"] if expected else 0, 0, 0], counts


actual_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()
assert actual_head == meta["head"], actual_head
print("TESTED SOURCE", actual_head, flush=True)
run("dependencies", [python, "-m", "pip", "freeze"])
run("dependency-check", [python, "-m", "pip", "check"])
try:
    test("fixed-tests", coverage=True)
    for name in meta["sources"]:
        original = subprocess.check_output(["git", "show", meta["base"] + ":" + name], cwd=work)
        (work / name).write_bytes(original)
    test("original-tests", expected=1)
finally:
    for name, contents in fixed.items():
        (work / name).write_bytes(contents)
test("restored-tests")
run("source-restoration", ["git", "diff", "--exit-code"])
run("patch-check", ["git", "diff", "--check", meta["base"]])
run("test-lint", [python, "-m", "ruff", "check", "--select", "E,F", meta["test"]])
run("source-lint", [python, "-m", "ruff", "check", "--select", "E9,F63,F7,F82", *meta["sources"]])
run("test-format", [python, "-m", "yapf", "--style=google", "--diff", meta["test"]])
assert not (evidence / "test-format.log").read_text().strip()
run("compile", [python, "-m", "py_compile", *meta["sources"], meta["test"]])
with tempfile.TemporaryDirectory() as directory:
    outside = Path(directory)
    packages = ["cad_estate"] if is_cad else ["metricx23", "metricx24"]
    for name in packages:
        shutil.copytree(source_root / name, outside / name,
                        ignore=shutil.ignore_patterns("__pycache__"))
    if is_cad:
        test_path = outside / "cad_estate/file_system_test.py"
    else:
        test_path = outside / "encoder_inputs_test.py"
        shutil.copyfile(work / meta["test"], test_path)
    outside_env = dict(env, PYTHONPATH=str(outside))
    import_code = "import importlib, pathlib; root=pathlib.Path.cwd(); " + "; ".join(
        "p=pathlib.Path(importlib.import_module(" + repr(name) + ").__file__).resolve(); "
        "print(p); assert p.is_relative_to(root)" for name in packages)
    run("exported-imports", [python, "-c", import_code], cwd=outside, run_env=outside_env)
    test("exported-tests", path=test_path, cwd=outside, run_env=outside_env)
summary["source_hashes"] = {name: hashlib.sha256(data).hexdigest() for name, data in fixed.items()}
(evidence / "summary.json").write_text(json.dumps(summary, indent=2))
print("VALIDATION COMPLETE", actual_head, flush=True)
