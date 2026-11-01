import json
import os
import subprocess
import sys
import unittest

import helpers
from helpers import TmpCase

from overseer import paths, store

SUBCOMMANDS = (
    "survey probe-transport models plan unlock issues slurm-handshake dispatch status wait abort "
    "resolve judge review reproduce dispositions fix nonvacuous pr merge next report"
).split()
STUBS = [c for c in SUBCOMMANDS if c not in ("plan", "unlock", "survey", "probe-transport", "models")]


class PathsTests(TmpCase):
    def test_safe_relpath(self):
        os.makedirs(os.path.join(self.root, "d"))
        self.assertEqual(paths.safe_relpath(self.root, "d/x.txt"), os.path.join(self.root, "d", "x.txt"))
        for bad in ("/etc/passwd", "../x", "a/../../x", "a\\..\\x", ""):
            with self.assertRaises(paths.UnsafePath, msg=bad):
                paths.safe_relpath(self.root, bad)

    def test_symlink_escape_refused(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.root, "link"))
        with self.assertRaises(paths.UnsafePath):
            paths.safe_relpath(self.root, "link/file")

    def test_check_id(self):
        for good in ("a", "a1_b-c", "0x", "a" * 64):
            paths.check_id(good)
        for bad in ("", "A", "-a", "_a", "a b", "a/b", "a" * 65, "a\n", "..", None):
            with self.assertRaises(paths.InvalidId, msg=repr(bad)):
                paths.check_id(bad)

    def test_find_root_git_toplevel_else_cwd(self):
        sub = os.path.join(self.root, "a", "b")
        os.makedirs(sub)
        self.assertEqual(paths.find_root(sub), sub)  # no git: cwd
        subprocess.run(["git", "init", "-q", self.root], check=True)
        self.assertEqual(paths.find_root(sub), self.root)
        self.assertEqual(paths.find_root(self.root), self.root)

    def test_state_dir_created_with_gitignore_and_git_stays_clean(self):
        subprocess.run(["git", "init", "-q", self.root], check=True)
        self.assertFalse(os.path.exists(paths.state_dir(self.root)))
        store.append_event(self.root, "x")
        store.write_json_atomic(os.path.join(paths.plan_dir(self.root, "p"), "plan.json"), {"a": 1})
        with open(os.path.join(paths.state_dir(self.root), ".gitignore")) as f:
            self.assertEqual(f.read().strip(), "*")
        out = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=self.root,
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout, "")


