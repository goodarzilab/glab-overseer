import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import ENTRY, TmpCase, plan_copy

from overseer import approval, cli, execution as X, store


def job(uid, command="true"):
    return {"id": uid, "kind": "job", "title": uid, "command": command, "outputs": ["r.txt"], "needs": []}


def make_plan(pid="demo", n=4):
    p = plan_copy()
    p["plan_id"] = pid
    p["caps"] = {"workers": 3, "attempts": 2}
    p["units"] = [job("j%d" % i) for i in range(1, n + 1)]
    return p


class Crash(Exception):
    pass


class Case(TmpCase):
    def setUp(self):
        super().setUp()
        sysbin = os.path.join(self.tmp, "sysbin")
        os.makedirs(sysbin)
        for t in ("sh", "sleep", "env", "cat", "echo", "git"):
            src = shutil.which(t)
            if src:
                os.symlink(src, os.path.join(sysbin, t))
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.penv = {"PATH": os.pathsep.join([self.shims, sysbin]), "HOME": self.tmp, "LC_ALL": "C.UTF-8"}
        patcher = mock.patch.dict(os.environ, self.penv, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.kill_all)
        self.approve("demo")

    def approve(self, pid, n=4):
        approval.approve_plan(self.root, make_plan(pid, n), "me")

    def kill_all(self):
        for i in list(X.iter_invocations(self.root)):
            ln = X._load(os.path.join(i["path"], "launch.json"))
            if isinstance(ln, dict) and isinstance(ln.get("pgid"), int):
                try:
                    os.killpg(ln["pgid"], signal.SIGKILL)
                except OSError:
                    pass
        time.sleep(0.05)

    def script(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, 0o755)
        return p

    def launch(self, uid="j1", argv=None, kind="author", plan="demo", n=None, **kw):
        if n is None:
            n = approval.load_unit_state(self.root, plan, uid)["counters"]["attempts"]
            if n == 0 or kw.pop("new", False):
                n, _ = X.allocate_attempt(self.root, plan, uid)
        return X.launch_invocation(self.root, plan, uid, n, kind, argv or [self.script("sl", "sleep 30")],
                                   self.root, **kw)

    def wait_for(self, inv, states=("EXITED",), timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            st = X.invocation_status(inv, True)
            if st["state"] in states:
                return st
            time.sleep(0.05)
        self.fail("timeout; last status %r" % (X.invocation_status(inv),))

    def rec(self, inv, name):
        with open(os.path.join(inv, name)) as f:
            return json.load(f)


class AttemptTests(Case):
    def test_allocate_caps_and_exclusive(self):
        n, d = X.allocate_attempt(self.root, "demo", "j1")
        self.assertEqual((n, os.path.basename(d)), (1, "a1"))
        self.assertEqual(approval.load_unit_state(self.root, "demo", "j1")["counters"]["attempts"], 1)
        self.assertEqual(X.allocate_attempt(self.root, "demo", "j1")[0], 2)
        with self.assertRaisesRegex(X.ExecutionError, "attempts cap"):
            X.allocate_attempt(self.root, "demo", "j1")
        stray = os.path.join(self.root, ".overseer", "plans", "demo", "units", "j2", "attempts", "a1")
        os.makedirs(stray)  # an existing directory that is not a marked allocation is never reused
        with self.assertRaisesRegex(X.ExecutionError, "already exists"):
            X.allocate_attempt(self.root, "demo", "j2")

    def test_allocation_crash_converges(self):
        with mock.patch.object(approval, "save_unit_state", side_effect=Crash):
            with self.assertRaises(Crash):
                X.allocate_attempt(self.root, "demo", "j1")
        self.assertEqual(approval.load_unit_state(self.root, "demo", "j1")["counters"]["attempts"], 0)
        self.assertEqual(X.allocate_attempt(self.root, "demo", "j1")[0], 1)
        self.assertEqual(approval.load_unit_state(self.root, "demo", "j1")["counters"]["attempts"], 1)

    def test_refused_while_active(self):
        self.launch("j1")
        with self.assertRaisesRegex(X.ExecutionError, "RUNNING"):
            X.allocate_attempt(self.root, "demo", "j1")

    def test_refused_while_uncertain(self):
        with self.assertRaises(Crash):
            self.launch("j1", spawn=mock.Mock(side_effect=Crash))  # intent, no process: UNCERTAIN
        with self.assertRaisesRegex(X.ExecutionError, "UNCERTAIN"):
            X.allocate_attempt(self.root, "demo", "j1")

    def test_quota_needs_flag(self):
        n, _ = X.allocate_attempt(self.root, "demo", "j1")
        X.record_classification(self.root, "demo", "j1", n, "QUOTA")
        with self.assertRaisesRegex(X.ExecutionError, "--after-quota"):
            X.allocate_attempt(self.root, "demo", "j1")
        self.assertEqual(X.allocate_attempt(self.root, "demo", "j1", after_quota=True)[0], 2)

    def test_unreadable_classification_blocks(self):
        n, d = X.allocate_attempt(self.root, "demo", "j1")
        with open(os.path.join(d, "classification.json"), "w") as f:
            f.write("[1]")
        with self.assertRaisesRegex(X.ExecutionError, "unreadable"):
            X.allocate_attempt(self.root, "demo", "j1")

    def test_counter_is_type_checked(self):
        st = approval.load_unit_state(self.root, "demo", "j1")
        st["counters"]["attempts"] = "1"
        approval.save_unit_state(self.root, "demo", "j1", st)
        with self.assertRaisesRegex(X.ExecutionError, "not a non-negative integer"):
            X.allocate_attempt(self.root, "demo", "j1")


class RecordTests(Case):
    def test_intent_launch_start_exit_and_rc(self):
        rec = self.launch("j1", [self.script("three", "echo out; echo err >&2; exit 3")], stdin_text="hi")
        inv = rec["dir"]
        self.assertEqual(os.path.basename(inv), "01-author")
        st = self.wait_for(inv)
        self.assertEqual((st["rc"], st["signal"]), (3, None))
        intent, launch = self.rec(inv, "intent.json"), self.rec(inv, "launch.json")
        start, ex = self.rec(inv, "start.json"), self.rec(inv, "exit.json")
        self.assertEqual(len(intent["token"]), 32)
        self.assertEqual(intent["argv_sha256"], X.argv_digest(intent["argv"]))
        self.assertTrue(os.path.isabs(intent["argv"][0]))
        self.assertEqual({launch["token"], start["token"], ex["token"]}, {intent["token"]})
        self.assertEqual(ex["argv_sha256"], intent["argv_sha256"])
        self.assertEqual((start["pid"], launch["pgid"]), (launch["pid"], launch["pid"]))
        self.assertEqual(launch["start"], X.proc_start(launch["pid"], True) or launch["start"])
        with open(os.path.join(inv, "stdout.log")) as f:
            self.assertEqual(f.read(), "out\n")
        with open(os.path.join(inv, "stderr.log")) as f:
            self.assertEqual(f.read(), "err\n")
        self.assertEqual(self.rec(inv, "intent.json")["kind"], "author")

    def test_exit_zero_after_sleep(self):
        rec = self.launch("j1", [self.script("ok", "sleep 0.3; exit 0")])
        self.assertEqual(X.invocation_status(rec["dir"])["state"], "RUNNING")
        self.assertEqual(self.wait_for(rec["dir"])["rc"], 0)

    def test_payload_sigkill_is_failure_not_success(self):
        rec = self.launch("j1", [self.script("k9", "kill -9 $$")])
        st = self.wait_for(rec["dir"])
        self.assertEqual(st["rc"], 137)

    def test_wrapper_sigkill_is_uncertain(self):
        rec = self.launch("j1")
        os.killpg(rec["launch"]["pgid"], signal.SIGKILL)
        st = self.wait_for(rec["dir"], ("UNCERTAIN",))
        self.assertFalse(os.path.exists(os.path.join(rec["dir"], "exit.json")))
        self.assertIn("no matching exit.json", st["reason"])
        self.assertEqual(X.slots_in_use(self.root), 1)

    def test_pid_reuse_is_not_running(self):
        rec = self.launch("j1")
        ln = self.rec(rec["dir"], "launch.json")
        ln["start"] = "1"
        store.write_json_atomic(os.path.join(rec["dir"], "launch.json"), ln)
        self.assertEqual(X.invocation_status(rec["dir"])["state"], "UNCERTAIN")

    def test_foreign_start_json_pid_is_not_running(self):
        rec = self.launch("j1")
        self.wait_for_file(rec["dir"], "start.json")
        st = self.rec(rec["dir"], "start.json")
        st["pid"] += 1
        store.write_json_atomic(os.path.join(rec["dir"], "start.json"), st)
        self.assertEqual(X.invocation_status(rec["dir"])["state"], "UNCERTAIN")

    def wait_for_file(self, inv, name):
        end = time.time() + 10
        while not os.path.exists(os.path.join(inv, name)) and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(os.path.join(inv, name)))

    def test_stale_or_mismatched_exit_json_is_ignored(self):
        a = self.launch("j1", [self.script("one", "exit 0")])
        self.wait_for(a["dir"])
        n2, _ = X.allocate_attempt(self.root, "demo", "j1")
        with self.assertRaises(Crash):
            self.launch("j1", n=n2, spawn=mock.Mock(side_effect=Crash))
        b = {"dir": [i["path"] for i in X.iter_invocations(self.root) if i["attempt"] == 2][0]}
        shutil.copy(os.path.join(a["dir"], "exit.json"), os.path.join(b["dir"], "exit.json"))
        self.assertIn("does not match", X.invocation_status(b["dir"])["reason"])
        ex = self.rec(a["dir"], "exit.json")
        ex["argv_sha256"] = "0" * 64
        store.write_json_atomic(os.path.join(a["dir"], "exit.json"), ex)
        self.assertEqual(X.invocation_status(a["dir"])["state"], "UNCERTAIN")
        ex.update(argv_sha256=self.rec(a["dir"], "intent.json")["argv_sha256"], token="0" * 32)
        store.write_json_atomic(os.path.join(a["dir"], "exit.json"), ex)  # right digest, other invocation's token
        self.assertEqual(X.invocation_status(a["dir"])["state"], "UNCERTAIN")
        ex.update(token=self.rec(a["dir"], "intent.json")["token"], rc="0")
        store.write_json_atomic(os.path.join(a["dir"], "exit.json"), ex)  # rc must be an int
        self.assertEqual(X.invocation_status(a["dir"])["state"], "UNCERTAIN")

    def test_tampered_intent_digest_is_uncertain(self):
        rec = self.launch("j1")
        it = self.rec(rec["dir"], "intent.json")
        it["argv"] = it["argv"] + ["x"]
        store.write_json_atomic(os.path.join(rec["dir"], "intent.json"), it)
        self.assertEqual(X.invocation_status(rec["dir"])["state"], "UNCERTAIN")

    def test_env_canary_through_run_sh(self):
        canary = {"FAKE_API_KEY": "k", "GH_TOKEN": "t", "ANTHROPIC_BASE_URL": "u", "AWS_PROFILE": "p"}
        with mock.patch.dict(os.environ, canary):
            rec = self.launch("j1", [self.script("pe", "env")], extras={"OVERSEER_OUT": "/x"})
        self.wait_for(rec["dir"])
        with open(os.path.join(rec["dir"], "stdout.log")) as f:
            out = f.read()
        for k in canary:
            self.assertNotIn(k, out)
        self.assertIn("OVERSEER_OUT=/x", out)

    def test_command_must_resolve_absolutely(self):
        with self.assertRaisesRegex(X.ExecutionError, "not found"):
            self.launch("j1", ["no-such-tool"])

    def test_two_invocations_keep_separate_records(self):
        a = self.launch("j1", [self.script("a", "echo A")], kind="author")
        self.wait_for(a["dir"])
        b = self.launch("j1", [self.script("b", "echo B; exit 2")], kind="fixer", n=1)
        self.wait_for(b["dir"])
        self.assertEqual([os.path.basename(x["dir"]) for x in (a, b)], ["01-author", "02-fixer"])
        ta, tb = (self.rec(x["dir"], "intent.json")["token"] for x in (a, b))
        self.assertNotEqual(ta, tb)
        for x, t, rc, out in ((a, ta, 0, "A\n"), (b, tb, 2, "B\n")):
            self.assertEqual(self.rec(x["dir"], "exit.json")["token"], t)
            self.assertEqual(self.rec(x["dir"], "exit.json")["rc"], rc)
            self.assertEqual(self.rec(x["dir"], "start.json")["token"], t)
            with open(os.path.join(x["dir"], "stdout.log")) as f:
                self.assertEqual(f.read(), out)


