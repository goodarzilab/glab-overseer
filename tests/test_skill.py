import json
import os
import re
import subprocess
import sys
import unittest

import helpers
from helpers import ENTRY, REPO, TmpCase

from overseer import review as R

SKILL_DIR = os.path.join(REPO, "skills", "glab-overseer")
REFS = ("plan-format.md", "operating-loop.md", "backends.md", "review.md", "install.md")
COMMANDS = ("survey probe-transport models slurm-handshake plan issues next dispatch wait judge review reproduce "
            "dispositions fix nonvacuous pr merge resolve abort status report unlock").split()
LOOP = "next dispatch wait judge review reproduce dispositions fix nonvacuous pr merge resolve report".split()
ACTIONS = "dispatch wait judge resolve retry review dispositions fix nonvacuous pr merge tracker stop".split()
FOREIGN_FLAGS = {"--json-schema", "--sandbox", "--pty", "--squash", "--match-head-commit", "--force", "--ff-only"}
NESTED = ("plan", "issues")


def read(*parts):
    with open(os.path.join(SKILL_DIR, *parts), encoding="utf-8") as f:
        return f.read()


_HELP = {}


def help_text(*args):
    if args in _HELP:
        return _HELP[args]
    _HELP[args] = _help_text(*args)
    return _HELP[args]


