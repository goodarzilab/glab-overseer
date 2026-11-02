#!/usr/bin/env python3
"""Create the U10 acceptance fixture: a tiny Python repository plus a plan.

    make_fixture.py OUT_DIR [--plan-id ID] [--no-slurm] [--job-only] [--repo OWNER/NAME]
                    [--partition P] [--account A] [--time T] [--mem M] [--cpus N] [--plan-only]

OUT_DIR gets one commit on `main` and two ignored files: `plan.template.json` (placeholders __REPO__,
__PARTITION__, __ACCOUNT__, __TIME__, __MEM__, __CPUS__) and `plan.json` (the template filled in; an unset
placeholder stays so `plan validate` fails until it is set; an unset account is dropped). `--plan-only`
re-renders plan.json from the template and leaves the repository alone. Stdlib only, Python 3.9.
"""
import argparse
import json
import os
import re
import subprocess
import sys

PLACEHOLDERS = ("__REPO__", "__PARTITION__", "__ACCOUNT__", "__TIME__", "__MEM__", "__CPUS__")
PLAN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TARGET_RE = PLAN_ID_RE

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\nplan.json\nplan.template.json\nexpect*.json\nresult.csv\n",
    "README.md": "# textkit\n\nTiny text helpers used as an acceptance fixture.\n",
    "CHANGELOG.md": (
        "# Changelog\n\n"
        "## slug helpers\n\n"
        "Entries for slugify.\n\n"
        "- package skeleton\n\n"
        "That is the end of the slug section.\n\n"
        "## count helpers\n\n"
        "Entries for word_count.\n\n"
        "- package skeleton\n\n"
        "That is the end of the count section.\n"),
    "textkit/__init__.py": "",
    # Buggy on purpose: no punctuation removal, no whitespace collapsing, no trimming.
    "textkit/slug.py": (
        '"""URL slugs."""\n\n\n'
        "def slugify(s):\n"
        '    """Lowercase ASCII slug: letters and digits joined by single hyphens, no leading or trailing hyphen."""\n'
        '    return s.lower().replace(" ", "-")\n'),
    "tests/test_slug.py": (
        "import unittest\n\nfrom textkit.slug import slugify\n\n\n"
        "class SlugTests(unittest.TestCase):\n"
        "    def test_basic(self):\n"
        '        self.assertEqual(slugify("Hello World"), "hello-world")\n\n'
        "    def test_punctuation_dropped(self):\n"
        '        self.assertEqual(slugify("Hello, World!"), "hello-world")\n\n'
        "    def test_runs_collapse(self):\n"
        '        self.assertEqual(slugify("a   b -- c"), "a-b-c")\n\n'
        "    def test_trim(self):\n"
        '        self.assertEqual(slugify("  --Edge Case--  "), "edge-case")\n\n'
        "    def test_empty(self):\n"
        '        self.assertEqual(slugify(""), "")\n'
        '        self.assertEqual(slugify("!!!"), "")\n\n\n'
        'if __name__ == "__main__":\n    unittest.main()\n'),
    # textkit/count.py is intentionally absent: the tests fail on import until task B adds it.
    "tests/test_count.py": (
        "import unittest\n\nfrom textkit.count import word_count\n\n\n"
        "class CountTests(unittest.TestCase):\n"
        "    def test_basic(self):\n"
        '        self.assertEqual(word_count("one two three"), 3)\n\n'
        "    def test_whitespace_runs(self):\n"
        '        self.assertEqual(word_count("  one \\t two\\nthree  "), 3)\n\n'
        "    def test_empty(self):\n"
        '        self.assertEqual(word_count(""), 0)\n'
        '        self.assertEqual(word_count("   \\n"), 0)\n\n'
        "    def test_punctuation_is_part_of_word(self):\n"
        '        self.assertEqual(word_count("well, hello-there"), 2)\n\n\n'
        'if __name__ == "__main__":\n    unittest.main()\n'),
    "scripts/summarize.py": (
        '"""Write result.csv (name,words,lines) for the text files at the repository root into $OVERSEER_OUT."""\n'
        "import csv\nimport os\nimport sys\n\n\n"
        "def main():\n"
        '    out = os.environ.get("OVERSEER_OUT")\n'
        "    if not out:\n"
        '        sys.exit("OVERSEER_OUT is not set")\n'
        "    os.makedirs(out, exist_ok=True)\n"
        '    rows = []\n'
        '    for name in ("README.md", "CHANGELOG.md"):\n'
        '        with open(name, encoding="utf-8") as f:\n'
        "            text = f.read()\n"
        "        rows.append((name, len(text.split()), len(text.splitlines())))\n"
        '    with open(os.path.join(out, "result.csv"), "w", newline="", encoding="utf-8") as f:\n'
        "        w = csv.writer(f, lineterminator=\"\\n\")\n"
        '        w.writerow(["name", "words", "lines"])\n'
        "        w.writerows(rows)\n\n\n"
        'if __name__ == "__main__":\n    main()\n'),
}