class CrashTests(Case):
    def test_crash_before_intent_leaves_nothing_active(self):
        with mock.patch.object(X, "_write_scripts", side_effect=Crash):
            with self.assertRaises(Crash):
                self.launch("j1")
        self.assertEqual(X.slots_in_use(self.root), 0)
        rec = self.launch("j1")  # converges: the next sequence number, no duplicate effect
        self.assertEqual(os.path.basename(rec["dir"]), "02-author")

    def test_intent_without_launch_is_uncertain_and_never_relaunched(self):
        marker = os.path.join(self.tmp, "ran")
        spawn = mock.Mock(side_effect=Crash)
        with self.assertRaises(Crash):
            self.launch("j1", [self.script("m", "echo x >> %s" % marker)], spawn=spawn)
        inv = next(X.iter_invocations(self.root))["path"]
        self.assertFalse(os.path.exists(os.path.join(inv, "launch.json")))
        for _ in range(2):
            self.assertEqual(X.invocation_status(inv, True)["state"], "UNCERTAIN")
        self.assertEqual(X.slots_in_use(self.root, True), 1)
        time.sleep(0.2)
        self.assertEqual(spawn.call_count, 1)
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(os.path.join(inv, "start.json")))
        self.assertFalse(os.path.exists(os.path.join(inv, "launch.json")))

    def test_crash_after_spawn_is_reconciled_from_process_table(self):
        def spawn_then_crash(inv, run_sh, envd, cwd):
            X._spawn_local(inv, run_sh, envd, cwd)
            raise Crash
        with self.assertRaises(Crash):
            self.launch("j1", spawn=spawn_then_crash)
        inv = next(X.iter_invocations(self.root))["path"]
        self.assertFalse(os.path.exists(os.path.join(inv, "launch.json")))
        st = X.invocation_status(inv, True)
        self.assertEqual(st["state"], "RUNNING")
        ln = self.rec(inv, "launch.json")
        self.assertTrue(ln["reconciled"])
        self.assertEqual(ln["token"], self.rec(inv, "intent.json")["token"])
        self.assertEqual(X.slots_in_use(self.root), 1)

    def test_dispatch_crash_before_launch_reuses_attempt(self):
        with mock.patch.object(X, "launch_invocation", side_effect=Crash):
            with self.assertRaises(Crash):
                self.run_cli("dispatch", "--unit", "j1")
        self.assertEqual(approval.load_unit_state(self.root, "demo", "j1")["counters"]["attempts"], 1)
        rc, out, err = self.run_cli("dispatch", "--unit", "j1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["attempt"], 1)

    def run_cli(self, *args):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = cli.main(["--root", self.root] + list(args))
        return rc, o.getvalue(), e.getvalue()


class SlotTests(Case):
    def test_cap_counts_across_plans_and_kinds(self):
        self.approve("other", 1)
        self.launch("j1", kind="author")
        self.launch("j2", kind="job")
        self.launch("j1", plan="other", kind="fixer")
        self.assertEqual(X.slots_in_use(self.root), 3)
        with self.assertRaises(X.SlotsFull):
            self.launch("j3")
        self.assertEqual(len([i for i in X.iter_invocations(self.root)]), 3)

    def test_finished_invocation_frees_slot(self):
        for u in ("j1", "j2", "j3"):
            self.launch(u, [self.script("q", "exit 0")])
        for i in list(X.iter_invocations(self.root)):
            self.wait_for(i["path"])
        self.assertEqual(X.slots_in_use(self.root), 0)
        self.launch("j4")


class CommandTests(Case):
    def run_cli(self, *args, **kw):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = cli.main(["--root", self.root] + list(args))
        return rc, o.getvalue(), e.getvalue()

    def test_dispatch_job_sets_overseer_out_and_launcher_exit_keeps_payload(self):
        p = make_plan()
        p["units"][0]["command"] = 'sleep 1; echo "$OVERSEER_OUT" > "$OVERSEER_OUT/o.txt"; exit 3'
        approval.approve_plan(self.root, p, "me")
        r = subprocess.run([sys.executable, ENTRY, "--root", self.root, "dispatch", "--unit", "j1"],
                           env=self.penv, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        inv = next(X.iter_invocations(self.root))["path"]
        self.assertEqual(X.invocation_status(inv)["state"], "RUNNING")  # the launcher is gone
        st = self.wait_for(inv)
        self.assertEqual(st["rc"], 3)
        out = os.path.join(os.path.dirname(os.path.dirname(inv)), "outputs")
        with open(os.path.join(out, "o.txt")) as f:
            self.assertEqual(f.read().strip(), out)

    def test_dispatch_refuses_second_while_running_and_code_units(self):
        p = make_plan()
        p["units"][0]["command"] = "sleep 30"
        approval.approve_plan(self.root, p, "me")
        self.assertEqual(self.run_cli("dispatch", "--unit", "j1")[0], 0)
        rc, _, err = self.run_cli("dispatch", "--unit", "j1")
        self.assertEqual(rc, 1)
        self.assertIn("RUNNING", err)
        code = plan_copy()
        code["plan_id"] = "codeplan"
        approval.approve_plan(self.root, code, "me")
        rc, _, err = self.run_cli("dispatch", "--unit", "alpha")
        self.assertEqual(rc, 1)
        self.assertIn("code unit", err)

    def test_status_json_reports_slots_and_active(self):
        rec = self.launch("j1")
        rc, out, _ = self.run_cli("status", "--json")
        data = json.loads(out)
        self.assertEqual((rc, data["slots_in_use"], data["plans"]["demo"]["workers_cap"]), (0, 1, 3))
        u = data["plans"]["demo"]["units"]["j1"]
        self.assertEqual(u["active"][0]["state"], "RUNNING")
        self.assertEqual((u["attempts"], u["authorization"]["state"]), (1, "approved"))
        self.assertEqual(data["plans"]["demo"]["units"]["j2"]["active"], [])
        rc, out, _ = self.run_cli("status")
        self.assertIn("slots 1/3", out)
        self.assertIn(rec["seq"], out)

    def test_wait_exit_codes(self):
        rec = self.launch("j1", [self.script("short", "sleep 0.6")])
        self.assertEqual(self.run_cli("wait", "--timeout", "0.3", "--poll", "0.1")[0], 3)
        rc, out, _ = self.run_cli("wait", "--timeout", "10", "--poll", "0.1")
        self.assertEqual(rc, 0)
        line = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((line["from"], line["to"], line["rc"], line["unit"]), ("RUNNING", "EXITED", 0, "j1"))
        self.assertEqual(self.run_cli("wait", "--timeout", "0.2")[0], 0)  # nothing active

    def test_abort_cooperative(self):
        rec = self.launch("j1")
        self.wait_for_start(rec["dir"])
        rc, out, _ = self.run_cli("abort", "j1", "--grace", "5")
        self.assertEqual(rc, 0, out)
        st = X.invocation_status(rec["dir"])
        self.assertEqual((st["state"], st["signal"]), ("EXITED", "TERM"))
        self.assertEqual(X.slots_in_use(self.root), 0)
        self.assertEqual(self.run_cli("abort", "j1")[1].strip(), "nothing to abort for j1")

    def wait_for_start(self, inv):
        end = time.time() + 10
        while not os.path.exists(os.path.join(inv, "start.json")) and time.time() < end:
            time.sleep(0.05)

    def test_abort_escalates_and_frees_slot_only_after_confirmation(self):
        ready = os.path.join(self.tmp, "ready")
        rec = self.launch("j1", [self.script("stubborn", "trap '' TERM; echo > %s; sleep 30" % ready)])
        while not os.path.exists(ready):  # TERM must arrive after the payload ignores it
            time.sleep(0.02)
        seen = []

        def spy(_):
            seen.append((X.slots_in_use(self.root), self.rec(rec["dir"], "cancel.json").get("confirmed_at")))
            time.sleep(0.1)

        res = X.abort_invocation(self.root, rec["dir"], grace=0.5, sleep=spy)
        self.assertEqual(res, "CANCELLED")
        self.assertTrue(seen and all(s == (1, None) for s in seen))  # requested, still holding the slot
        self.assertEqual(X.slots_in_use(self.root), 0)
        cancel = self.rec(rec["dir"], "cancel.json")
        self.assertLessEqual(cancel["requested_at"], cancel["confirmed_at"])
        kinds = [e["kind"] for e in store.read_events(self.root) if e["kind"].startswith("cancel_")]
        self.assertEqual(kinds, ["cancel_requested", "cancel_confirmed"])
        self.assertFalse(os.path.exists(os.path.join(rec["dir"], "exit.json")))  # SIGKILL skips the trap

    def test_unconfirmed_abort_keeps_slot(self):
        rec = self.launch("j1")
        with mock.patch.object(X, "identified_alive", return_value=True), \
                mock.patch.object(X, "_signal"):
            self.assertEqual(X.abort_invocation(self.root, rec["dir"], grace=0.1, sleep=lambda s: None, confirm_wait=0.2), "UNCONFIRMED")
        self.assertNotIn("confirmed_at", self.rec(rec["dir"], "cancel.json"))
        self.assertEqual(X.slots_in_use(self.root), 1)


if __name__ == "__main__":
    unittest.main()
