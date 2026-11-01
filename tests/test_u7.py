import ast
import json
import os
import re
import sys
import unittest
from unittest import mock

from helpers import SCRIPTS, plan_copy
from test_u5 import COMMIT, ID, Case, jload, sh

from overseer import approval, execution as X, github as G, judge as J, paths, store

SRC = os.path.join(SCRIPTS, "overseer")

SHIM = r'''#!%(py)s
import json, os, re, subprocess, sys
st_path = os.environ["GH_SHIM_STATE"]
st = json.load(open(st_path))
a = sys.argv[1:]
st["calls"].append(a)
faults = st["faults"]
def save():
    json.dump(st, open(st_path, "w"))
def out(text=""):
    save(); sys.stdout.write(text); sys.exit(0)
def die(msg, rc=1):
    save(); sys.stderr.write(msg + "\n"); sys.exit(rc)
def opt(name):
    return a[a.index(name) + 1]
def fault(name):
    if faults.get(name):
        faults[name] = False if faults.get(name) is True else faults[name] - 1 if faults[name] > 1 else False
        return True
    return False
repo = opt("-R") if "-R" in a else ""
def issue(n):
    return next(i for i in st["issues"] if i["number"] == n)
def pick(d, fields):
    return {k: d[k] for k in fields.split(",") if k in d}
if a[0] == "issue" and a[1] == "list":
    out(json.dumps([pick(i, "number,title,body,state") for i in st["issues"]]))
if a[0] == "issue" and a[1] == "create":
    if fault("create_timeout_before"):
        die("error connecting to api.github.com")
    n = len(st["issues"]) + 1
    st["issues"].append({"number": n, "id": 1000 + n, "title": opt("--title"), "body": open(opt("--body-file")).read(),
                         "state": "OPEN", "comments": [], "blockers": []})
    if fault("create_then_fail"):
        die("Post \"https://api.github.com/graphql\": dial tcp 1.2.3.4:443: i/o timeout")
    out("https://github.com/%%s/issues/%%d\n" %% (repo, n))
if a[0] == "issue" and a[1] == "view":
    n = int(a[2])
    i = issue(n)
    i["number"] = n
    out(json.dumps(pick(i, opt("--json"))))
if a[0] == "issue" and a[1] == "comment":
    issue(int(a[2]))["comments"].append({"body": open(opt("--body-file")).read()})
    out()
if a[0] == "issue" and a[1] == "close":
    if fault("close_timeout"):
        issue(int(a[2]))["state"] = "CLOSED"
        die("TLS handshake timeout")
    issue(int(a[2]))["state"] = "CLOSED"
    out()
if a[0] == "api":
    m = re.match(r"repos/[^/]+/[^/]+/issues/(\d+)(/dependencies/blocked_by)?", next(x for x in a if x.startswith("repos/")))
    if faults.get("deps_404") and m.group(2):
        die("gh: Not Found (HTTP 404)")
    i = issue(int(m.group(1)))
    if "-X" in a:
        i["blockers"].append(int(a[a.index("-F") + 1].split("=")[1]))
        out("{}")
    if m.group(2):
        out(json.dumps([{"number": b - 1000, "id": b} for b in i["blockers"]]))
    out(json.dumps({"number": i["number"], "id": i["id"]}))
def pr_json(p):
    p["headRefOid"] = faults.get("wrong_head") or subprocess.check_output(
        ["git", "--git-dir", st["origin"], "rev-parse", "refs/heads/" + p["headRefName"]]).decode().strip()
    return p
if a[0] == "pr" and a[1] == "list":
    out(json.dumps([{k: p[k] for k in ("number", "state", "headRefName")} for p in st["prs"] if p["headRefName"] == opt("--head")]))
if a[0] == "pr" and a[1] == "create":
    n = len(st["prs"]) + 1
    st["prs"].append({"number": n, "state": "OPEN", "headRefName": opt("--head"), "baseRefName": opt("--base"),
                      "title": opt("--title"), "body": open(opt("--body-file")).read(), "mergeCommit": None})
    out("https://github.com/%%s/pull/%%d\n" %% (repo, n))
if a[0] == "pr" and a[1] == "edit":
    st["prs"][int(a[2]) - 1]["body"] = open(opt("--body-file")).read()
    out()
if a[0] == "pr" and a[1] == "view":
    p = pr_json(dict(st["prs"][int(a[2]) - 1]))
    p["closingIssuesReferences"] = [{"number": int(x)} for x in re.findall(r"Closes #(\d+)", p["body"])]
    out(json.dumps(pick(p, opt("--json"))))
die("unhandled: %%r" %% (a,), 2)
'''