SLUG_PROMPT = (
    "Fix slugify(s) in textkit/slug.py so tests/test_slug.py passes. A slug is the lowercase ASCII letters and "
    "digits of s, each other run of characters becoming one hyphen, with no leading or trailing hyphen. Do not edit "
    "tests/test_slug.py. Add tests/test_slug_extra.py with two more edge cases that fail on the original code. Add "
    "exactly one bullet line mentioning slugify at the end of the 'slug helpers' list in CHANGELOG.md and change "
    "nothing else in that file. Run the test command until it passes. Do not push.")
COUNT_PROMPT = (
    "Create textkit/count.py with word_count(s), the number of whitespace-separated words in s, so "
    "tests/test_count.py passes. Do not edit tests/test_count.py. Add tests/test_count_extra.py with two more edge "
    "cases that fail without count.py. Add exactly one bullet line mentioning word_count at the end of the 'count "
    "helpers' list in CHANGELOG.md and change nothing else in that file. Run the test command until it passes. "
    "Do not push.")


def template(plan_id, no_slurm, job_only=False, target="main"):
    plan = {
        "plan_id": plan_id,
        "repo": "__REPO__",
        "target_branch": target,
        "scope": ["textkit/**", "tests/**", "CHANGELOG.md", "scripts/**"],
        "caps": {"workers": 3, "attempts": 2, "review_rounds": 3},
        "test_globs": ["tests/**"],
        "units": [
            {"id": "slug", "kind": "code", "title": "Fix slugify",
             "prompt": SLUG_PROMPT,
             "claims": ["slugify drops punctuation, collapses runs and trims hyphens",
                        "tests/test_slug.py passes unchanged",
                        "CHANGELOG.md gains one line under the slug section only"],
             "test_command": "python3 -m unittest discover -s tests -p 'test_slug*.py' && grep -q slugify CHANGELOG.md",
             "outputs": ["textkit/slug.py", {"path": "tests/test_slug_extra.py", "min_bytes": 1}],
             "scope": ["textkit/slug.py", "tests/test_slug_extra.py", "CHANGELOG.md"], "needs": []},
            {"id": "count", "kind": "code", "title": "Add word_count",
             "prompt": COUNT_PROMPT,
             "claims": ["word_count counts whitespace-separated words",
                        "tests/test_count.py passes unchanged",
                        "CHANGELOG.md gains one line under the count section only"],
             "test_command": "python3 -m unittest discover -s tests -p 'test_count*.py' && grep -q word_count CHANGELOG.md",
             "outputs": ["textkit/count.py", {"path": "tests/test_count_extra.py", "min_bytes": 1}],
             "scope": ["textkit/count.py", "tests/test_count_extra.py", "CHANGELOG.md"], "needs": []},
            {"id": "summarize", "kind": "job", "title": "Summarize repository text files",
             "command": "python3 scripts/summarize.py",
             "outputs": [{"path": "result.csv", "min_bytes": 20,
                          "validate": "test \"$(head -n 1 \"$OVERSEER_OUTPUT\")\" = name,words,lines "
                                      "&& test \"$(wc -l < \"$OVERSEER_OUTPUT\")\" -ge 3"}],
             "needs": ["slug", "count"]},
        ],
    }
    if job_only:  # for the accounting-fallback runs: two independent jobs, no code units
        job = plan["units"][2]
        lost = dict(job, id="summarize-lost", title="Summarize, evidence will be lost", needs=[])
        plan["units"] = [dict(job, needs=[]), lost]
    if not no_slurm:
        plan["slurm"] = {"partition": "__PARTITION__", "account": "__ACCOUNT__", "time": "__TIME__",
                         "cpus": "__CPUS__", "mem": "__MEM__"}
    return plan


