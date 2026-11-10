import base64
import hashlib
import json
import os
import re
import subprocess
import unittest

from helpers import TmpCase, plan_copy

from overseer import approval, judge, paths, report, store

HOST = "node-canary-7.example.invalid"
HOME = "/ho" + "me/canaryuser/proj"  # built at runtime so the tracked-file scan stays clean
TOKEN = "sk-ant-" + "A" * 30
APPROVER = "Canary Person"
ACCOUNT = "lab-account"
SECRETS = (HOST, HOME, "canaryuser", TOKEN, APPROVER, ACCOUNT, "batch-cpu", "secret_note")
CAND, BASE, MERGE, IBASE = "a" * 40, "b" * 40, "c" * 40, "d" * 40
TOKEN_ID = "tok1"


def wj(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def git(wt, *a):
    return subprocess.run(["git", "-C", wt, "-c", "user.name=t", "-c", "user.email=t@localhost"] + list(a),
                          capture_output=True, text=True, check=True).stdout.strip()


class ReportCase(TmpCase):
    def setUp(self):
        TmpCase.setUp(self)
        plan = plan_copy()
        plan["repo"] = "lab-org/demo-repo"
        approval.approve_plan(self.root, plan, APPROVER)
        self.pid = "demo"
        wj(os.path.join(paths.state_dir(self.root), "survey.json"), {"host": HOST, "path": HOME})
        store.append_event(self.root, "canary", note=TOKEN, where=HOME)

    def adir(self, uid, n=1):
        return paths.attempt_dir(self.root, self.pid, uid, n)

    def unit_state(self, uid, **kw):
        st = approval.load_unit_state(self.root, self.pid, uid)
        for k, v in kw.items():
            st[k] = v
        approval.save_unit_state(self.root, self.pid, uid, st)

    def inv(self, uid, kind, argv, backend="local"):
        d = os.path.join(self.adir(uid), "inv", "01-" + kind)
        wj(os.path.join(d, "intent.json"), {"token": TOKEN_ID, "kind": kind, "argv": argv, "backend": backend, "cwd": HOME})
        return d

    def make_alpha(self):
        """alpha: merged, one review round with a same-family fallback, one dismissed finding."""
        a = self.adir("alpha")
        wt = os.path.join(a, "worktree")
        os.makedirs(os.path.join(wt, "src"))
        git(wt, "init", "-q")
        for name, text in (("src/alpha.py", "print(1)\n"), ("src/alpha_data.txt", "x\n")):
            with open(os.path.join(wt, name), "w") as f:
                f.write(text)
        git(wt, "add", ".")
        git(wt, "commit", "-q", "-m", "alpha")
        self.cand = git(wt, "rev-parse", "HEAD")
        self.alpha_py = "print(1)\n"
        wj(os.path.join(a, "workspace.json"), {"base": BASE, "worktree": wt, "branch": "b"})
        self.inv("alpha", "author", ["/x/bin/claude", "-p", "--permission-mode", "auto", "--model", "sonnet"])
        wj(os.path.join(a, "judgment.json"), {
            "status": "DONE", "base": BASE, "candidate": self.cand, "test_rc": 0, "orchestrator_resolved": False,
            "secret_note": TOKEN, "reasons": ["see " + HOME],
            "outputs": [{"path": "src/alpha.py", "ok": True, "problems": [], "size": 9,
                         "sha256": hashlib.sha256(b"print(1)\n").hexdigest()},
                        {"path": "src/alpha_data.txt", "ok": True, "problems": [], "size": 2,
                         "sha256": hashlib.sha256(b"x\n").hexdigest()},
                        {"path": "/" + "etc/passwd", "ok": True, "problems": []}]})
        wj(os.path.join(a, "integration.json"), {"integration_base": IBASE})
        r1 = os.path.join(a, "reviews", "r1")
        wj(os.path.join(r1, "round.json"), {
            "round": 1, "candidate": self.cand, "reviewer_family": "google", "model": "gemini-3-flash-high",
            "state": "complete", "verdict": "FAIL", "same_family_fallback": True, "fallback_reason": TOKEN,
            "calls": [{"family": "google", "model": "gemini-3-flash-high", "state": "failed"},
                      {"family": "claude", "model": "haiku", "state": "ok"}]})
        wj(os.path.join(r1, "review.json"), {"verdict": "FAIL", "findings": [
            {"id": "f1", "severity": "blocking", "description": HOME}]})
        wj(os.path.join(r1, "dispositions.json"), {"dispositions": {"f1": {"ruling": "dismissed", "evidence": [], "note": HOME}}})
        wj(os.path.join(paths.unit_dir(self.root, self.pid, "alpha"), "pr.json"),
           {"number": 5, "head_oid": self.cand, "candidate": self.cand, "push": HOME})
        wj(os.path.join(paths.unit_dir(self.root, self.pid, "alpha"), "merge.json"),
           {"state": "merged", "number": 5, "candidate": self.cand, "pr_head": self.cand, "merge_commit": MERGE,
            "integration_base": IBASE})
        ip = os.path.join(paths.plan_dir(self.root, self.pid), "issues.json")
        with open(ip) as f:
            rec = json.load(f)
        wj(ip, dict(rec, filed={"alpha": 11, "beta": 12, "crunch": 13}))
        self.unit_state("alpha", counters={"attempts": 1, "review_rounds": 1},
                        closure={"state": "merged", "pr": 5, "merge_commit": MERGE},
                        tracker={"issue": 11, "state": "closed"})

    def make_crunch(self, status):
        a = self.adir("crunch")
        self.inv("crunch", "job", ["/x/bin/sh", "-c", "true"], backend="slurm")
        j = {"status": status, "kind": "job", "base": None, "candidate": None, "test_rc": None, "invocation_token": TOKEN_ID,
             "orchestrator_resolved": status == "DONE", "reasons": [HOME], "outputs": []}
        if status == "DONE":
            os.makedirs(os.path.join(a, "outputs"))
            with open(os.path.join(a, "outputs", "result.txt"), "w") as f:
                f.write("hello\n")
            j["outputs"] = [{"path": "result.txt", "ok": True, "problems": [], "size": 6,
                             "sha256": hashlib.sha256(b"hello\n").hexdigest()}]
        wj(os.path.join(a, "judgment.json"), j)
        self.unit_state("crunch", counters={"attempts": 1, "review_rounds": 0}, tracker={"issue": 13, "state": "closed"})

    def strip_recorded(self, uid):
        j = os.path.join(self.adir(uid), "judgment.json")
        with open(j) as f:
            d = json.load(f)
        for o in d["outputs"]:
            o.pop("size", None), o.pop("sha256", None)
        wj(j, d)

    def build(self, public=True):
        return report.build(self.root, public)

    def units(self, rep):
        return dict((u["id"], u) for p in rep["plans"] for u in p["units"])


class PublicAllowlistTests(ReportCase):
    def test_canary_values_never_reach_the_public_report(self):
        self.make_alpha()
        self.make_crunch("UNCERTAIN")
        out = os.path.join(self.tmp, "out")
        r = self.cli("report", "--public", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("report.md", "report.json"):
            with open(os.path.join(out, name)) as f:
                text = f.read()
            for s in SECRETS:
                self.assertNotIn(s, text, "%s leaked %s" % (name, s))
        # the same values are in the private report: the canary is real
        priv = os.path.join(self.tmp, "priv")
        self.assertEqual(self.cli("report", "--out", priv).returncode, 0)
        with open(os.path.join(priv, "report.json")) as f:
            ptext = f.read()
        for s in (HOST, HOME, TOKEN, APPROVER, ACCOUNT, "secret_note"):
            self.assertIn(s, ptext, s)

    def test_public_fields(self):
        self.make_alpha()
        self.make_crunch("UNCERTAIN")
        u = self.units(self.build())
        a = u["alpha"]
        self.assertEqual(a["state"], "merged")
        self.assertEqual(a["title"], "Add alpha")
        self.assertEqual(a["issue"], {"number": 11, "url": "https://github.com/lab-org/demo-repo/issues/11"})
        self.assertEqual(a["pr"]["url"], "https://github.com/lab-org/demo-repo/pull/5")
        self.assertEqual(a["merge"]["merge_commit"], MERGE)
        self.assertEqual(a["merge"]["integration_base"], IBASE)
        at = a["attempts"][0]
        self.assertEqual((at["base"], at["integration_base"], at["backends"]), (BASE, IBASE, ["local"]))
        self.assertEqual(at["workers"], [{"family": "claude", "model": "sonnet"}])
        self.assertEqual(at["judgment"]["candidate"], self.cand)
        self.assertEqual(at["judgment"]["test_rc"], 0)
        rv = at["reviews"][0]
        self.assertEqual((rv["verdict"], rv["same_family_fallback"], rv["reviewer_family"], rv["reviewer_model"]),
                         ("FAIL", True, "claude", "haiku"))
        self.assertEqual((rv["findings"], rv["dispositions"]), (1, {"dismissed": 1}))
        outs = dict((o["path"], o) for o in at["outputs"])
        self.assertEqual(outs["src/alpha.py"]["size"], len(self.alpha_py))
        self.assertEqual(outs["src/alpha.py"]["sha256"], hashlib.sha256(self.alpha_py.encode()).hexdigest())
        self.assertEqual(u["crunch"]["attempts"][0]["backends"], ["slurm"])

    def test_untrusted_values_are_dropped_not_copied(self):
        self.make_alpha()
        j = os.path.join(self.adir("alpha"), "judgment.json")
        with open(j) as f:
            d = json.load(f)
        d.update(candidate="../../" + HOME, status="DONE; " + TOKEN, test_rc=TOKEN)
        wj(j, d)
        self.inv("alpha", "author", ["/x/bin/claude", "--model", TOKEN])
        rep = self.build()
        text = json.dumps(rep)
        self.assertEqual(self.units(rep)["alpha"]["attempts"][0]["workers"], [{"family": "claude", "model": None}])
        for s in SECRETS:
            self.assertNotIn(s, text)
        jj = self.units(rep)["alpha"]["attempts"][0]["judgment"]
        self.assertEqual((jj["candidate"], jj["status"], jj["test_rc"]), (None, None, None))
        # an absolute output path is dropped and reported
        self.assertIn({"unit": "alpha", "item": "an output record has an invalid path"}, rep["incomplete"])


class UsageTests(ReportCase):
    def test_token_usage_is_summed_per_role_and_family_and_unknown_is_counted_not_zeroed(self):
        self.make_alpha()
        inv = os.path.join(self.adir("alpha"), "inv", "01-author")
        with open(os.path.join(inv, "stdout.log"), "w") as f:  # claude -p event array; the result event carries usage
            json.dump([{"type": "system"}, {"type": "result", "usage": {
                "input_tokens": 6, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 40, "output_tokens": 7,
                "service_tier": TOKEN}}], f)
        fx = os.path.join(self.adir("alpha"), "inv", "02-fixer")
        wj(os.path.join(fx, "intent.json"), {"token": TOKEN_ID, "kind": "fixer", "argv": ["/x/bin/claude", "--model", "sonnet"],
                                             "backend": "local", "cwd": HOME})
        with open(os.path.join(fx, "stdout.log"), "w") as f:
            f.write("not json " + HOME)  # a crashed fixer: unknown, never zero
        r1 = os.path.join(self.adir("alpha"), "reviews", "r1")
        with open(os.path.join(r1, "round.json")) as f:
            rd = json.load(f)
        rd["calls"][0]["dir"], rd["calls"][1]["dir"] = "c1", "c2"
        wj(os.path.join(r1, "round.json"), rd)
        wj(os.path.join(r1, "c1", "stdout.txt"), {"status": "SUCCESS", "usage": {
            "input_tokens": 50, "output_tokens": 5, "cache_read_tokens": 30, "thinking_tokens": 2, "total_tokens": 55}})
        wj(os.path.join(r1, "c2", "stdout.txt"), {"type": "result", "usage": {"input_tokens": 1, "output_tokens": -1}})
        rep = self.build()
        alpha = self.units(rep)["alpha"]
        self.assertEqual(alpha["attempts"][0]["author_usage"], {"calls": 1, "unknown_calls": 1, "input": 6, "cache_read": 100,
                                                                "cache_write": 40, "output": 7})
        self.assertEqual(alpha["attempts"][0]["reviews"][0]["usage"], {"calls": 1, "unknown_calls": 1, "input": 50,
                                                                       "cache_read": 30, "cache_write": 0, "output": 5})
        rows = {(r["role"], r["family"]): r for r in rep["plans"][0]["usage"]}
        self.assertEqual((rows[("author", "claude")]["input"], rows[("author", "claude")]["unknown_calls"]), (6, 1))
        md = report.render(rep)
        self.assertIn("| author | claude | 1 | 6 | 100 | 40 | 7 | 1 |", md)
        self.assertNotIn(TOKEN, json.dumps(rep))
        self.assertNotIn(HOME, json.dumps(rep))

    def test_each_call_counts_for_its_own_family_and_odd_records_are_unknown(self):
        self.make_alpha()
        with open(os.path.join(self.adir("alpha"), "inv", "01-author", "stdout.log"), "w") as f:
            json.dump({"type": "result", "usage": {"input_tokens": 6, "output_tokens": 1}}, f)
        fx = os.path.join(self.adir("alpha"), "inv", "02-fixer")
        wj(os.path.join(fx, "intent.json"), {"token": TOKEN_ID, "kind": "fixer", "argv": ["/x/bin/agy", "--model", "gemini-4-argon-high"],
                                             "backend": "local", "cwd": HOME})
        with open(os.path.join(fx, "stdout.log"), "w") as f:
            f.write("[" * 5000 + "]" * 5000)  # nesting deep enough to exhaust the parser: unknown, and the report survives
        r1 = os.path.join(self.adir("alpha"), "reviews", "r1")
        with open(os.path.join(r1, "round.json")) as f:
            rd = json.load(f)
        rd["calls"][0]["dir"] = "c1"  # google call with usage; the claude fallback call has no directory recorded
        wj(os.path.join(r1, "round.json"), rd)
        wj(os.path.join(r1, "c1", "stdout.txt"), {"usage": {"input_tokens": 50, "output_tokens": 5, "cache_read_tokens": 3,
                                                            "cache_creation_input_tokens": 9}})  # field names follow the family
        os.makedirs(os.path.join(r1, "c2"))
        os.mkfifo(os.path.join(r1, "c2", "stdout.txt"))  # never opened: a FIFO could block the report
        rd["calls"][1]["dir"] = "c2"
        wj(os.path.join(r1, "round.json"), rd)
        rep = self.build()
        rows = {(r["role"], r["family"]): (r["calls"], r["unknown_calls"], r["input"]) for r in rep["plans"][0]["usage"]}
        self.assertEqual(rows, {("author", "claude"): (1, 0, 6), ("author", "google"): (0, 1, 0),
                                ("review", "google"): (1, 0, 50), ("review", "claude"): (0, 1, 0)})
        g = next(r for r in rep["plans"][0]["usage"] if (r["role"], r["family"]) == ("review", "google"))
        self.assertEqual((g["cache_read"], g["cache_write"]), (3, 0))

    def test_usage_without_a_known_family_or_with_string_counts_is_unknown(self):
        p = os.path.join(self.tmp, "out.json")
        wj(p, {"type": "result", "usage": {"input_tokens": 6, "output_tokens": 1, "cache_read_tokens": 3}})
        self.assertEqual(report._usage_of(p, "google"), {"input": 6, "output": 1, "cache_read": 3})
        for fam in (None, [], "mixed"):
            self.assertIsNone(report._usage_of(p, fam))
        wj(p, {"type": "result", "usage": {"input_tokens": "6", "output_tokens": 1}})
        self.assertIsNone(report._usage_of(p, "claude"))
        by = {}
        report._add_by_family(by, [], None)  # an unhashable recorded family is unknown, not a crash
        self.assertEqual(by["unknown"]["unknown_calls"], 1)


class StateTests(ReportCase):
    def test_incomplete_items_list_uncertain_and_pending(self):
        self.make_alpha()
        self.make_crunch("UNCERTAIN")
        rep = self.build()
        u = self.units(rep)
        self.assertEqual((u["beta"]["state"], u["crunch"]["state"]), ("pending", "UNCERTAIN"))
        self.assertIn({"unit": "crunch", "item": "state UNCERTAIN"}, rep["incomplete"])
        self.assertIn({"unit": "beta", "item": "state pending"}, rep["incomplete"])
        self.assertFalse([i for i in rep["incomplete"] if i["unit"] == "alpha" and i["item"].startswith("state")])
        self.assertFalse([i for i in rep["incomplete"] if i["unit"] is None])

    def test_done_job_output_has_size_and_digest(self):
        self.make_crunch("DONE")
        c = self.units(self.build())["crunch"]
        self.assertEqual(c["state"], "done")
        o = c["attempts"][0]["outputs"][0]
        self.assertEqual((o["path"], o["ok"], o["size"]), ("result.txt", True, 6))
        self.assertEqual(o["sha256"], hashlib.sha256(b"hello\n").hexdigest())
        self.assertTrue(c["attempts"][0]["judgment"]["orchestrator_resolved"])
        self.assertEqual(c["facets"]["closure"], "closed")  # closed by the DONE judgment; the stored facet is still open

    def test_job_closure_stays_open_until_done(self):
        self.make_crunch("FAILED")
        c = self.units(self.build())["crunch"]
        self.assertEqual((c["state"], c["facets"]["closure"]), ("failed", "open"))

    def test_unreadable_record_is_incomplete_not_skipped(self):
        self.make_alpha()
        with open(os.path.join(self.adir("alpha"), "judgment.json"), "w") as f:
            f.write("{not json")
        rep = self.build()
        self.assertIn({"unit": "alpha", "item": "judgment unreadable"}, rep["incomplete"])

    def test_changed_plan_digest_is_reported(self):
        p = os.path.join(paths.plan_dir(self.root, self.pid), "plan.json")
        with open(p) as f:
            d = json.load(f)
        d["scope"].append("extra/**")
        wj(p, d)
        rep = self.build()
        self.assertTrue(any("approval is invalid" in i["item"] for i in rep["incomplete"]), rep["incomplete"])

    def test_output_without_computable_digest_is_incomplete(self):
        self.make_alpha()
        self.strip_recorded("alpha")  # an older judgment, written before size and digest were recorded
        os.rename(os.path.join(self.adir("alpha"), "worktree"), os.path.join(self.tmp, "gone"))
        rep = self.build()
        self.assertTrue(any("size and digest unavailable" in i["item"] for i in rep["incomplete"]), rep["incomplete"])


class MissingPlanTests(ReportCase):
    def test_plan_without_plan_json_is_reported_with_its_uncertain_units(self):
        self.make_crunch("UNCERTAIN")
        os.unlink(os.path.join(paths.plan_dir(self.root, self.pid), "plan.json"))
        for public in (True, False):
            rep = self.build(public)
            self.assertEqual([(p["plan_id"], p["units"]) for p in rep["plans"]], [(self.pid, [])])
            self.assertIn("plan.json", rep["plans"][0]["problem"])
            self.assertIn({"unit": "crunch", "item": "state UNCERTAIN"}, rep["incomplete"])
            self.assertTrue(any(i["unit"] is None and "plan.json" in i["item"] for i in rep["incomplete"]))
        out = os.path.join(self.tmp, "o")
        self.assertEqual(self.cli("report", "--public", "--out", out).returncode, 0)
        with open(os.path.join(out, "report.md")) as f:
            md = f.read()
        self.assertIn("plan.json", md)
        self.assertIn("crunch: state UNCERTAIN", md)

    def test_unreadable_plan_json_is_reported_too(self):
        self.make_crunch("UNCERTAIN")
        with open(os.path.join(paths.plan_dir(self.root, self.pid), "plan.json"), "w") as f:
            f.write("{nope")
        rep = self.build()
        self.assertEqual([p["plan_id"] for p in rep["plans"]], [self.pid])
        self.assertIn({"unit": "crunch", "item": "state UNCERTAIN"}, rep["incomplete"])

    def test_failed_and_unjudged_units_are_listed_without_a_plan(self):
        self.make_crunch("FAILED")
        os.unlink(os.path.join(paths.plan_dir(self.root, self.pid), "plan.json"))
        self.assertIn({"unit": "crunch", "item": "state FAILED"}, self.build()["incomplete"])


class JobOutputJudgmentTests(ReportCase):
    def test_changed_output_reports_judged_digest_and_is_incomplete(self):
        self.make_crunch("DONE")
        open(os.path.join(self.adir("crunch"), "outputs", "result.txt"), "w").close()
        rep = self.build()
        o = self.units(rep)["crunch"]["attempts"][0]["outputs"][0]
        self.assertEqual((o["ok"], o["size"], o["sha256"]), (True, 6, hashlib.sha256(b"hello\n").hexdigest()))
        self.assertTrue(o["changed_since_judgment"])
        self.assertEqual((o["current_size"], o["current_sha256"]), (0, hashlib.sha256(b"").hexdigest()))
        self.assertIn({"unit": "crunch", "item": "output result.txt changed since judgment"}, rep["incomplete"])

    def test_unchanged_output_is_not_flagged(self):
        self.make_crunch("DONE")
        rep = self.build()
        o = self.units(rep)["crunch"]["attempts"][0]["outputs"][0]
        self.assertFalse(o["changed_since_judgment"])
        self.assertFalse([i for i in rep["incomplete"] if i["unit"] == "crunch"])

    def test_judgment_without_a_recorded_digest_is_incomplete(self):
        self.make_crunch("DONE")
        j = os.path.join(self.adir("crunch"), "judgment.json")
        with open(j) as f:
            d = json.load(f)
        d["outputs"] = [{"path": "result.txt", "ok": True, "problems": []}]
        wj(j, d)
        rep = self.build()
        o = self.units(rep)["crunch"]["attempts"][0]["outputs"][0]
        self.assertEqual((o["size"], o["sha256"]), (None, None))
        self.assertIn({"unit": "crunch", "item": "output result.txt has no size and digest recorded in its judgment"}, rep["incomplete"])

    def test_judge_records_size_and_digest_of_job_outputs(self):
        adir = os.path.join(self.tmp, "a1")
        os.makedirs(os.path.join(adir, "outputs"))
        with open(os.path.join(adir, "outputs", "r.txt"), "wb") as f:
            f.write(b"abc")
        r = judge.job_outputs(adir, {"outputs": ["r.txt"]})[0]
        self.assertEqual((r["ok"], r["size"], r["sha256"]), (True, 3, hashlib.sha256(b"abc").hexdigest()))


class PrivateCompletenessTests(ReportCase):
    def put(self, rel, data):
        p = os.path.join(paths.state_dir(self.root), rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def files(self):
        return self.build(False)["private"]["files"]

    def test_every_regular_file_is_listed_with_size_and_digest(self):
        self.make_alpha()
        self.put("plans/demo/units/alpha/attempts/a1/inv/01-author/stdout.log", b"unique-worker-log-canary\n")
        self.put("extra/notes.txt", b"plain text")
        self.put("extra/blob.bin", b"\xff\xfe\x00\x80")
        want = set()
        base = paths.state_dir(self.root)
        for d, dirs, names in os.walk(base):
            dirs[:] = [x for x in dirs if x != "report"]
            want |= set(os.path.relpath(os.path.join(d, n), base) for n in names)
        got = self.files()
        self.assertEqual(set(got), want)
        for rel, e in got.items():
            with open(os.path.join(base, rel), "rb") as f:
                data = f.read()
            self.assertEqual((e["size"], e["sha256"]), (len(data), hashlib.sha256(data).hexdigest()), rel)
        log = got["plans/demo/units/alpha/attempts/a1/inv/01-author/stdout.log"]
        self.assertEqual(log["text"], "unique-worker-log-canary\n")
        self.assertEqual(got["extra/notes.txt"]["text"], "plain text")
        self.assertEqual(got["survey.json"]["json"], {"host": HOST, "path": HOME})
        b = got["extra/blob.bin"]
        self.assertEqual((b["encoding"], base64.b64decode(b["content"])), ("base64", b"\xff\xfe\x00\x80"))
        self.assertNotIn("text", b)

    def test_symlinks_are_listed_not_followed(self):
        target = os.path.join(self.tmp, "outside.txt")
        with open(target, "w") as f:
            f.write("outside-canary")
        os.symlink(target, os.path.join(paths.state_dir(self.root), "lnk"))
        got = self.files()
        self.assertEqual(got["lnk"]["symlink"], target)
        self.assertNotIn("outside-canary", json.dumps(got))

    def test_public_report_has_no_private_section(self):
        self.put("extra/notes.txt", b"plain text")
        self.assertNotIn("private", self.build(True))


class Round2ReportTests(ReportCase):
    def put(self, rel, data):
        p = os.path.join(paths.state_dir(self.root), rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_f3_merged_unit_with_missing_judgment_is_incomplete(self):
        self.make_alpha()
        os.unlink(os.path.join(self.adir("alpha"), "judgment.json"))
        for public in (True, False):
            rep = self.build(public)
            self.assertEqual(self.units(rep)["alpha"]["state"], "merged")
            self.assertIn({"unit": "alpha", "item": "judgment missing"}, rep["incomplete"])

    def test_f3_intact_merged_unit_has_no_judgment_item(self):
        self.make_alpha()
        self.assertNotIn({"unit": "alpha", "item": "judgment missing"}, self.build()["incomplete"])

    def test_f4_orphan_done_code_unit_that_is_not_merged_is_incomplete(self):
        self.make_alpha()
        self.unit_state("alpha", closure={"state": "open"}, tracker={"issue": 11, "state": "open"})
        os.unlink(os.path.join(paths.plan_dir(self.root, self.pid), "plan.json"))
        rep = self.build()
        self.assertIn({"unit": "alpha", "item": "state DONE, not merged"}, rep["incomplete"])

    def test_f4_orphan_merged_code_unit_and_done_job_are_complete(self):
        self.make_alpha()
        self.make_crunch("DONE")
        os.unlink(os.path.join(paths.plan_dir(self.root, self.pid), "plan.json"))
        rep = self.build()
        self.assertFalse([i for i in rep["incomplete"] if i["unit"] in ("alpha", "crunch")], rep["incomplete"])

    def test_f5_private_report_inlines_whole_large_and_binary_files(self):
        n = 300000
        log = b"A" * n + b"unique-middle-traceback" + b"B" * n
        blob = bytes(range(256)) * 3000
        self.put("extra/big.log", log)
        self.put("extra/blob.bin", blob)
        self.put("extra/big.json", json.dumps({"k": "v" * 700000}).encode())
        got = self.build(False)["private"]["files"]
        e = got["extra/big.log"]
        self.assertEqual(e["text"], log.decode())
        self.assertNotIn("truncated", e)
        self.assertEqual((e["size"], e["sha256"]), (len(log), hashlib.sha256(log).hexdigest()))
        b = got["extra/blob.bin"]
        self.assertEqual((b["encoding"], base64.b64decode(b["content"])), ("base64", blob))
        self.assertEqual(got["extra/big.json"]["json"], {"k": "v" * 700000})

    def test_f5_worktree_files_are_listed_only_and_report_md_says_so(self):
        self.make_alpha()
        wt = os.path.join("plans", self.pid, "units", "alpha", "attempts", "a1", "worktree")
        got = self.build(False)["private"]["files"]
        e = got[os.path.join(wt, "src", "alpha.py")]
        self.assertEqual(e["listed_only"], True)
        self.assertEqual(e["sha256"], hashlib.sha256(b"print(1)\n").hexdigest())
        self.assertNotIn("text", e)
        self.assertTrue([k for k in got if os.sep + ".git" + os.sep in k and got[k].get("listed_only")])
        out = os.path.join(self.tmp, "o")
        self.assertEqual(self.cli("report", "--out", out).returncode, 0)
        with open(os.path.join(out, "report.md")) as f:
            md = f.read()
        self.assertIn("listed with size and digest only", md)
        self.assertEqual(self.cli("report", "--public", "--out", os.path.join(self.tmp, "p")).returncode, 0)
        with open(os.path.join(self.tmp, "p", "report.md")) as f:
            self.assertNotIn("listed with size and digest only", f.read())


class Round3ReportTests(ReportCase):
    def put(self, rel, data):
        p = os.path.join(paths.state_dir(self.root), rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def wtrel(self, uid="alpha"):
        return os.path.join("plans", self.pid, "units", uid, "attempts", "a1", "worktree")

    # F4
    def test_f4_uncommitted_worktree_files_are_included_in_full(self):
        self.make_alpha()
        wt = os.path.join(self.adir("alpha"), "worktree")
        with open(os.path.join(wt, "src", "alpha.py"), "w") as f:
            f.write("print(2)  # modified\n")
        os.makedirs(os.path.join(wt, "notes"))
        with open(os.path.join(wt, "notes", "unfinished.txt"), "w") as f:
            f.write("uncommitted evidence")
        with open(os.path.join(wt, "src", "with space.txt"), "w") as f:
            f.write("spaced")
        got = self.build(False)["private"]["files"]
        r = self.wtrel()
        self.assertEqual(got[os.path.join(r, "src", "alpha.py")]["text"], "print(2)  # modified\n")
        self.assertEqual(got[os.path.join(r, "notes", "unfinished.txt")]["text"], "uncommitted evidence")
        self.assertEqual(got[os.path.join(r, "src", "with space.txt")]["text"], "spaced")
        self.assertNotIn("listed_only", got[os.path.join(r, "notes", "unfinished.txt")])
        clean = got[os.path.join(r, "src", "alpha_data.txt")]  # committed and unmodified
        self.assertEqual((clean["listed_only"], clean["sha256"]), (True, hashlib.sha256(b"x\n").hexdigest()))
        self.assertNotIn("text", clean)
        self.assertTrue([k for k in got if os.sep + ".git" + os.sep in k and got[k].get("listed_only")])

    def test_f4_a_worktree_git_cannot_read_is_listed_as_unreadable(self):
        self.put(os.path.join(self.wtrel("beta"), "f.txt"), b"x")  # no .git: not a checkout
        got = self.build(False)["private"]["files"]
        e = got[self.wtrel("beta")]
        self.assertIn("unreadable", e)
        self.assertEqual(got[os.path.join(self.wtrel("beta"), "f.txt")]["listed_only"], True)

    def test_f4_report_md_describes_what_is_listed_only(self):
        out = os.path.join(self.tmp, "o")
        self.assertEqual(self.cli("report", "--out", out).returncode, 0)
        with open(os.path.join(out, "report.md")) as f:
            md = f.read()
        self.assertIn("modified and untracked", md)
        self.assertIn("listed with size and digest only", md)

    # F5
    def test_f5_recorded_code_output_digests_survive_a_missing_worktree(self):
        self.make_alpha()
        os.rename(os.path.join(self.adir("alpha"), "worktree"), os.path.join(self.tmp, "moved"))
        rep = self.build()
        outs = dict((o["path"], o) for o in self.units(rep)["alpha"]["attempts"][0]["outputs"])
        self.assertEqual((outs["src/alpha.py"]["size"], outs["src/alpha.py"]["sha256"]),
                         (9, hashlib.sha256(b"print(1)\n").hexdigest()))
        self.assertEqual([i["item"] for i in rep["incomplete"] if i["unit"] == "alpha"],
                         ["an output record has an invalid path"])  # the planted absolute path, nothing about digests

    def test_f5_present_worktree_that_disagrees_with_the_record_is_flagged(self):
        self.make_alpha()
        j = os.path.join(self.adir("alpha"), "judgment.json")
        with open(j) as f:
            d = json.load(f)
        d["outputs"][0]["sha256"] = "e" * 64
        wj(j, d)
        rep = self.build()
        o = [o for o in self.units(rep)["alpha"]["attempts"][0]["outputs"] if o["path"] == "src/alpha.py"][0]
        self.assertEqual((o["sha256"], o["changed_since_judgment"]), ("e" * 64, True))
        self.assertIn({"unit": "alpha", "item": "output src/alpha.py changed since judgment"}, rep["incomplete"])

    def test_f5_judge_records_size_and_digest_of_code_outputs(self):
        wt = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(wt, "src"))
        git(wt, "init", "-q")
        with open(os.path.join(wt, "src", "a.py"), "w") as f:
            f.write("abc\n")
        git(wt, "add", ".")
        git(wt, "commit", "-q", "-m", "a")
        cand = git(wt, "rev-parse", "HEAD")
        with open(os.path.join(wt, "src", "a.py"), "w") as f:
            f.write("changed after the commit\n")
        r = judge.code_outputs(wt, cand, {"outputs": ["src/a.py", "src/missing.py"]})
        self.assertEqual((r[0]["ok"], r[0]["size"], r[0]["sha256"]), (True, 4, hashlib.sha256(b"abc\n").hexdigest()))
        self.assertNotIn("sha256", r[1])

    # F6
    def test_f6_large_files_are_copied_by_streaming_not_inlined(self):
        old = report.INLINE_MAX
        report.INLINE_MAX = 1000
        self.addCleanup(setattr, report, "INLINE_MAX", old)
        big = os.urandom(5000) + b"unique-tail"
        self.put("extra/big.bin", big)
        small = b"s" * 900
        self.put("extra/small.txt", small)
        out = os.path.join(self.tmp, "o")
        self.assertEqual(self.cli("report", "--out", out).returncode, 0)  # CLI uses the module default; copy below
        rep = report.write_report(self.root, os.path.join(self.tmp, "o2"), False)
        e = rep["private"]["files"]["extra/big.bin"]
        self.assertEqual((e["size"], e["sha256"], e["copied_to"]), (len(big), hashlib.sha256(big).hexdigest(), "files/extra/big.bin"))
        for k in ("text", "content", "json"):
            self.assertNotIn(k, e)
        with open(os.path.join(self.tmp, "o2", "files", "extra", "big.bin"), "rb") as f:
            self.assertEqual(f.read(), big)
        self.assertEqual(rep["private"]["files"]["extra/small.txt"]["text"], small.decode())

    def test_f6_memory_stays_bounded_for_a_file_over_the_inline_limit(self):
        import tracemalloc
        p = self.put("extra/huge.bin", b"")
        with open(p, "wb") as f:
            block = b"\0" * (1 << 20)
            for _ in range(40):
                f.write(block)
        tracemalloc.start()
        try:
            rep = report.write_report(self.root, os.path.join(self.tmp, "o3"), False)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 12 * 1024 * 1024, peak)
        self.assertEqual(rep["private"]["files"]["extra/huge.bin"]["size"], 40 << 20)
        self.assertEqual(os.path.getsize(os.path.join(self.tmp, "o3", "files", "extra", "huge.bin")), 40 << 20)

    def test_f6_public_report_has_no_copied_files(self):
        self.put("extra/big.bin", b"x" * 10)
        out = os.path.join(self.tmp, "pub")
        report.write_report(self.root, out, True)
        self.assertFalse(os.path.exists(os.path.join(out, "files")))


class FilesTests(ReportCase):
    def test_md_and_json_agree_on_unit_states(self):
        self.make_alpha()
        self.make_crunch("UNCERTAIN")
        for public in (True, False):
            out = os.path.join(self.tmp, "o%d" % public)
            args = ["report", "--out", out] + (["--public"] if public else [])
            self.assertEqual(self.cli(*args).returncode, 0)
            with open(os.path.join(out, "report.json")) as f:
                rep = json.load(f)
            with open(os.path.join(out, "report.md")) as f:
                md = f.read()
            rows = dict((m.group(1), m.group(2)) for m in re.finditer(r"^\| ([a-z0-9_-]+) \| \w+ \| ([A-Za-z_]+) \|", md, re.M) if m.group(1) not in ("unit", "role"))  # table headers
            states = dict((u["id"], u["state"]) for p in rep["plans"] for u in p["units"])
            self.assertEqual(rows, states)
            self.assertEqual(rep["public"], public)
            self.assertEqual("private" in rep, not public)
            self.assertEqual(rows, {"alpha": "merged", "beta": "pending", "crunch": "UNCERTAIN"})

    def test_default_location_and_read_only(self):
        self.make_alpha()
        ev = paths.events_path(self.root)
        with open(ev, "rb") as f:
            before = f.read()
        self.assertEqual(self.cli("report", "--public").returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(paths.state_dir(self.root), "report", "public", "report.json")))
        self.assertEqual(self.cli("report").returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(paths.state_dir(self.root), "report", "private", "report.md")))
        with open(ev, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_private_report_does_not_ingest_earlier_reports(self):
        self.make_alpha()
        self.cli("report")
        rep = self.build(False)
        self.assertFalse([k for k in rep["private"]["files"] if k.startswith("report")])

    def test_out_that_is_a_file_is_refused(self):
        f = os.path.join(self.tmp, "afile")
        open(f, "w").close()
        r = self.cli("report", "--public", "--out", f)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a directory", r.stderr)

    def test_empty_project_reports_nothing(self):
        r = self.cli("report", "--public", "--out", os.path.join(self.tmp, "o"), cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