class GhCase(Case):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.tmp, "gh-state.json")
        self.gh_state({"issues": [], "prs": [], "calls": [], "faults": {}, "origin": self.origin})
        p = os.path.join(self.shims, "gh")
        with open(p, "w") as f:
            f.write(SHIM % {"py": sys.executable})
        os.chmod(p, 0o755)
        for k, v in (("GH_SHIM_STATE", self.state),):
            mock.patch.dict(os.environ, {k: v}).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(G, "RETRY_DELAY", 0).start()

    def gh_state(self, st=None):
        if st is not None:
            with open(self.state, "w") as f:
                json.dump(st, f)
        return jload(self.state)

    def fault(self, **kw):
        st = self.gh_state()
        st["faults"].update(kw)
        self.gh_state(st)

    def calls(self, *prefix):
        return [c for c in self.gh_state()["calls"] if tuple(c[:len(prefix)]) == prefix]

    def issues_json(self):
        return jload(os.path.join(paths.plan_dir(self.root, "demo"), "issues.json"))

    def three_units(self):
        p = self.approve()
        p["units"].append({"id": "beta", "kind": "job", "title": "Beta", "command": "true", "outputs": ["b.txt"],
                           "needs": ["alpha"]})
        approval.approve_plan(self.root, p, "me")


class Transport(GhCase):
    def test_gh_runs_with_supervisor_env_and_reports_rc_stdout_stderr(self):
        r = G.gh(["issue", "list", "-R", "o/r", "--state", "all", "--limit", "1000", "--json", "number"])
        self.assertEqual((r["rc"], json.loads(r["stdout"])), (0, []))
        r = G.gh(["nothing"])
        self.assertEqual((r["rc"], "unhandled" in r["stderr"]), (2, True))

    def test_transient_classification(self):
        mk = lambda rc, err, t=False: {"rc": rc, "stderr": err, "timed_out": t}
        for e in ("dial tcp 1.2.3.4:443: i/o timeout", "TLS handshake timeout", "error connecting to api.github.com"):
            self.assertTrue(G.transient(mk(1, e)), e)
        self.assertTrue(G.transient(mk(None, "", True)))
        self.assertFalse(G.transient(mk(1, "HTTP 422: Validation Failed")))
        self.assertFalse(G.transient(mk(127, "command not found")))

    def test_transient_read_is_retried_definite_failure_is_not(self):
        with mock.patch.object(G, "gh", side_effect=[{"rc": 1, "stderr": "TLS handshake timeout", "stdout": "", "timed_out": False},
                                                     {"rc": 0, "stdout": "[]", "stderr": "", "timed_out": False}]) as m:
            self.assertEqual(G._read(["x"], "x", list), [])
        self.assertEqual(m.call_count, 2)
        with mock.patch.object(G, "gh", return_value={"rc": 1, "stderr": "HTTP 422", "stdout": "", "timed_out": False}) as m:
            with self.assertRaises(G.GithubError):
                G._read(["x"], "x", list)
        self.assertEqual(m.call_count, 1)


