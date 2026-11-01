import contextlib
import copy
import datetime
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts the scripts directory on sys.path)
from overseer import adapters, cli, env as E, models, store, survey

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


CLAUDE_EVENTS = json.loads(fixture("claude_events.json"))
AGY_RESULT = json.loads(fixture("agy_result.json"))
AGY_MODELS = fixture("agy_models.txt")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ovr-u2-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "project")
        self.shims = os.path.join(self.tmp, "shims")
        os.makedirs(self.root)
        os.makedirs(self.shims)
        self.log = os.path.join(self.tmp, "calls.log")

    def shim(self, name, body):
        p = os.path.join(self.shims, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\necho \"%s $*\" >> '%s'\n%s\n" % (name, self.log, body))
        os.chmod(p, 0o755)

    def data_file(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def host_env(self, **extra):
        sysbin = os.path.join(self.tmp, "sysbin")  # only the system tools the shims and git need
        if not os.path.isdir(sysbin):
            os.makedirs(sysbin)
            for t in ("sh", "cat", "env", "sleep", "git", "echo"):
                src = shutil.which(t)
                if src:
                    os.symlink(src, os.path.join(sysbin, t))
        e = {"PATH": os.pathsep.join([self.shims, sysbin]), "HOME": self.tmp, "LC_ALL": "C.UTF-8"}
        e.update(extra)
        return e

    def run_cli(self, *args, **env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.host_env(**env), clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--root", self.root] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def state(self, name):
        with open(os.path.join(self.root, ".overseer", name)) as f:
            return json.load(f)


class EnvTests(Case):
    CANARIES = {"FAKE_API_KEY": "k", "GH_TOKEN": "t", "GITHUB_TOKEN": "t", "ANTHROPIC_BASE_URL": "u",
                "OPENAI_API_KEY": "k", "SECRET_TOKEN": "t", "FOO": "bar", "AWS_PROFILE": "p",
                "CLAUDE_CODE_USE_BEDROCK": "1", "GOOGLE_APPLICATION_CREDENTIALS": "f"}

    def test_canary_child_environment(self):
        self.shim("dump", "env")
        base = dict(self.host_env(SLURM_JOB_ID="7", XDG_CONFIG_HOME="/x"), **self.CANARIES)
        r = E.run_tool(["dump"], E.worker_env(base), 10)
        names = {l.split("=", 1)[0] for l in r["stdout"].splitlines()}
        self.assertFalse(names & set(self.CANARIES), names & set(self.CANARIES))
        self.assertTrue({"PATH", "HOME", "SLURM_JOB_ID", "XDG_CONFIG_HOME"} <= names)

    def test_extras_pass_but_denylist_still_applies(self):
        e = E.worker_env(self.host_env(), {"OVERSEER_OUT": "/o", "GH_TOKEN": "t"})
        self.assertEqual(e["OVERSEER_OUT"], "/o")
        self.assertNotIn("GH_TOKEN", e)

    def test_supervisor_env_keeps_github_drops_llm(self):
        e = E.supervisor_env(dict(self.host_env(), **self.CANARIES))
        for kept in ("GH_TOKEN", "GITHUB_TOKEN", "FOO"):
            self.assertIn(kept, e)
        for dropped in ("FAKE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK"):
            self.assertNotIn(dropped, e)

    def test_timeout_kills_process_group(self):
        self.shim("slow", "sleep 30")
        t = time.time()
        r = E.run_tool(["slow"], self.host_env(), 1)
        self.assertTrue(r["timed_out"] and r["rc"] is None)
        self.assertLess(time.time() - t, 10)


class ModelTests(Case):
    def test_p10_listing_selects_argon_and_flash(self):
        c = models.resolve_google(models.parse_listing(AGY_MODELS))
        self.assertEqual(c, {"plan": "gemini-4-argon-high", "code": "gemini-4-argon-high",
                             "review": "gemini-3.8-flash-high"})

    def test_claude_only_listing_errors(self):
        with self.assertRaises(models.ModelError):
            models.resolve_google(models.parse_listing("claude-opus-5-5-high\tClaude Opus 5.5 (High)\n"))

    def test_missing_review_family_errors(self):
        with self.assertRaises(models.ModelError):
            models.resolve_google([("gemini-3.1-pro-high", "x")])

    def test_unparseable_listing_errors(self):
        for text in ("", "Fetching available models...\n", "no tabs here\n"):
            with self.assertRaises(models.ModelError):
                models.parse_listing(text)

    def test_numeric_version_and_tie_order(self):
        pick = lambda ids: models.resolve_google([(i, "") for i in ids + ["gemini-1-flash-high"]])["plan"]
        self.assertEqual(pick(["gemini-3.9-pro-high", "gemini-3.10-pro-high"]), "gemini-3.10-pro-high")
        self.assertEqual(pick(["gemini-5-pro-high", "gemini-5-carbon-high", "gemini-5-argon-high"]),
                         "gemini-5-argon-high")
        self.assertEqual(pick(["gemini-5-pro-high", "gemini-5-carbon-high"]), "gemini-5-carbon-high")
        self.assertEqual(pick(["gemini-3.0-pro-high", "gemini-3-argon-high"]), "gemini-3-argon-high")

    def test_claude_roles_and_event(self):
        rc, out, _ = self.run_cli("models", "--host", "claude")
        self.assertEqual(rc, 0)
        rec = self.state("models.json")
        self.assertEqual(rec["choices"], {"plan": "opus", "code": "sonnet", "review": "haiku"})
        self.assertEqual(rec["host_family"], "claude")
        self.assertEqual(rec["date"], datetime.datetime.now(datetime.timezone.utc).date().isoformat())

    def test_google_via_shim_and_cache(self):
        self.shim("agy", "cat '%s'" % os.path.join(FIX, "agy_models.txt"))
        rc, out, _ = self.run_cli("models", "--host", "google")
        self.assertEqual((rc, self.state("models.json")["raw"]), (0, AGY_MODELS))
        self.assertIn("review: gemini-3.8-flash-high", out)
        self.run_cli("models", "--host", "google")
        self.assertEqual(self.calls(), ["agy models"])  # second call served from cache
        self.run_cli("models", "--host", "google", "--refresh")
        self.assertEqual(len(self.calls()), 2)

    def test_agy_failure_and_empty_listing_are_errors(self):
        self.shim("agy", "echo boom >&2; exit 3")
        rc, _, err = self.run_cli("models", "--host", "google")
        self.assertEqual(rc, 1)
        self.assertIn("boom", err)
        self.shim("agy", "echo 'Fetching available models...'")
        rc, _, err = self.run_cli("models", "--host", "google")
        self.assertEqual(rc, 1)
        self.assertIn("unparseable", err)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".overseer", "models.json")))

    def test_old_cache_warns_and_malformed_cache_is_ignored(self):
        old = (datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=31)).isoformat()
        rec = {"date": old, "host_family": "claude", "raw": "", "choices": dict(models.CLAUDE_ROLES)}
        store.write_json_atomic(os.path.join(self.root, ".overseer", "models.json"), rec)
        rc, _, err = self.run_cli("models", "--host", "claude")
        self.assertIn("31 days old", err)
        self.assertEqual(self.state("models.json")["date"], old)
        for bad in (dict(rec, choices={"plan": 1}), dict(rec, date="yesterday"), dict(rec, raw=None)):
            store.write_json_atomic(os.path.join(self.root, ".overseer", "models.json"), bad)
            _, _, err = self.run_cli("models", "--host", "claude")
            self.assertIn("malformed", err)
            self.assertEqual(self.state("models.json")["choices"]["plan"], "opus")

    def test_crash_between_event_and_record_converges(self):
        with mock.patch.object(store, "write_json_atomic", side_effect=OSError("disk")):
            rc, _, err = self.run_cli("models", "--host", "claude")
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".overseer", "models.json")))
        self.assertEqual(len(store.read_events(self.root)), 1)
        rc, _, _ = self.run_cli("models", "--host", "claude")
        self.assertEqual((rc, self.state("models.json")["choices"]["plan"]), (0, "opus"))
        self.assertEqual(len(store.read_events(self.root)), 2)


