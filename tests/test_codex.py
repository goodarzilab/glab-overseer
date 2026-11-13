"""codex as an opt-in, reviewer-only family.

Fixtures are `codex exec --json` stdout captured from codex-cli 0.169.0 (ChatGPT Enterprise, 2026-11-13),
with message text shortened."""
import json
import os
import tempfile
import time
import unittest

import test_u2 as U2
import test_u6 as U6
from test_u5 import jload

from overseer import adapters, models, report, store

OK = "\n".join([
    '{"type":"thread.started","thread_id":"01a1d619-86e8-7c02-a334-c667d957bb31"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"PROBE-OK"}}',
    '{"type":"turn.completed","usage":{"input_tokens":24196,"cached_input_tokens":0,'
    '"cache_write_input_tokens":0,"output_tokens":8,"reasoning_output_tokens":0}}',
]) + "\n"
BAD_MODEL = "\n".join([
    '{"type":"thread.started","thread_id":"01a1d619-77fe-7940-97a8-74c0a2a4efa8"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Model metadata for `gpt-nope-9` not found."}}',
    '{"type":"turn.started"}',
    '{"type":"error","message":"The \'gpt-nope-9\' model is not supported when using Codex with a ChatGPT account."}',
    '{"type":"turn.failed","error":{"message":"The \'gpt-nope-9\' model is not supported when using Codex with a ChatGPT account."}}',
]) + "\n"
CATALOG = {"models": [
    {"slug": "gpt-6.1-sol", "visibility": "list", "priority": 1},
    {"slug": "gpt-6-astra", "visibility": "list", "priority": 3},
    {"slug": "gpt-6.1-terra", "visibility": "list", "priority": 2},
    {"slug": "gpt-6-sol", "visibility": "list", "priority": 4},
    {"slug": "gpt-reserve", "visibility": "hide", "priority": 5},
    {"slug": "gpt-5.6-sol", "visibility": "list", "priority": 7},
    {"slug": "gpt-5.6-terra", "visibility": "list", "priority": 8},
    {"slug": "codex-auto-review", "visibility": "hide", "priority": 10},
]}


def codex_out(review=None, sid="codex-thread", error=None, cached=100):
    lines = [{"type": "thread.started", "thread_id": sid}, {"type": "turn.started"}]
    if error is not None:
        lines += [{"type": "error", "message": error}, {"type": "turn.failed", "error": {"message": error}}]
    else:
        lines += [{"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": json.dumps(review)}},
                  {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": cached,
                                                       "cache_write_input_tokens": 0, "output_tokens": 50,
                                                       "reasoning_output_tokens": 20}}]
    return "\n".join(json.dumps(l) for l in lines) + "\n"


class Parse(unittest.TestCase):
    def test_measured_success(self):
        r = adapters.parse_result("codex", 0, OK, "")
        self.assertEqual((r["ok"], r["text"], r["session_id"], r["num_turns"]),
                         (True, "PROBE-OK", "01a1d619-86e8-7c02-a334-c667d957bb31", 1))
        self.assertEqual(r["usage"]["input_tokens"], 24196)
        self.assertIsNone(r["structured"])

    def test_measured_unsupported_model_is_config(self):
        r = adapters.parse_result("codex", 1, BAD_MODEL, "Reading additional input from stdin...")
        self.assertEqual((r["ok"], r["classification"], r["signature"]["name"]), (False, "CONFIG", "invalid-model"))

    def test_final_json_message_is_structured(self):
        r = adapters.parse_result("codex", 0, codex_out(U6.PASS), "")
        self.assertEqual((r["structured"], r["structured_native"]), (U6.PASS, True))

    def test_quota_and_login_text_classify(self):
        q = adapters.parse_result("codex", 1, codex_out(error="You've hit your usage limit."), "")
        a = adapters.parse_result("codex", 1, codex_out(error="Not logged in. Run `codex login`."), "")
        self.assertEqual((q["classification"], a["classification"]), ("QUOTA", "AUTH"))

    def test_no_events_and_missing_final_message_fail(self):
        self.assertFalse(adapters.parse_result("codex", 0, "not json", "")["ok"])
        partial = OK.replace('"type":"turn.completed"', '"type":"turn.other"')
        self.assertFalse(adapters.parse_result("codex", 0, partial, "")["ok"])
        self.assertFalse(adapters.parse_result("codex", 1, OK, "")["ok"])