class Filing(GhCase):
    def test_files_all_then_rerun_creates_none_and_records_numbers_with_read_time(self):
        self.approve()
        rc, out, err = self.cli("issues", "file")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.calls("issue", "create")), 2)
        rec = self.issues_json()
        self.assertEqual(rec["filed"], {"alpha": 1, "crunch": 2})
        self.assertTrue(re.match(r"\d{4}-\d\d-\d\dT", rec["filed_at"]["alpha"]))
        self.assertEqual(self.calls("issue", "list")[0][-2:], ["--json", "number,title,body,state"])
        self.assertIn("--state", self.calls("issue", "list")[0])
        self.assertFalse([c for c in self.calls("issue", "create") if "--body-file" not in c])
        self.assertEqual(self.cli("issues", "file")[0], 0)
        self.assertEqual(len(self.calls("issue", "create")), 2)
        self.assertEqual(len(self.gh_state()["issues"]), 2)
        self.assertIn("overseer plan=demo unit=alpha", self.gh_state()["issues"][0]["body"])

    def test_create_that_succeeds_then_fails_is_adopted_by_marker_not_duplicated(self):
        self.approve()
        self.fault(create_then_fail=True)
        rc, _, err = self.cli("issues", "file")
        self.assertEqual(rc, 0, err)  # reconciled in the same run by listing, not by repeating the create
        self.assertEqual(len(self.gh_state()["issues"]), 2)
        self.assertEqual(self.issues_json()["filed"], {"alpha": 1, "crunch": 2})
        self.assertEqual(len(self.calls("issue", "create")), 2)
        ev = [e["kind"] for e in store.read_events(self.root)]
        self.assertIn("github_ambiguous", ev)
        self.assertLess(ev.index("github_intent"), ev.index("github_ambiguous"))

    def test_ambiguous_create_not_visible_stops_and_rerun_reconciles(self):
        self.approve()
        self.fault(create_timeout_before=True)
        rc, _, err = self.cli("issues", "file")
        self.assertEqual(rc, 3, err)
        self.assertIn("unknown outcome", err)
        self.assertEqual(len(self.calls("issue", "create")), 1)  # never repeated in the same run
        self.assertEqual(self.cli("issues", "file")[0], 0)
        self.assertEqual(len(self.gh_state()["issues"]), 2)

    def test_interrupted_after_create_before_record_converges_on_rerun(self):
        self.approve()
        with mock.patch.object(G, "_record_filed", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                G.file_issues(self.root, "demo", out=lambda s: None)
        self.assertEqual(len(self.gh_state()["issues"]), 1)
        self.assertEqual(self.cli("issues", "file")[0], 0)
        self.assertEqual(len(self.gh_state()["issues"]), 2)
        self.assertEqual(self.issues_json()["filed"], {"alpha": 1, "crunch": 2})

    def test_two_marker_matches_stop_before_any_create(self):
        self.approve()
        marker = "<!-- overseer plan=demo unit=crunch -->"
        st = self.gh_state()
        st["issues"] = [{"number": n, "id": 1000 + n, "title": "dup", "body": marker + "\n", "state": "OPEN",
                         "comments": [], "blockers": []} for n in (1, 2)]
        self.gh_state(st)
        rc, _, err = self.cli("issues", "file")
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous", err)
        self.assertEqual(self.calls("issue", "create"), [])

    def test_closed_issue_is_matched_and_marker_must_be_a_whole_line(self):
        self.approve()
        st = self.gh_state()
        st["issues"] = [{"number": 7, "id": 1007, "title": "old", "state": "CLOSED", "comments": [], "blockers": [],
                         "body": "x\n<!-- overseer plan=demo unit=alpha -->\n"},
                        {"number": 8, "id": 1008, "title": "quote", "state": "OPEN", "comments": [], "blockers": [],
                         "body": "see > <!-- overseer plan=demo unit=crunch --> here"}]
        self.gh_state(st)
        self.assertEqual(self.cli("issues", "file")[0], 0)
        self.assertEqual(self.issues_json()["filed"], {"alpha": 7, "crunch": 3})

    def test_dry_run_makes_no_create_call_and_writes_nothing(self):
        self.approve()
        before = self.issues_json()
        events = len(store.read_events(self.root))
        rc, out, err = self.cli("issues", "file", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("would create", out)
        self.assertEqual(self.calls("issue", "create"), [])
        self.assertEqual((self.issues_json(), len(store.read_events(self.root))), (before, events))

    def test_requires_valid_approval(self):
        self.approve()
        p = os.path.join(paths.plan_dir(self.root, "demo"), "issues.json")
        rec = jload(p)
        rec["drafts"]["alpha"]["title"] = "tampered"
        store.write_json_atomic(p, rec)
        rc, _, err = self.cli("issues", "file")
        self.assertEqual(rc, 1)
        self.assertIn("digest", err)
        self.assertEqual(self.gh_state()["calls"], [])

    def test_readback_mismatch_is_an_error(self):
        self.approve()
        with mock.patch.object(G, "view_issue", return_value={"number": 1, "title": "other", "body": "", "state": "OPEN"}):
            with self.assertRaises(G.GithubError):
                G.file_issues(self.root, "demo", out=lambda s: None)

    def test_recorded_number_without_marker_match_is_an_error(self):
        self.approve()
        self.cli("issues", "file")
        st = self.gh_state()
        st["issues"].pop(0)
        self.gh_state(st)
        rc, _, err = self.cli("issues", "file")
        self.assertEqual(rc, 1)
        self.assertIn("disagrees", err)

    def test_truncated_listing_is_refused(self):
        self.approve()
        row = {"number": 1, "title": "t", "body": "", "state": "OPEN"}
        with mock.patch.object(G, "_read", return_value=[dict(row, number=i + 1) for i in range(G.LIST_LIMIT)]):
            with self.assertRaises(G.GithubError):
                G.list_issues("o/r")


class Links(GhCase):
    def test_native_blocked_by_link_is_added_and_read_back(self):
        self.three_units()
        self.assertEqual(self.cli("issues", "file")[0], 0)
        st = self.gh_state()
        self.assertEqual(st["issues"][2]["blockers"], [1001])  # beta (#3) blocked by alpha (#1)
        self.assertEqual(self.issues_json()["links"]["beta<-alpha"]["mode"], "native")
        post = [c for c in st["calls"] if c[:3] == ["api", "-X", "POST"]]
        self.assertEqual((len(post), post[0][-2:]), (1, ["-F", "issue_id=1001"]))
        self.assertEqual(self.cli("issues", "file")[0], 0)
        self.assertEqual(len([c for c in self.gh_state()["calls"] if c[:3] == ["api", "-X", "POST"]]), 1)

    def test_missing_api_falls_back_to_one_marked_comment(self):
        self.three_units()
        self.fault(deps_404=True)
        self.assertEqual(self.cli("issues", "file")[0], 0)
        self.assertEqual(self.issues_json()["links"]["beta<-alpha"]["mode"], "comment")
        c = self.gh_state()["issues"][2]["comments"]
        self.assertEqual(len(c), 1)
        self.assertIn("Blocked by #1", c[0]["body"])
        rec = self.issues_json()
        del rec["links"]  # forget the record: the marker alone keeps the comment idempotent
        store.write_json_atomic(os.path.join(paths.plan_dir(self.root, "demo"), "issues.json"), rec)
        self.assertEqual(self.cli("issues", "file")[0], 0)
        self.assertEqual(len(self.gh_state()["issues"][2]["comments"]), 1)

    def test_link_not_visible_after_write_is_an_error(self):
        self.three_units()
        with mock.patch.object(G, "_blocked_by", return_value=set()):
            with self.assertRaises(G.Ambiguous):
                G.file_issues(self.root, "demo", out=lambda s: None)


class Close(GhCase):
    def test_close_issue_comments_once_closes_and_reads_back(self):
        self.approve()
        self.cli("issues", "file")
        self.assertEqual(G.close_issue(2, "done: ok", "lab-org/demo-repo", self.root), "CLOSED")
        st = self.gh_state()
        self.assertEqual((st["issues"][1]["state"], len(st["issues"][1]["comments"])), ("CLOSED", 1))
        self.assertEqual(G.close_issue(2, "done: ok", "lab-org/demo-repo", self.root), "CLOSED")
        self.assertEqual(len(self.gh_state()["issues"][1]["comments"]), 1)

    def test_close_that_times_out_after_applying_is_reconciled(self):
        self.approve()
        self.cli("issues", "file")
        self.fault(close_timeout=True)
        self.assertEqual(G.close_issue(2, "done", "lab-org/demo-repo", self.root), "CLOSED")
        self.assertEqual(len(self.calls("issue", "close")), 1)

    def test_close_reading_open_is_an_error(self):
        self.approve()
        self.cli("issues", "file")
        with mock.patch.object(G, "view_issue", return_value={"number": 2, "state": "OPEN", "comments": [{"body": "<!-- overseer close=2 -->"}]}):
            with self.assertRaises(G.Ambiguous):
                G.close_issue(2, "x", "lab-org/demo-repo", self.root)

    def test_unit_close_needs_done_judgment_of_a_job(self):
        self.approve()
        self.cli("issues", "file")
        rc, _, err = self.cli("issues", "close", "crunch", "--comment", "done")
        self.assertEqual(rc, 1)
        self.assertIn("no valid DONE judgment", err)
        self.assertEqual(self.calls("issue", "close"), [])
        done = {"status": "DONE", "invocation_token": "t"}
        unit = X._unit_of(approval.effective_plan(self.root, "demo"), "crunch")
        with mock.patch.object(G, "_judgment", return_value=done):
            self.assertEqual(G.close_unit_issue(self.root, "demo", unit, "ok"), (2, "CLOSED"))
        alpha = X._unit_of(approval.effective_plan(self.root, "demo"), "alpha")
        with mock.patch.object(G, "_judgment", return_value=done):
            with self.assertRaises(G.GithubError):
                G.close_unit_issue(self.root, "demo", alpha, "ok")


class PullRequest(GhCase):
    def done_unit(self):
        self.approve()
        self.cli("issues", "file")
        j = self.run_unit('mkdir -p src; echo hi > src/alpha.py; git add src; %s' % COMMIT, expect="DONE")
        return j

    def test_pr_pushes_creates_with_closes_and_records_read_back_head(self):
        j = self.done_unit()
        rc, out, err = self.cli("pr", "alpha")
        self.assertEqual(rc, 0, err)
        rec = json.loads(out)
        branch = "overseer/demo/alpha/a1"
        self.assertEqual(sh(self.origin, "rev-parse", "refs/heads/" + branch), j["candidate"])
        self.assertEqual((rec["number"], rec["head_oid"], rec["base_ref"], rec["closing_issues"]), (1, j["candidate"], "main", [1]))
        pr = self.gh_state()["prs"][0]
        self.assertTrue(pr["body"].startswith("Closes #1\n"))
        for needle in ("alpha works", "base: " + j["base"], "candidate: " + j["candidate"], "test rc: 0"):
            self.assertIn(needle, pr["body"])
        create = self.calls("pr", "create")[0]
        self.assertEqual(create[create.index("--head") + 1], branch)
        self.assertIn("--body-file", create)
        self.assertEqual(jload(os.path.join(paths.unit_dir(self.root, "demo", "alpha"), "pr.json"))["number"], 1)

    def test_rerun_edits_the_existing_pr_instead_of_creating_another(self):
        self.done_unit()
        self.assertEqual(self.cli("pr", "alpha")[0], 0)
        st = self.gh_state()
        st["prs"][0]["body"] = "stale"
        self.gh_state(st)
        self.assertEqual(self.cli("pr", "alpha")[0], 0)
        self.assertEqual((len(self.gh_state()["prs"]), len(self.calls("pr", "create")), len(self.calls("pr", "edit"))), (1, 1, 1))
        self.assertTrue(self.gh_state()["prs"][0]["body"].startswith("Closes #1"))

    def test_pr_without_filed_issue_omits_closes(self):
        self.approve()
        self.run_unit('mkdir -p src; echo hi > src/alpha.py; git add src; %s' % COMMIT, expect="DONE")
        self.assertEqual(self.cli("pr", "alpha")[0], 0)
        self.assertNotIn("Closes", self.gh_state()["prs"][0]["body"])

    def test_refuses_without_done_judgment_or_after_head_moves(self):
        self.approve()
        rc, _, err = self.cli("pr", "alpha")
        self.assertEqual(rc, 1)
        self.run_unit('mkdir -p src; echo hi > src/alpha_x.py', expect="FAILED")
        rc, _, err = self.cli("pr", "alpha")
        self.assertEqual((rc, "no valid DONE judgment" in err), (1, True))
        self.assertEqual(self.calls("pr"), [])
        self.assertEqual(sh(self.origin, "branch", "--list", "overseer/*"), "")

    def test_refuses_non_ancestor_remote_branch_and_never_pushes(self):
        j = self.done_unit()
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "clone", "-q", self.origin, other)
        self.put("z.txt", "z\n", base=other)
        sh(other, *(ID + ["add", "-A"]))
        sh(other, *(ID + ["commit", "-q", "-m", "diverge"]))
        sh(other, "push", "-q", "origin", "HEAD:refs/heads/overseer/demo/alpha/a1")
        diverged = sh(self.origin, "rev-parse", "refs/heads/overseer/demo/alpha/a1")
        rc, _, err = self.cli("pr", "alpha")
        self.assertEqual(rc, 1)
        self.assertIn("not an ancestor", err)
        self.assertEqual(sh(self.origin, "rev-parse", "refs/heads/overseer/demo/alpha/a1"), diverged)
        self.assertNotEqual(diverged, j["candidate"])
        self.assertEqual(self.calls("pr"), [])

    def test_ancestor_remote_branch_is_fast_forwarded_by_plain_push(self):
        j = self.done_unit()
        sh(self.wt(), "push", "-q", "origin", "%s:refs/heads/overseer/demo/alpha/a1" % j["base"])
        self.assertEqual(self.cli("pr", "alpha")[0], 0)
        self.assertEqual(sh(self.origin, "rev-parse", "refs/heads/overseer/demo/alpha/a1"), j["candidate"])

    def test_head_mismatch_on_read_back_is_an_error(self):
        self.done_unit()
        self.fault(wrong_head="0" * 40)
        rc, _, err = self.cli("pr", "alpha")
        self.assertEqual(rc, 1)
        self.assertIn("head reads", err)
        self.assertFalse(os.path.exists(os.path.join(paths.unit_dir(self.root, "demo", "alpha"), "pr.json")))

    def test_pr_create_that_applies_then_times_out_is_adopted(self):
        self.done_unit()
        real = G.gh

        def flaky(args, timeout=60, cwd=None):
            r = real(args, timeout, cwd)
            if args[:2] == ["pr", "create"]:
                return {"rc": 1, "stdout": "", "stderr": "TLS handshake timeout", "timed_out": False}
            return r
        with mock.patch.object(G, "gh", flaky):
            self.assertEqual(self.cli("pr", "alpha")[0], 0)
        self.assertEqual(len(self.gh_state()["prs"]), 1)

    def test_push_timeout_that_applied_is_accepted_but_unapplied_is_an_error(self):
        j = self.done_unit()
        real = G.W.git

        def timeout_push(cwd, *a, **kw):
            if a[0] == "push":
                real(cwd, *a, **kw)
                raise G.W.GitError("git push timed out")
            return real(cwd, *a, **kw)
        with mock.patch.object(G.W, "git", timeout_push):
            self.assertEqual(self.cli("pr", "alpha")[0], 0)

        def dead_push(cwd, *a, **kw):
            if a[0] == "push":
                raise G.W.GitError("git push timed out")
            return real(cwd, *a, **kw)
        sh(self.origin, "update-ref", "-d", "refs/heads/overseer/demo/alpha/a1")
        with mock.patch.object(G.W, "git", dead_push):
            rc, _, err = self.cli("pr", "alpha")
        self.assertEqual((rc, "unknown outcome" in err), (1, True))

    def test_readback_pr_fields_and_validation(self):
        self.done_unit()
        self.cli("pr", "alpha")
        r = G.readback_pr(1, "lab-org/demo-repo")
        self.assertEqual((r["state"], r["merge_oid"], r["base_ref"], r["closing_issues"]), ("OPEN", None, "main", [1]))
        self.assertEqual(len(r["head_oid"]), 40)
        st = self.gh_state()
        st["prs"][0].update(state="MERGED", mergeCommit={"oid": "a" * 40})
        self.gh_state(st)
        self.assertEqual(G.readback_pr(1, "lab-org/demo-repo")["merge_oid"], "a" * 40)
        with mock.patch.object(G, "_read", return_value={"number": 1, "state": "WEIRD"}):
            with self.assertRaises(G.GithubError):
                G.readback_pr(1, "o/r")


class SourceRules(unittest.TestCase):
    def sources(self):
        for d, _, fs in os.walk(SRC):
            for f in fs:
                if f.endswith(".py"):
                    yield os.path.join(d, f)

    def test_no_force_push_form_anywhere_in_the_supervisor(self):
        bad = re.compile(r"--force|-f\b|[\"']\+|:\+")
        seen = 0
        for p in self.sources():
            with open(p) as fh:
                text = fh.read()
            for ln in text.splitlines():
                if re.search(r"\bpush\b", ln) and "push_" not in ln.split("push")[0][-1:]:
                    seen += 1
                    self.assertFalse(bad.search(ln), "%s: %s" % (p, ln))
            for node in ast.walk(ast.parse(text)):
                elts = node.args if isinstance(node, ast.Call) else node.elts if isinstance(node, (ast.List, ast.Tuple)) else []
                consts = [e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if "push" in consts:
                    for c in consts:
                        self.assertFalse(c.startswith(("--force", "+")) or c in ("-f", "-d"), "%s pushes with %r" % (p, c))
        self.assertGreater(seen, 0)

    def test_push_argv_is_plain(self):
        self.assertEqual(G.push_argv("overseer/p/u/a1"), ["push", "origin", "overseer/p/u/a1:overseer/p/u/a1"])
        for bad in ("+x", "-f", "a b", "a..b"):
            with self.assertRaises(Exception):
                G.push_argv(bad)

    def test_worker_side_modules_do_not_import_github(self):
        for name in ("adapters", "execution", "slurm", "workspace"):
            with open(os.path.join(SRC, name + ".py")) as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                mods = [a.name for a in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) else []
                if isinstance(node, ast.ImportFrom):
                    mods += [node.module or ""]
                self.assertFalse([m for m in mods if m.split(".")[-1] == "github"], name)

    def test_no_merge_function_is_exported(self):
        self.assertFalse([n for n in dir(G) if re.search(r"merge(?!_oid)", n) and not n.startswith("_")])


if __name__ == "__main__":
    unittest.main()