class ParserTests(unittest.TestCase):
    def events(self, **result):
        ev = copy.deepcopy(CLAUDE_EVENTS)
        ev[-1].update(result)
        return json.dumps(ev)

    def test_claude_array_ok(self):
        r = adapters.parse_claude(0, json.dumps(CLAUDE_EVENTS), "")
        self.assertTrue(r["ok"])
        self.assertEqual((r["num_turns"], r["api_key_source"], r["usage"]["output_tokens"]), (2, "none", 264))
        self.assertEqual(r["session_id"], "00000000-0000-0000-0000-000000000001")
        self.assertIn("hello.txt", r["text"])

    def test_claude_single_object_and_last_result_wins(self):
        self.assertTrue(adapters.parse_claude(0, json.dumps(CLAUDE_EVENTS[-1]), "")["ok"])
        ev = CLAUDE_EVENTS + [dict(CLAUDE_EVENTS[-1], is_error=True)]
        self.assertFalse(adapters.parse_claude(0, json.dumps(ev), "")["ok"])

    def test_claude_not_ok_cases(self):
        bad = {
            "is_error true rc 0": (0, self.events(is_error=True, result="x")),
            "subtype": (0, self.events(subtype="error_max_turns")),
            "is_error missing": (0, json.dumps(CLAUDE_EVENTS[:-1] + [{
                k: v for k, v in CLAUDE_EVENTS[-1].items() if k != "is_error"}])),
            "rc nonzero": (1, json.dumps(CLAUDE_EVENTS)),
            "no result event": (0, json.dumps(CLAUDE_EVENTS[:-1])),
            "result not text": (0, self.events(result=None)),
            "not json": (0, "not json"),
            "empty": (0, ""),
            "wrong shape": (0, json.dumps("str")),
            "non-dict event": (0, json.dumps(CLAUDE_EVENTS + [3])),
        }
        for label, (rc, out) in bad.items():
            r = adapters.parse_claude(rc, out, "")
            self.assertFalse(r["ok"], label)
            self.assertIn(r["classification"], (adapters.UNKNOWN, adapters.CONFIG, adapters.TRANSIENT), label)

    def test_claude_api_key_source_is_config(self):
        for source in ("ANTHROPIC_API_KEY", None, 3):
            ev = copy.deepcopy(CLAUDE_EVENTS)
            ev[0]["apiKeySource"] = source
            r = adapters.parse_claude(0, json.dumps(ev), "")
            self.assertEqual((r["ok"], r["classification"]), (False, adapters.CONFIG), source)
        ev = copy.deepcopy(CLAUDE_EVENTS)
        del ev[0]["apiKeySource"]
        self.assertTrue(adapters.parse_claude(0, json.dumps(ev), "")["ok"])

    def test_claude_failure_is_classified_from_result_text(self):
        out = self.events(is_error=True, api_error_status=529, result="API Error: overloaded")
        r = adapters.parse_claude(1, out, "")
        self.assertEqual((r["ok"], r["classification"], r["signature"]["name"]), (False, "TRANSIENT", "http-529"))

    def test_structured_output(self):
        self.assertIsNone(adapters.parse_claude(0, json.dumps(CLAUDE_EVENTS), "")["structured"])
        r = adapters.parse_claude(0, self.events(structured_output={"a": 1}, result="ignored {}"), "")
        self.assertEqual(r["structured"], {"a": 1})
        r = adapters.parse_claude(0, self.events(structured_output=[1], result='{"a": 2}'), "")
        self.assertIsNone(r["structured"])  # wrong type is not coerced or replaced
        self.assertEqual(adapters.extract_json('x\n```json\n{"b": 2}\n```\n'), {"b": 2})
        self.assertEqual(adapters.extract_json(' {"c": 3} '), {"c": 3})
        self.assertIsNone(adapters.extract_json("[1]"))
        self.assertIsNone(adapters.extract_json("prose only"))

    def test_agy_ok_and_not_ok(self):
        r = adapters.parse_agy(0, json.dumps(AGY_RESULT), "")
        self.assertTrue(r["ok"])
        self.assertEqual((r["session_id"], r["num_turns"], r["usage"]["total_tokens"]),
                         ("00000000-0000-0000-0000-000000000002", 1, 15343))
        for label, rc, out in (
                ("status", 0, json.dumps(dict(AGY_RESULT, status="ERROR"))),
                ("status missing", 0, json.dumps({k: v for k, v in AGY_RESULT.items() if k != "status"})),
                ("rc", 2, json.dumps(AGY_RESULT)),
                ("response type", 0, json.dumps(dict(AGY_RESULT, response=5))),
                ("array", 0, json.dumps([AGY_RESULT])),
                ("garbage", 0, "{")):
            self.assertFalse(adapters.parse_agy(rc, out, "")["ok"], label)

    def test_agy_structured_from_fenced_response(self):
        out = json.dumps(dict(AGY_RESULT, response='```json\n{"verdict": "ok"}\n```'))
        self.assertEqual(adapters.parse_agy(0, out, "")["structured"], {"verdict": "ok"})


