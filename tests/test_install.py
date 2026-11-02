import os
import shutil
import subprocess
import unittest

from helpers import REPO, TmpCase

INSTALL = os.path.join(REPO, "install.sh")
SRC = os.path.join(REPO, "skills", "glab-overseer")
MARKER = ".glab-overseer-install"


def listing(d):
    out = []
    for dp, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        out += [os.path.relpath(os.path.join(dp, f), d) for f in files]
    return sorted(out)


class InstallCase(TmpCase):
    def setUp(self):
        TmpCase.setUp(self)
        self.c = os.path.join(self.tmp, "claude skills")  # a space: paths must be quoted
        self.a = os.path.join(self.tmp, "agy")
        self.tc = os.path.join(self.c, "glab-overseer")
        self.ta = os.path.join(self.a, "glab-overseer")

    def run_install(self, *extra, **kw):
        argv = ["bash", INSTALL] + ([] if kw.get("defaults") else ["--claude-dir", self.c, "--agy-dir", self.a]) + list(extra)
        env = dict(self.env(), HOME=self.tmp)
        return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)

    def stat(self, p):
        s = os.stat(p)
        return (s.st_ino, s.st_mtime_ns)


class InstallTests(InstallCase):
    def test_copies_to_both_roots_with_marker(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        for t in (self.tc, self.ta):
            self.assertTrue(os.path.isfile(os.path.join(t, MARKER)), t)
            self.assertFalse(os.path.islink(os.path.join(t, "SKILL.md")))
            self.assertEqual(listing(t), sorted(listing(SRC) + [MARKER]))
            self.assertIn("install (copy): " + t, r.stdout)
        self.assertEqual(self.cli_py(self.tc).returncode, 0)

    def cli_py(self, target):
        return subprocess.run(["python3", os.path.join(target, "scripts", "overseer.py"), "--help"],
                              capture_output=True, text=True, env=self.env(), timeout=60)

    def test_bytecode_caches_are_not_installed(self):
        co = os.path.join(self.tmp, "checkout")
        shutil.copytree(SRC, os.path.join(co, "skills", "glab-overseer"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(INSTALL, co)
        cache = os.path.join(co, "skills", "glab-overseer", "scripts", "overseer", "__pycache__")
        os.makedirs(cache)
        with open(os.path.join(cache, "x.pyc"), "wb") as f:
            f.write(b"x")
        r = subprocess.run(["bash", os.path.join(co, "install.sh"), "--claude-dir", self.c, "--only", "claude"],
                           capture_output=True, text=True, env=self.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tc, "scripts", "overseer", "__pycache__")))

    def test_defaults_use_home(self):
        r = self.run_install(defaults=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, ".claude", "skills", "glab-overseer", MARKER)))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, ".gemini", "config", "skills", "glab-overseer", MARKER)))

    def test_second_run_changes_nothing(self):
        self.run_install()
        before = dict((p, self.stat(os.path.join(t, p))) for t in (self.tc, self.ta) for p in listing(t))
        r = self.run_install()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.count("unchanged:"), 2, r.stdout)
        after = dict((p, self.stat(os.path.join(t, p))) for t in (self.tc, self.ta) for p in listing(t))
        self.assertEqual(before, after)

    def test_changed_install_is_updated(self):
        self.run_install()
        skill = os.path.join(self.tc, "SKILL.md")
        with open(skill, "a") as f:
            f.write("local edit\n")
        r = self.run_install()
        self.assertIn("update (copy): " + self.tc, r.stdout)
        self.assertIn("unchanged: " + self.ta, r.stdout)
        with open(skill) as f, open(os.path.join(SRC, "SKILL.md")) as g:
            self.assertEqual(f.read(), g.read())

    def test_unmarked_directory_is_refused_and_nothing_is_changed(self):
        os.makedirs(self.ta)
        keep = os.path.join(self.ta, "mine.txt")
        with open(keep, "w") as f:
            f.write("mine")
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("refusing to overwrite " + self.ta, r.stderr)
        self.assertEqual(os.listdir(self.ta), ["mine.txt"])
        self.assertFalse(os.path.exists(self.tc), "the other target must not be touched either")

    def test_symlink_or_symlinked_marker_is_refused(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        os.makedirs(self.a)
        os.symlink(other, self.ta)
        r = self.run_install("--only", "agy")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(os.listdir(other), [])
        os.unlink(self.ta)
        os.makedirs(self.ta)
        with open(os.path.join(other, "m"), "w") as f:
            f.write("x")
        os.symlink(os.path.join(other, "m"), os.path.join(self.ta, MARKER))
        self.assertEqual(self.run_install("--only", "agy").returncode, 1)
        self.assertEqual(self.run_install("--only", "agy", "--uninstall").returncode, 1)
        self.assertTrue(os.path.isdir(self.ta))

    def test_only(self):
        self.assertEqual(self.run_install("--only", "claude").returncode, 0)
        self.assertTrue(os.path.isdir(self.tc))
        self.assertFalse(os.path.exists(self.a))
        self.assertEqual(self.run_install("--only", "bogus").returncode, 2)

    def test_dry_run_writes_nothing(self):
        r = self.run_install("--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertIn("would install (copy): " + self.tc, r.stdout)
        self.assertFalse(os.path.exists(self.c) or os.path.exists(self.a))
        self.run_install()
        skill = os.path.join(self.tc, "SKILL.md")
        with open(skill, "a") as f:
            f.write("x\n")
        before = self.stat(skill)
        r = self.run_install("--dry-run")
        self.assertIn("would update", r.stdout)
        self.assertEqual(self.stat(skill), before)
        r = self.run_install("--uninstall", "--dry-run")
        self.assertIn("would remove: " + self.tc, r.stdout)
        self.assertTrue(os.path.isdir(self.tc) and os.path.isdir(self.ta))

    def test_uninstall_removes_only_marked_directories(self):
        self.run_install("--only", "claude")
        os.makedirs(self.ta)
        with open(os.path.join(self.ta, "mine.txt"), "w") as f:
            f.write("mine")
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.tc))
        self.assertEqual(os.listdir(self.ta), ["mine.txt"])
        self.assertIn("refusing to remove " + self.ta, r.stderr)
        # nothing installed: clean exit
        self.assertEqual(self.run_install("--uninstall", "--only", "claude").returncode, 0)

    def test_link_mode_links_into_the_checkout_and_is_idempotent(self):
        r = self.run_install("--link")
        self.assertEqual(r.returncode, 0, r.stderr)
        for t in (self.tc, self.ta):
            self.assertTrue(os.path.isfile(os.path.join(t, MARKER)))
            self.assertFalse(os.path.islink(t))
            self.assertEqual(os.path.realpath(os.path.join(t, "SKILL.md")), os.path.join(os.path.realpath(SRC), "SKILL.md"))
        r = self.run_install("--link")
        self.assertEqual(r.stdout.count("unchanged:"), 2, r.stdout)
        r = self.run_install()  # switching mode replaces the marked directory with a copy
        self.assertIn("update (copy)", r.stdout)
        self.assertFalse(os.path.islink(os.path.join(self.tc, "SKILL.md")))
        self.assertEqual(self.run_install("--uninstall").returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(SRC, "SKILL.md")), "uninstall must not follow links into the checkout")

    def test_glob_characters_in_a_root_are_literal(self):
        for shell in [x for x in ("bash", "dash") if shutil.which(x)]:
            t = os.path.join(self.tmp, "g" + shell)
            odd, plain = os.path.join(t, "a[1]"), os.path.join(t, "a1")
            for d in (odd, plain):
                r = subprocess.run([shell, INSTALL, "--only", "claude", "--claude-dir", d], capture_output=True, text=True, env=self.env())
                self.assertEqual(r.returncode, 0, r.stderr)
            r = subprocess.run([shell, INSTALL, "--only", "claude", "--claude-dir", odd, "--uninstall"], capture_output=True, text=True, env=self.env())
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(os.path.join(odd, "glab-overseer")), shell)
            self.assertTrue(os.path.isfile(os.path.join(plain, "glab-overseer", MARKER)), shell)

    def test_round2_newline_or_cr_in_a_root_is_refused(self):
        self.run_install("--only", "claude")
        for bad in (self.tc + "\n" + os.path.join(self.tmp, "b"), self.c + "\r", "x\ny"):
            for extra in ((), ("--uninstall",), ("--dry-run",)):
                r = subprocess.run(["bash", INSTALL, "--only", "claude", "--claude-dir", bad] + list(extra),
                                   capture_output=True, text=True, env=self.env())
                self.assertEqual(r.returncode, 2, (bad, extra, r.stdout))
                self.assertIn("newline", r.stderr)
        self.assertTrue(os.path.isdir(self.tc), "an unrelated installation was removed")
        r = subprocess.run(["bash", INSTALL, "--only", "agy", "--agy-dir", "a\nb", "--claude-dir", self.c], capture_output=True, text=True, env=self.env())
        self.assertEqual(r.returncode, 2)

    def test_unknown_flag_and_missing_value(self):
        self.assertEqual(self.run_install("--nope").returncode, 2)
        r = subprocess.run(["bash", INSTALL, "--claude-dir"], capture_output=True, text=True, env=self.env())
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