def render(tmpl, values):
    """Fill placeholders. `values` maps a placeholder to a string or int; unset ones stay. An unset account is dropped."""
    def fill(v):
        if isinstance(v, str) and v in PLACEHOLDERS and values.get(v) not in (None, ""):
            return values[v]
        if isinstance(v, dict):
            return dict((k, fill(x)) for k, x in v.items())
        if isinstance(v, list):
            return [fill(x) for x in v]
        return v
    out = fill(tmpl)
    sl = out.get("slurm")
    if isinstance(sl, dict) and sl.get("account") == "__ACCOUNT__" and values.get("__ACCOUNT__") in (None, ""):
        del sl["account"]
    return out


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    subprocess.run(["git"] + list(args), cwd=cwd, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def values_from(args):
    return {"__REPO__": args.repo, "__PARTITION__": args.partition, "__ACCOUNT__": args.account,
            "__TIME__": args.time, "__MEM__": args.mem, "__CPUS__": args.cpus}


def make(out, plan_id, no_slurm, values, job_only=False, target="main"):
    if os.path.exists(out) and os.listdir(out):
        raise SystemExit("%s exists and is not empty" % out)
    os.makedirs(out, exist_ok=True)
    for rel, text in FILES.items():
        p = os.path.join(out, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    git(out, "init", "-q")
    git(out, "symbolic-ref", "HEAD", "refs/heads/" + target)
    git(out, "add", "-A")
    git(out, "commit", "-q", "-m", "Initial fixture: two failing tasks and one job")
    tmpl = template(plan_id, no_slurm, job_only, target)
    write_json(os.path.join(out, "plan.template.json"), tmpl)
    write_json(os.path.join(out, "plan.json"), render(tmpl, values))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", metavar="OUT_DIR")
    ap.add_argument("--plan-id", default=None, help="default accept-run1")
    ap.add_argument("--no-slurm", action="store_true", help="omit the slurm block")
    ap.add_argument("--job-only", action="store_true", help="plan with two job units and no code units")
    ap.add_argument("--plan-only", action="store_true", help="re-render plan.json from plan.template.json; no git")
    ap.add_argument("--repo", help="owner/name")
    ap.add_argument("--target", default="main",
                    help="target branch, default main; one branch per run lets runs share one repository")
    ap.add_argument("--partition")
    ap.add_argument("--account")
    ap.add_argument("--time", help="finite walltime, e.g. 00:30:00")
    ap.add_argument("--mem", help="e.g. 8G")
    ap.add_argument("--cpus", type=int)
    a = ap.parse_args(argv)
    if a.plan_id is not None and not PLAN_ID_RE.match(a.plan_id):
        ap.error("--plan-id must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    if not TARGET_RE.match(a.target):
        ap.error("--target must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    out = os.path.abspath(a.out_dir)
    vals = values_from(a)
    if a.plan_only:
        with open(os.path.join(out, "plan.template.json"), encoding="utf-8") as f:
            tmpl = json.load(f)
        if a.plan_id:
            tmpl["plan_id"] = a.plan_id
        if a.no_slurm:
            tmpl.pop("slurm", None)
        write_json(os.path.join(out, "plan.json"), render(tmpl, vals))
    else:
        make(out, a.plan_id or "accept-run1", a.no_slurm, vals, a.job_only, a.target)
    print("wrote %s" % os.path.join(out, "plan.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