class ClassifierTests(unittest.TestCase):
    TABLE = [
        ("claude", 1, "Claude AI usage limit reached|1700000000", "", "QUOTA", "usage-limit"),
        ("claude", 1, "", "You've hit your limit", "QUOTA", "usage-limit"),
        ("google", 1, "", "Quota exceeded for this account", "QUOTA", "quota-exhausted"),
        ("claude", 1, "", "Please run /login", "AUTH", "login-required"),
        ("google", 1, "", "You are not logged in", "AUTH", "login-required"),
        ("claude", 1, "", "Invalid authentication credentials", "AUTH", "invalid-credentials"),
        ("claude", 1, "", "HTTP 429 Too Many Requests", "TRANSIENT", "http-429"),
        ("claude", 1, "API Error: 529", "", "TRANSIENT", "http-529"),
        ("google", 1, "", "model is overloaded", "TRANSIENT", "overloaded"),
        ("google", 1, "", "Rate limit hit", "TRANSIENT", "rate-limit"),
        ("google", 1, "", "503 Service Unavailable", "TRANSIENT", "service-unavailable"),
        ("claude", 1, "", "usage limit reached (429)", "QUOTA", "usage-limit"),
        ("claude", 1, "", "unknown model: foo", "CONFIG", "invalid-model"),
        ("google", 127, "", "", "CONFIG", "binary-missing"),
        ("claude", None, "", "", "UNKNOWN", "timeout"),
        ("claude", 1, "", "segfault", "UNKNOWN", None),
        ("claude", 1, "", "request id 14290 failed", "UNKNOWN", None),
    ]

    def test_table(self):
        for family, rc, out, err, cls, name in self.TABLE:
            got, sig = adapters.classify_failure(family, rc, out, err)
            self.assertEqual((got, sig["name"] if sig else None), (cls, name), (out, err))

    def test_quota_and_auth_signatures_are_provisional(self):
        for cls, name, _, prov in adapters.SIGNATURES:
            self.assertEqual(prov, cls in (adapters.QUOTA, adapters.AUTH), name)
        self.assertTrue(adapters.classify_failure("claude", 1, "usage limit reached", "")[1]["provisional"])