class Argv(unittest.TestCase):
    def setUp(self):
        self.cwd = tempfile.mkdtemp()
        self.td = tempfile.mkdtemp()

    def test_stdin_only_read_only_with_effort(self):
        inv = adapters.prepare("codex", "reviewer", "gpt-6.1-sol", "TASK", self.cwd, task_dir=self.td)
        self.assertEqual((inv["transport"], inv["stdin"], inv["prompt_file"]), ("stdin", "TASK", None))
        a = inv["argv"]
        self.assertEqual((a[:2], a[-1]), (["codex", "exec"], "-"))
        for flag in ("--json", "--ephemeral", "--skip-git-repo-check"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("-s") + 1], "read-only")
        self.assertEqual(a[a.index("-m") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="high"', a)
        self.assertNotIn("--output-schema", a)

    def test_schema_flag_writes_schema_file_outside_cwd(self):
        inv = adapters.prepare("codex", "reviewer", "m", "T", self.cwd, task_dir=self.td,
                               schema={"type": "object"}, schema_flag=True)
        path = inv["argv"][inv["argv"].index("--output-schema") + 1]
        self.assertEqual(path, os.path.join(os.path.realpath(self.td), "schema.json"))
        with open(path) as f:
            self.assertEqual(json.load(f), {"type": "object"})
        self.assertEqual(os.listdir(self.cwd), [])

    def test_schema_without_flag_goes_in_prompt(self):
        inv = adapters.prepare("codex", "reviewer", "m", "T", self.cwd, schema={"type": "object"}, schema_flag=False)
        self.assertIn("JSON schema", inv["stdin"])

    def test_author_role_and_file_transport_refused(self):
        with self.assertRaises(ValueError):
            adapters.prepare("codex", "author", "m", "T", self.cwd, task_dir=self.td)
        with self.assertRaises(ValueError):
            adapters.prepare("codex", "reviewer", "m", "T", self.cwd, task_dir=self.td, transport="file")


class Models(unittest.TestCase):
    def test_resolve_picks_newest_sol_and_terra_from_visible(self):
        slugs = models.parse_codex_listing(json.dumps(CATALOG))
        self.assertNotIn("gpt-reserve", slugs)
        self.assertEqual(models.resolve_codex(slugs), {"review": "gpt-6.1-sol", "small": "gpt-6.1-terra"})

    def test_listing_without_sol_or_unparseable_raises(self):
        with self.assertRaises(models.ModelError):
            models.resolve_codex(["gpt-6-astra"])
        with self.assertRaises(models.ModelError):
            models.parse_codex_listing("nope")

    def rec(self, **kw):
        r = {"date": "2026-11-13", "host": "claude", "raw": "", "choices": {
            "claude": dict(models.CLAUDE_ROLES), "google": {"available": False, "reason": "x"}}}
        r.update(kw)
        return r

    def test_cache_validation_and_reviewer_family(self):
        plain = self.rec()
        self.assertTrue(models._valid_cache(plain))
        self.assertEqual(models.reviewer_family(plain), "google")
        cx = self.rec(reviewer="codex")
        cx["choices"]["codex"] = {"available": True, "review": "gpt-6.1-sol", "small": "gpt-6.1-terra"}
        self.assertTrue(models._valid_cache(cx))
        self.assertEqual(models.reviewer_family(cx), "codex")
        self.assertFalse(models._valid_cache(self.rec(reviewer="codex")))  # opted in, no choices.codex
        self.assertFalse(models._valid_cache(dict(cx, reviewer="gpt")))
        self.assertIsNone(models.roles_for(plain, "codex"))


class Persist(U2.Case):
    def test_refresh_keeps_the_opted_in_reviewer_until_none(self):
        cat = os.path.join(self.tmp, "codex-models.json")
        with open(cat, "w") as f:
            json.dump(CATALOG, f)
        self.shim("codex", "cat '%s'" % cat)
        self.assertEqual(self.run_cli("models", "--host", "claude", "--reviewer", "codex")[0], 0)
        for args in ((), ("--refresh",)):  # the stale-cache warning suggests a plain --refresh
            rc, out, err = self.run_cli("models", "--host", "claude", *args)
            self.assertEqual(rc, 0, err)
            self.assertIn("reviewer: codex", out)
            self.assertEqual(models.load_valid(self.root)["reviewer"], "codex")
        rc, out, err = self.run_cli("models", "--host", "claude", "--reviewer", "none")
        self.assertEqual(rc, 0, err)
        self.assertIn("reviewer: google", out)
        self.assertNotIn("reviewer", models.load_valid(self.root))

    def test_a_failing_codex_listing_leaves_the_record_and_names_the_way_out(self):
        cat = os.path.join(self.tmp, "codex-models.json")
        with open(cat, "w") as f:
            json.dump(CATALOG, f)
        self.shim("codex", "cat '%s'" % cat)
        self.assertEqual(self.run_cli("models", "--host", "claude", "--reviewer", "codex")[0], 0)
        before = models.read_cache(self.root)
        self.shim("codex", "echo down >&2; exit 2")
        rc, _, err = self.run_cli("models", "--host", "claude", "--refresh")
        self.assertNotEqual(rc, 0)
        self.assertIn("models.json is unchanged", err)
        self.assertIn("--reviewer none", err)
        self.assertEqual(models.read_cache(self.root), before)

    def test_host_families_keep_the_measured_probe_schema(self):
        self.assertNotIn("additionalProperties", adapters.PROBE_SCHEMA)
        self.assertIs(adapters.CODEX_PROBE_SCHEMA["additionalProperties"], False)


class Usage(unittest.TestCase):
    def test_cached_tokens_split_out_of_input(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(codex_out(U6.PASS, cached=300))
        self.assertEqual(report._usage_of(f.name, "codex"), {"input": 700, "cache_read": 300, "cache_write": 0, "output": 50})
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as g:
            g.write(BAD_MODEL)
        self.assertIsNone(report._usage_of(g.name, "codex"))


class Review(U6.Case):
    def done_codex(self, google=True):
        self.done()
        g = ({"available": True, "plan": "gemini-9-argon-high", "code": "gemini-9-argon-high",
              "review": "gemini-9-flash-high"} if google else {"available": False, "reason": "agy not found"})
        store.write_json_atomic(os.path.join(self.root, ".overseer", "models.json"), {
            "date": time.strftime("%Y-%m-%d", time.gmtime()), "host": "claude", "raw": "", "reviewer": "codex",
            "choices": {"claude": dict(models.CLAUDE_ROLES), "google": g,
                        "codex": {"available": True, "review": "gpt-6.1-sol", "small": "gpt-6.1-terra"}}})

    def test_opted_in_codex_reviews(self):
        self.done_codex()
        self.reviewer("codex", codex_out(U6.PASS))
        rc, res, err = self.review()
        self.assertEqual((rc, res["verdict"], res["reviewer_family"], res["model"], res["same_family_fallback"]),
                         (0, "PASS", "codex", "gpt-6.1-sol", False), err)
        md = report.render(report.build(self.root, True))
        self.assertIn("by codex gpt-6.1-sol: PASS", md)  # the model name survives the report's allowlist
        (name, cwd, argv, count), = self.calls()
        self.assertEqual((name, count), ("codex", "0"))
        self.assertTrue(argv.endswith(" -"))
        rd = jload(os.path.join(self.rdir(), "round.json"))
        self.assertEqual((rd["reviewer_family"], rd["calls"][0]["session_id"]), ("codex", "codex-thread"))
        self.assertFalse(os.path.exists(os.path.join(self.rdir(), "c1", "task.md")))

    def test_codex_quota_falls_back_cross_family_first(self):
        self.done_codex()
        self.reviewer("codex", codex_out(error="You've hit your usage limit."), 1)
        self.reviewer("agy", U6.agy_out(U6.PASS, "agy-conv"))
        rc, res, err = self.review()
        self.assertEqual((rc, res["verdict"], res["reviewer_family"], res["same_family_fallback"]),
                         (0, "PASS", "google", False), err)
        md = report.render(report.build(self.root, True))
        self.assertIn("(fallback from the opted-in reviewer)", md)
        rd = jload(os.path.join(self.rdir(), "round.json"))
        self.assertIn("QUOTA from codex", rd["reviewer_fallback"])
        self.assertEqual([(c["purpose"], c["family"]) for c in rd["calls"]],
                         [("primary", "codex"), ("cross_fallback", "google")])

    def test_codex_then_google_quota_reaches_same_family_once(self):
        self.done_codex()
        self.reviewer("codex", codex_out(error="Not logged in. Run `codex login`."), 1)
        self.reviewer("agy", U6.agy_out(U6.PASS, error="usage limit reached"), 1)
        self.reviewer("claude", U6.claude_out(U6.PASS, "fallback-sess"))
        rc, res, err = self.review()
        self.assertEqual((rc, res["reviewer_family"], res["same_family_fallback"]), (0, "claude", True), err)
        self.assertEqual([c[0] for c in self.calls()], ["codex", "agy", "claude"])

    def test_codex_quota_without_google_goes_same_family(self):
        self.done_codex(google=False)
        self.reviewer("codex", codex_out(error="You've hit your usage limit."), 1)
        self.reviewer("claude", U6.claude_out(U6.PASS, "fallback-sess"))
        rc, res, err = self.review()
        self.assertEqual((rc, res["reviewer_family"], res["same_family_fallback"]), (0, "claude", True), err)

    def test_codex_transient_error_does_not_fall_back(self):
        self.done_codex()
        self.reviewer("codex", codex_out(error="HTTP 529 overloaded"), 1)
        rc, res, err = self.review()
        self.assertEqual((rc, res["state"]), (1, "failed"))
        self.assertEqual([c[0] for c in self.calls()], ["codex"])


if __name__ == "__main__":
    unittest.main()
