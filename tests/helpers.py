import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "skills", "glab-overseer", "scripts")
ENTRY = os.path.join(SCRIPTS, "overseer.py")
sys.path.insert(0, SCRIPTS)

SHIMS = ("claude", "agy", "gh", "sbatch", "squeue", "scontrol", "sacct", "sinfo")


def base_plan():
    return {
        "plan_id": "demo",
        "repo": "lab-org/demo-repo",
        "target_branch": "main",
        "scope": ["src/**", "tests/**", "docs/*.md"],
        "slurm": {"partition": "batch-cpu", "account": "lab-account", "time": "01:00:00", "mem": "4G"},
        "units": [
            {"id": "alpha", "kind": "code", "title": "Add alpha", "prompt": "Write alpha.",
             "claims": ["alpha works"], "test_command": "python -m unittest",
             "outputs": ["src/alpha.py", {"path": "src/alpha_data.txt", "min_bytes": 1}],
             "scope": ["src/alpha*", "tests/test_alpha.py"], "needs": []},
            {"id": "beta", "kind": "code", "title": "Add beta", "prompt": "Write beta.",
             "claims": ["beta works", "beta is fast"], "test_command": "python -m unittest",
             "outputs": ["src/beta.py"], "scope": ["src/beta.py"], "needs": ["alpha"]},
            {"id": "crunch", "kind": "job", "title": "Crunch", "command": "python run.py",
             "outputs": ["result.txt"], "needs": ["beta"]},
        ],
    }


def plan_copy():
    return copy.deepcopy(base_plan())


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ovr-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "project")
        os.makedirs(self.root)
        self.shims = os.path.join(self.tmp, "shims")
        os.makedirs(self.shims)
        self.marker = os.path.join(self.tmp, "shim-called")
        for name in SHIMS:
            p = os.path.join(self.shims, name)
            with open(p, "w") as f:
                f.write("#!/bin/sh\necho %s >> '%s'\nexit 97\n" % (name, self.marker))
            os.chmod(p, 0o755)

    def tearDown(self):
        self.assertFalse(os.path.exists(self.marker), "an external CLI shim was invoked")

    def env(self):
        git = shutil.which("git")
        dirs = [self.shims, "/usr/bin", "/bin"]
        if git and os.path.dirname(git) not in dirs:
            dirs.append(os.path.dirname(git))
        return {"PATH": os.pathsep.join(dirs), "HOME": self.tmp, "LC_ALL": "C.UTF-8"}

    def write_plan(self, plan=None, name="plan.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            json.dump(plan if plan is not None else plan_copy(), f)
        return path

    def cli(self, *args, **kw):
        return subprocess.run([sys.executable, ENTRY] + list(args), cwd=kw.get("cwd", self.root),
                              env=self.env(), capture_output=True, text=True, timeout=60)