class PrepareTests(Case):
    SCHEMA = {"type": "object"}

    def test_argv_shapes(self):
        self.assertEqual(adapters.build_argv("claude", "sonnet", "P"),
                         ["claude", "-p", "--permission-mode", "auto", "--model", "sonnet",
                          "--output-format", "json", "P"])
        self.assertEqual(adapters.build_argv("claude", "m", None, {"a": 1})[-2:], ["--json-schema", '{"a":1}'])
        self.assertEqual(adapters.build_argv("google", "m", "P", {"a": 1}),
                         ["agy", "-p", "P", "--model", "m", "--dangerously-skip-permissions", "--sandbox",
                          "--output-format", "json", "--json-schema", '{"a":1}'])
        with self.assertRaises(ValueError):
            adapters.build_argv("google", "m", None)

    def agy_strict_shim(self):
        # Like the real agy: exits 2 when the value after -p looks like a flag.
        self.shim("agy", 'case "$1" in -p) case "$2" in -*) echo "-p took $2 as its prompt" >&2; exit 2;; esac;; '
                  '*) exit 2;; esac; cat \'%s\'' % self.data_file("a.json", json.dumps(AGY_RESULT)))

    def test_agy_prompt_is_the_value_of_dash_p(self):
        self.agy_strict_shim()
        both = {"transport": {"google": {"schema": True, "stdin": True}}}
        for role, schema, survey in (("author", None, {}), ("reviewer", self.SCHEMA, {}),
                                     ("reviewer", self.SCHEMA, both)):
            inv = adapters.prepare("google", role, "m", "T", self.root, survey=survey, schema=schema)
            argv = inv["argv"]
            i = argv.index("-p")
            self.assertEqual(argv[i + 1], adapters.POINTER)
            self.assertEqual(inv["transport"], "file")
            self.assertFalse(argv[i + 1].startswith("-"))
            r = E.run_tool(argv, self.host_env(), 10, cwd=self.root)
            self.assertEqual(r["rc"], 0, r["stderr"])
        self.assertTrue(adapters.parse_agy(0, r["stdout"], "")["ok"])
        self.assertEqual(E.run_tool(["agy", "-p", "--sandbox", "x"], self.host_env(), 10)["rc"], 2)

    def test_agy_never_uses_stdin(self):
        with self.assertRaises(ValueError):
            adapters.prepare("google", "author", "m", "T", self.root, transport="stdin")

    def test_schema_flag_is_inline_compact_json_for_both_families(self):
        ok = {"transport": {"claude": {"schema": True}, "google": {"schema": True}}}
        for family in adapters.FAMILIES:
            inv = adapters.prepare(family, "reviewer", "m", "T", self.root, survey=ok, schema=self.SCHEMA)
            i = inv["argv"].index("--json-schema")
            self.assertEqual(json.loads(inv["argv"][i + 1]), self.SCHEMA)
            self.assertEqual(inv["argv"][i + 1], '{"type":"object"}')
            self.assertFalse(os.path.exists(os.path.join(self.root, "schema.json")))

    def test_file_transport_is_default_and_never_truncates(self):
        task = "x" * 500000
        inv = adapters.prepare("claude", "author", "sonnet", task, self.root)
        self.assertEqual((inv["transport"], inv["stdin"]), ("file", None))
        with open(os.path.join(self.root, "task.md")) as f:
            self.assertEqual(f.read(), task)
        self.assertLess(len(inv["argv"][-1]), 200)
        self.assertNotIn("\n", inv["argv"][-1])
        self.assertIn("task.md", inv["argv"][-1])

    def test_stdin_requires_recorded_true_probe(self):
        for t in ({}, {"claude": {"stdin": False}}, {"claude": {"stdin": "true"}}, {"claude": {"stdin": None}}):
            inv = adapters.prepare("claude", "author", "m", "T", self.root, survey={"transport": t})
            self.assertEqual(inv["transport"], "file", t)
        for s in (None, [], {"transport": []}, {"transport": {"claude": 1}}):
            self.assertEqual(adapters.prepare("claude", "author", "m", "T", self.root, survey=s)["transport"], "file")
        inv = adapters.prepare("claude", "author", "m", "T", self.root, survey={"transport": {"claude": {"stdin": True}}})
        self.assertEqual((inv["transport"], inv["stdin"], inv["argv"][-1]), ("stdin", "T", "json"))
        other = adapters.prepare("google", "author", "m", "T", self.root, survey={"transport": {"claude": {"stdin": True}}})
        self.assertEqual(other["transport"], "file")

    def test_reviewer_schema_flag_only_after_passing_probe(self):
        inv = adapters.prepare("google", "reviewer", "m", "T", self.root, schema=self.SCHEMA, survey={})
        self.assertNotIn("--json-schema", inv["argv"])
        with open(os.path.join(self.root, "task.md")) as f:
            self.assertIn('{"type":"object"}', f.read())
        ok = {"transport": {"google": {"schema": True}}}
        inv = adapters.prepare("google", "reviewer", "m", "T", self.root, schema=self.SCHEMA, survey=ok)
        self.assertIn("--json-schema", inv["argv"])
        with open(os.path.join(self.root, "task.md")) as f:
            self.assertNotIn("JSON schema", f.read())
        inv = adapters.prepare("google", "author", "m", "T", self.root, schema=self.SCHEMA, survey=ok)
        self.assertNotIn("--json-schema", inv["argv"])

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            adapters.prepare("claude", "judge", "m", "T", self.root)
        with self.assertRaises(ValueError):
            adapters.prepare("claude", "author", "m", "T", os.path.join(self.root, "missing"))