def _help_text(*args):
    r = subprocess.run([sys.executable, ENTRY] + list(args) + ["--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout


def doc_files():
    return [("SKILL.md", read("SKILL.md"))] + [(n, read("references", n)) for n in REFS]


class SkillTests(unittest.TestCase):
    def test_frontmatter_parses(self):
        text = read("SKILL.md")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(m)
        fields = dict(ln.split(": ", 1) for ln in m.group(1).split("\n"))
        self.assertEqual(sorted(fields), ["description", "name"])
        self.assertEqual(fields["name"], "glab-overseer")
        d = fields["description"].lower()
        for word in ("start", "plan", "run", "resume", "pick up", "agents", "claude code", "antigravity"):
            self.assertIn(word, d)

    def test_length_and_references_exist(self):
        text = read("SKILL.md")
        self.assertLessEqual(len(text.splitlines()), 250)
        for r in REFS:
            self.assertTrue(os.path.isfile(os.path.join(SKILL_DIR, "references", r)), r)
        for ref in re.findall(r"references/([a-z-]+\.md)", text):
            self.assertIn(ref, REFS)

    def test_required_topics_are_covered(self):
        text = read("SKILL.md")
        for needle in ("Front door", "Operating loop", "without asking", "stops for the owner", "Models", "UNCERTAIN",
                       "Caps", "Resuming", "cooperative boundary", "Reporting", "Claude Code", "Antigravity",
                       "ANTHROPIC_API_KEY", "login node", "approver", "plan approve", "issues file",
                       "orchestrator-resolved", "same_family_fallback", "report --public"):
            self.assertIn(needle.lower(), text.lower(), needle)
        for c in LOOP:
            self.assertRegex(text, r"overseer\.py %s\b|`%s`" % (c, c))

    def test_every_command_named_exists_in_help(self):
        top = help_text()
        names = set(re.findall(r"^    ([a-z][a-z-]*)\s", top, re.M))
        for c in COMMANDS:
            self.assertIn(c, names, c)
        for fname, text in doc_files():
            for m in re.finditer(r"overseer\.py[ ]+(?:--root[ ]+\S+[ ]+)?([a-z][a-z-]*)(?:[ ]+([a-z][a-z-]*))?", text):
                self.assertIn(m.group(1), names, "%s names unknown command %s" % (fname, m.group(1)))
                if m.group(1) in NESTED and m.group(2):
                    self.assertIn(m.group(2), help_text(m.group(1)), "%s: %s %s" % (fname, m.group(1), m.group(2)))

    def test_every_flag_exists_in_the_right_help(self):
        top = help_text()
        install = subprocess.run(["bash", os.path.join(REPO, "install.sh"), "--help"], capture_output=True, text=True).stdout
        cache = {}

        def helps(cmd, sub):
            key = (cmd, sub)
            if key not in cache:
                args = [cmd] + ([sub] if sub and cmd in NESTED else [])
                cache[key] = help_text(*args) + (help_text(cmd) if len(args) > 1 else "") + top
            return cache[key]
        for fname, text in doc_files():
            for line in text.splitlines():
                for seg in re.split(r"`", line) if "overseer.py" in line and line.count("`") % 2 == 0 else [line]:
                    m = re.search(r"overseer\.py[ ]+(?:--root[ ]+\S+[ ]+)?([a-z][a-z-]*)(?:[ ]+([a-z][a-z-]*))?(.*)", seg)
                    if not m:
                        continue
                    for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", m.group(3)):
                        self.assertIn(flag, helps(m.group(1), m.group(2)), "%s: %s %s" % (fname, m.group(1), flag))
            allowed = set(re.findall(r"--[a-z][a-z-]*", top + install)) | FOREIGN_FLAGS
            for cmd in COMMANDS:
                allowed |= set(re.findall(r"--[a-z][a-z-]*", help_text(cmd)))
                if cmd in NESTED:
                    for sub in re.findall(r"^    ([a-z]+)\s", help_text(cmd), re.M):
                        allowed |= set(re.findall(r"--[a-z][a-z-]*", help_text(cmd, sub)))
            for flag in set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", text)):
                self.assertIn(flag, allowed, "%s names unknown flag %s" % (fname, flag))

    def test_next_actions_in_skill_are_real_controller_actions(self):
        with open(os.path.join(SKILL_DIR, "scripts", "overseer", "controller.py")) as f:
            src = f.read()
        table = re.findall(r"^\| `([a-z]+)`", read("SKILL.md"), re.M)
        self.assertEqual(sorted(table), sorted(ACTIONS))
        for a in table:
            self.assertIn('"%s"' % a, src, a)

    def test_install_doc_flags_match_install_script(self):
        text = read("references", "install.md")
        usage = subprocess.run(["bash", os.path.join(REPO, "install.sh"), "--help"], capture_output=True, text=True).stdout
        for flag in ("--claude-dir", "--agy-dir", "--only", "--link", "--dry-run", "--uninstall"):
            self.assertIn(flag, text)
            self.assertIn(flag, usage)

    def test_dispositions_example_uses_real_rulings(self):
        m = re.search(r"```json\n(\{\"f1\".*?)```", read("references", "review.md"), re.S)
        d = json.loads(m.group(1))
        for v in d.values():
            self.assertIn(v["ruling"], R.RULINGS)
        for ruling in R.RULINGS:
            self.assertIn("`%s`" % ruling, read("references", "review.md"))

    def test_prose_has_no_em_or_en_dashes(self):
        for rel in ("README.md",):
            with open(os.path.join(REPO, rel), encoding="utf-8") as f:
                self.assertNotRegex(f.read(), "[\u2013\u2014]", rel)
        for fname, text in doc_files():
            self.assertNotRegex(text, "[\u2013\u2014]", fname)
        for rel in ("install.sh",):
            with open(os.path.join(REPO, rel), encoding="utf-8") as f:
                self.assertNotRegex(f.read(), "[\u2013\u2014]", rel)

    def test_readme_covers_requirements(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            text = f.read()
        for needle in ("claude", "agy", "gh", "git", "Python 3.9", "./install.sh", "Quick start", "ANTHROPIC_API_KEY",
                       "bills that key instead of your subscription"):
            self.assertIn(needle, text, needle)


class PlanExampleTests(TmpCase):
    def test_plan_example_validates_and_approves(self):
        m = re.search(r"```json\n(.*?)```", read("references", "plan-format.md"), re.S)
        plan = json.loads(m.group(1))
        self.assertEqual(sorted(set(u["kind"] for u in plan["units"])), ["code", "job"])
        path = self.write_plan(plan)
        r = self.cli("plan", "validate", path)
        self.assertEqual((r.returncode, r.stdout.strip(), r.stderr), (0, "plan is valid", ""))
        r = self.cli("plan", "approve", path, "--approver", "Doc Reader")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("issues: 3", r.stdout)


if __name__ == "__main__":
    unittest.main()