class StoreTests(TmpCase):
    def test_atomic_write_and_stray_temp(self):
        target = os.path.join(self.root, "d", "t.json")
        store.write_json_atomic(target, {"v": 1})
        stray = os.path.join(self.root, "d", ".t.json.stray.tmp")
        with open(stray, "w") as f:
            f.write("{garbage")
        store.write_json_atomic(target, {"v": 2})
        self.assertEqual(store.read_json(target), {"v": 2})
        self.assertTrue(os.path.exists(stray))  # not ours to delete
        self.assertEqual([n for n in os.listdir(os.path.dirname(target)) if n.endswith(".tmp")],
                         [".t.json.stray.tmp"])

    def test_events_append_only(self):
        store.append_event(self.root, "a", n=1)
        store.append_event(self.root, "b", n=2)
        ev = store.read_events(self.root)
        self.assertEqual([e["kind"] for e in ev], ["a", "b"])
        self.assertIn("time", ev[0])

    def test_lock_records_holder_and_blocks_second(self):
        with store.ProjectLock(self.root, "first") as lk:
            rec = store.read_lock(self.root)
            self.assertEqual((rec["command"], rec["pid"]), ("first", os.getpid()))
            self.assertTrue(rec["host"] and rec["time"])
            with self.assertRaises(store.LockHeld) as cm:
                store.ProjectLock(self.root, "second").acquire()
            self.assertIn("first", str(cm.exception))
        self.assertIsNone(store.read_lock(self.root))
        store.ProjectLock(self.root, "third").acquire().release()

    def test_two_processes_contend(self):
        code = ("import sys; sys.path.insert(0, %r)\n"
                "from overseer.store import ProjectLock\n"
                "with ProjectLock(%r, 'holder-cmd'):\n"
                "    print('ready', flush=True); sys.stdin.readline()\n") % (helpers.SCRIPTS, self.root)
        holder = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            plan = self.write_plan()
            r = self.cli("plan", "approve", plan, "--approver", "tester")
            self.assertEqual(r.returncode, 1)
            self.assertIn("holder-cmd", r.stderr)
            self.assertIn(str(holder.pid), r.stderr)
            self.assertFalse(os.path.exists(os.path.join(paths.plans_dir(self.root), "demo")))
        finally:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=30)
            holder.stdin.close()
            holder.stdout.close()
        self.assertEqual(self.cli("plan", "approve", plan, "--approver", "tester").returncode, 0)

    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def make_dead_holder_lock(self, command="stuck"):
        store.ProjectLock(self.root, command).acquire()
        rec = store.read_lock(self.root)
        rec["pid"] = self.dead_pid()
        with open(paths.lock_path(self.root), "w") as f:
            json.dump(rec, f)

    def test_unlock_refuses_live_local_holder_unless_even_if_alive(self):
        store.ProjectLock(self.root, "busy").acquire()
        r = self.cli("unlock", "--force", "--reason", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("alive", r.stderr)
        self.assertIsNotNone(store.read_lock(self.root))
        self.assertEqual(store.read_events(self.root), [])
        r = self.cli("unlock", "--force", "--reason", "x", "--even-if-alive")
        self.assertEqual(r.returncode, 0, r.stderr)
        ev = store.read_events(self.root)[0]
        self.assertTrue(ev["holder_alive"])
        self.assertTrue(ev["even_if_alive"])

    def test_unlock_dead_or_remote_holder_needs_no_override(self):
        self.make_dead_holder_lock()
        self.assertEqual(self.cli("unlock", "--force", "--reason", "x").returncode, 0)
        ev = store.read_events(self.root)[0]
        self.assertFalse(ev["holder_alive"])
        self.assertFalse(ev["even_if_alive"])
        store.ProjectLock(self.root, "remote").acquire()
        rec = store.read_lock(self.root)
        rec["host"] = "some-other-host"
        with open(paths.lock_path(self.root), "w") as f:
            json.dump(rec, f)
        self.assertEqual(self.cli("unlock", "--force", "--reason", "y").returncode, 0)

    def test_lock_published_with_link_and_no_temp_left(self):
        from unittest import mock
        with mock.patch.object(store.os, "link", wraps=os.link) as link:
            lk = store.ProjectLock(self.root, "linked").acquire()
        self.assertEqual(link.call_count, 1)
        self.assertEqual(store.read_lock(self.root)["command"], "linked")
        with self.assertRaises(store.LockHeld) as cm:
            store.ProjectLock(self.root, "other").acquire()
        self.assertIn("linked", str(cm.exception))
        self.assertNotIn("unknown", str(cm.exception).split("(")[0])
        leftovers = [n for n in os.listdir(paths.state_dir(self.root)) if n.startswith(".lock.")]
        self.assertEqual(leftovers, [])
        lk.release()

    def test_forced_unlocks_are_serialized(self):
        from unittest import mock
        self.make_dead_holder_lock()
        real, nested = store.holder_alive, []
        def probe(holder):
            if holder.get("command") == "unlock":
                return real(holder)
            with self.assertRaises(store.UnlockBusy):
                store.force_unlock(self.root, "second")
            nested.append(1)
            return real(holder)
        with mock.patch.object(store, "holder_alive", side_effect=probe):
            self.assertIsNotNone(store.force_unlock(self.root, "first"))
        self.assertEqual(nested, [1])
        self.assertFalse(os.path.exists(paths.lock_path(self.root) + ".unlock"))
        store.ProjectLock(self.root, "new").acquire()
        with self.assertRaises(store.LockAlive):
            store.force_unlock(self.root, "late")
        self.assertFalse(os.path.exists(paths.lock_path(self.root) + ".unlock"))
        self.assertEqual(store.read_lock(self.root)["command"], "new")

    def write_guard(self, **kw):
        rec = {"command": "unlock", "host": store.socket.gethostname(), "pid": self.dead_pid(),
               "time": store.utc_now(), "token": "t"}
        rec.update(kw)
        with open(paths.lock_path(self.root) + ".unlock", "w") as f:
            json.dump(rec, f)

    def test_stale_local_guard_removed_live_or_remote_refused(self):
        paths.ensure_state_dir(self.root)
        self.write_guard()
        self.assertIsNone(store.force_unlock(self.root, "x"))
        self.assertEqual([e["kind"] for e in store.read_events(self.root)], ["unlock_guard_removed"])
        self.assertFalse(os.path.exists(paths.lock_path(self.root) + ".unlock"))
        for kw in ({"pid": os.getpid()}, {"host": "some-other-host"}):
            self.write_guard(**kw)
            with self.assertRaises(store.UnlockBusy):
                store.force_unlock(self.root, "x")
            self.assertTrue(os.path.exists(paths.lock_path(self.root) + ".unlock"))
            os.unlink(paths.lock_path(self.root) + ".unlock")

    def test_release_only_removes_own_lock(self):
        a = store.ProjectLock(self.root, "a").acquire()
        os.unlink(paths.lock_path(self.root))  # forced away
        b = store.ProjectLock(self.root, "b").acquire()
        a.release()
        self.assertEqual(store.read_lock(self.root)["command"], "b")
        b.release()
        self.assertIsNone(store.read_lock(self.root))

    def test_unlock_force_records_event(self):
        self.make_dead_holder_lock()
        r = self.cli("unlock", "--reason", "crashed")
        self.assertEqual(r.returncode, 1)
        self.assertIsNotNone(store.read_lock(self.root))
        r = self.cli("unlock", "--force")
        self.assertEqual(r.returncode, 1)
        r = self.cli("unlock", "--force", "--reason", "holder crashed")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(store.read_lock(self.root))
        ev = [e for e in store.read_events(self.root) if e["kind"] == "unlock"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["reason"], "holder crashed")
        self.assertEqual(ev[0]["previous_holder"]["command"], "stuck")


class GlobTests(unittest.TestCase):
    def test_glob_match_segments(self):
        m = paths.glob_match
        self.assertTrue(m("src/*.py", "src/a.py"))
        self.assertFalse(m("src/*.py", "src/sub/a.py"))
        self.assertFalse(m("src/a?.py", "src/a/.py"))
        self.assertTrue(m("src/a[bc].py", "src/ab.py"))
        self.assertFalse(m("src/[a-z]*", "src/x/y"))
        self.assertTrue(m("src/**", "src/a/b/c.py"))
        self.assertTrue(m("src/**/c.py", "src/c.py"))
        self.assertTrue(m("src/**/c.py", "src/a/b/c.py"))
        self.assertTrue(m("**/c.py", "c.py"))
        self.assertFalse(m("src/**", "other/a.py"))
        self.assertFalse(m("src/a*", "src"))


class CliTests(TmpCase):
    def test_help_lists_every_subcommand(self):
        r = self.cli("--help", cwd=self.tmp)
        self.assertEqual(r.returncode, 0)
        for c in SUBCOMMANDS:
            self.assertIn(c, r.stdout)

    def test_stubs_exit_2(self):
        for c in STUBS:
            r = self.cli(c, "--whatever", "x")
            self.assertEqual(r.returncode, 2, c)
            self.assertIn("not implemented in this version", r.stderr)

    def test_ci_file_names_both_pythons_and_triggers(self):
        with open(os.path.join(helpers.REPO, ".github", "workflows", "ci.yml")) as f:
            text = f.read()
        for needle in ('"3.9"', '"3.12"', "push:", "pull_request:", "python -m unittest discover -s tests"):
            self.assertIn(needle, text)

    def test_no_private_identifiers_in_tracked_sources(self):
        # Site identifiers (cluster, host and user names) are never written here:
        # committing them would itself break O6. They come from an untracked
        # file named by OVERSEER_PRIVATE_IDENTIFIERS, one per line.
        banned = ["/home/"]
        extra = os.environ.get("OVERSEER_PRIVATE_IDENTIFIERS")
        if extra:
            with open(extra) as f:
                banned += [ln.strip().lower() for ln in f if ln.strip()]
        for base in ("skills", "tests", ".github"):
            for dp, _, files in os.walk(os.path.join(helpers.REPO, base)):
                for n in files:
                    if n.endswith(".pyc") or n == "test_core.py":
                        continue
                    with open(os.path.join(dp, n), errors="replace") as f:
                        text = f.read().lower()
                    for b in banned:
                        self.assertNotIn(b, text, "%s contains %s" % (n, b))


if __name__ == "__main__":
    unittest.main()