class SurveyTests(Case):
    def sinfo_shim(self, body='echo "batch-cpu*|1-00:00:00|up"'):
        for t in survey.SLURM_TOOLS:
            self.shim(t, 'echo "slurm 0.0"' if t != "sinfo" else 'case "$1" in --version) echo "slurm 0.0";; *) %s;; esac' % body)

    def test_slurm_disabled_never_runs_slurm_tools(self):
        self.sinfo_shim()
        s = survey.collect(self.root, self.host_env(OVERSEER_DISABLE_SLURM="1"))
        self.assertIs(s["slurm"]["usable"], False)
        self.assertTrue(s["tools"]["sinfo"]["found"])
        self.assertEqual([c for c in self.calls() if c.split()[0] in survey.SLURM_TOOLS], [])

    def test_slurm_usable_partitions_and_allocation(self):
        self.sinfo_shim()
        s = survey.collect(self.root, self.host_env(SLURM_JOB_ID="12"))
        self.assertIs(s["slurm"]["usable"], True)
        self.assertIs(s["slurm"]["in_allocation"], True)
        self.assertEqual(s["slurm"]["partitions"],
                         [{"name": "batch-cpu", "time_limit": "1-00:00:00", "availability": "up"}])
        self.assertIs(survey.collect(self.root, self.host_env())["slurm"]["in_allocation"], False)

    def test_slurm_missing_tools_is_unusable(self):
        s = survey.collect(self.root, self.host_env())
        self.assertIs(s["slurm"]["usable"], False)
        self.assertIn("sbatch", s["slurm"]["reason"])
        self.assertFalse(s["tools"]["sbatch"]["found"])

    def test_sinfo_timeout_is_unknown_not_absent(self):
        self.sinfo_shim("sleep 30")
        t = time.time()
        s = survey.collect(self.root, self.host_env(), timeout=1)
        self.assertLess(time.time() - t, 15)
        self.assertEqual((s["slurm"]["usable"], s["slurm"]["partitions"]), ("unknown", "unknown"))

    def test_slow_version_and_gh_are_unknown(self):
        self.shim("claude", "sleep 30")
        self.shim("gh", "sleep 30")
        s = survey.collect(self.root, self.host_env(), timeout=1)
        self.assertEqual((s["tools"]["claude"]["version"], s["gh_auth"]), ("unknown", "unknown"))
        self.assertTrue(s["tools"]["claude"]["found"])

    def test_gh_status_and_key_names_without_values(self):
        self.shim("gh", "exit 1")
        s = survey.collect(self.root, self.host_env(FAKE_API_KEY="sekrit", GH_TOKEN="t"))
        self.assertEqual(s["gh_auth"], "not authenticated")
        self.assertEqual(s["llm_key_vars"], ["FAKE_API_KEY"])
        self.assertIn("bills", s["warnings"][0])
        self.assertNotIn("sekrit", json.dumps(s))

    def test_command_writes_file_without_calling_a_model(self):
        self.shim("claude", 'echo "claude 0.0"')
        self.shim("agy", 'echo "agy 0.0"')
        rc, out, _ = self.run_cli("survey")
        self.assertEqual(rc, 0, out)
        s = self.state("survey.json")
        self.assertEqual(s["tools"]["claude"]["version"], "claude 0.0")
        self.assertNotIn("auth_probe", s)
        self.assertEqual(s["git"]["toplevel"], None)
        self.assertTrue([e for e in store.read_events(self.root) if e["kind"] == "survey"])
        self.assertTrue(all(c.endswith("--version") for c in self.calls()), self.calls())

    def test_probe_auth_runs_in_worker_env_and_records_source(self):
        ev = copy.deepcopy(CLAUDE_EVENTS)
        ev[0]["apiKeySource"] = "ANTHROPIC_API_KEY"
        self.shim("claude", 'case "$1" in --version) echo c;; *) echo "key=${ANTHROPIC_API_KEY:-unset}" >> \'%s\'; cat \'%s\';; esac'
                  % (self.log, self.data_file("c.json", json.dumps(ev))))
        self.shim("agy", 'case "$1" in --version) echo a;; *) cat \'%s\';; esac' % self.data_file("a.json", json.dumps(AGY_RESULT)))
        rc, _, _ = self.run_cli("survey", "--probe-auth", ANTHROPIC_API_KEY="sekrit")
        self.assertEqual(rc, 0)
        p = self.state("survey.json")["auth_probe"]
        self.assertEqual(p["claude"], {"status": "failed", "classification": "CONFIG", "api_key_source": "ANTHROPIC_API_KEY"})
        self.assertEqual(p["google"]["status"], "ok")
        self.assertIn("key=unset", self.calls())

    def test_plain_survey_keeps_earlier_probe_results(self):
        store.write_json_atomic(os.path.join(self.root, ".overseer", "survey.json"),
                                {"transport": {"claude": {"stdin": True}}})
        self.run_cli("survey")
        self.assertEqual(self.state("survey.json")["transport"], {"claude": {"stdin": True}})


class ProbeTransportTests(Case):
    def test_probe_records_results_and_survey_uses_them(self):
        stdin_log = os.path.join(self.tmp, "stdin.txt")
        ok = lambda text, **kw: json.dumps(CLAUDE_EVENTS[:-1] + [dict(CLAUDE_EVENTS[-1], result=text, **kw)])
        # Measured shape: the model nested JSON text inside the schema field.
        nested = ok('{"answer":"{\\"answer\\": \\"ok\\"}"}', structured_output={"answer": '{"answer": "ok"}'})
        self.shim("claude", 'case "$*" in *--json-schema*) cat \'%s\';; *) cat > \'%s\'; cat \'%s\';; esac'
                  % (self.data_file("s.json", nested), stdin_log, self.data_file("p.json", ok("PROBE-OK"))))
        self.shim("agy", "echo broken >&2; exit 1")
        rc, out, _ = self.run_cli("probe-transport")
        self.assertEqual(rc, 0, out)
        t = self.state("survey.json")["transport"]
        self.assertEqual(t["claude"], {"stdin": True, "schema": True})
        self.assertEqual(t["google"], {"stdin": "unsupported", "schema": False})
        with open(stdin_log) as f:
            self.assertIn("PROBE-OK", f.read())
        survey_doc = self.state("survey.json")
        inv = adapters.prepare("claude", "reviewer", "haiku", "T", self.root, survey=survey_doc, schema={"type": "object"})
        self.assertEqual((inv["transport"], inv["schema_flag"]), ("stdin", True))
        inv = adapters.prepare("google", "reviewer", "m", "T", self.root, survey=survey_doc, schema={"type": "object"})
        self.assertEqual((inv["transport"], inv["schema_flag"]), ("file", False))

    def test_schema_needs_native_structured_output(self):
        # JSON in the reply text alone does not show the CLI enforced the schema.
        text_only = json.dumps(CLAUDE_EVENTS[:-1] + [dict(CLAUDE_EVENTS[-1], result='{"answer": "ok"}')])
        self.shim("claude", 'case "$*" in *--json-schema*) cat \'%s\';; *) cat > /dev/null; echo x;; esac'
                  % self.data_file("t.json", text_only))
        self.run_cli("probe-transport")
        self.assertIs(self.state("survey.json")["transport"]["claude"]["schema"], False)

    def test_missing_cli_is_recorded_as_not_run(self):
        self.run_cli("probe-transport")
        t = self.state("survey.json")["transport"]
        self.assertEqual((t["claude"]["stdin"], t["claude"]["schema"]), (None, None))
        self.assertEqual(self.calls(), [])

    def test_probe_timeout_is_unknown(self):
        self.shim("claude", "sleep 30")
        with mock.patch.object(survey, "PROBE_TIMEOUT", 1):
            self.run_cli("probe-transport")
        self.assertEqual(self.state("survey.json")["transport"]["claude"], {"stdin": "unknown", "schema": "unknown"})


if __name__ == "__main__":
    unittest.main()
