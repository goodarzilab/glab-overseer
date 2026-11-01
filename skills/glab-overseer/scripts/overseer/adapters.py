"""CLI adapters: argv builders, prompt transport, result parsers, failure classifier.

Supported envelope: the output shapes measured for `claude -p --output-format
json` (event array) and `agy -p --output-format json` (one object). Anything
else parses as not ok. Quota and auth signatures are provisional until a real
sanitized failure confirms them.
"""
import json
import os
import re

from . import env as E

FAMILIES = ("claude", "google")
BINARY = {"claude": "claude", "google": "agy"}

QUOTA, AUTH, TRANSIENT, CONFIG, UNKNOWN = "QUOTA", "AUTH", "TRANSIENT", "CONFIG", "UNKNOWN"

# (class, name, regex, provisional). Checked in this order; first match wins,
# so a specific QUOTA or AUTH phrase beats a bare 429 in the same text.
SIGNATURES = [
    (CONFIG, "binary-missing", r"command not found|not executable", False),
    (CONFIG, "invalid-model", r"(invalid|unknown|unsupported|unrecognized) model|model[^\n]{0,40}(not found|does not exist)", False),
    (QUOTA, "usage-limit", r"usage limit (reached|exceeded)|(5-hour|weekly|daily|monthly) limit reached|you(?:'ve| have) hit your (usage )?limit", True),
    (QUOTA, "quota-exhausted", r"quota (exceeded|exhausted)|exceeded your (current )?quota|out of (credits|usage)", True),
    (AUTH, "login-required", r"please (run )?/?login|please log in|not logged in|login required|run [`']?(claude|agy) (auth )?login", True),
    (AUTH, "invalid-credentials", r"invalid (authentication )?credentials|authentication[_ ]error|oauth token (has )?expired", True),
    (TRANSIENT, "http-429", r"\b429\b", False),
    (TRANSIENT, "http-529", r"\b529\b", False),
    (TRANSIENT, "overloaded", r"overloaded", False),
    (TRANSIENT, "rate-limit", r"rate[ _-]?limit", False),
    (TRANSIENT, "service-unavailable", r"\b50[234]\b|service unavailable|temporarily unavailable|econnreset|connection reset", False),
]
_COMPILED = [(c, n, re.compile(p, re.I), prov) for c, n, p, prov in SIGNATURES]


def classify_failure(family, rc, stdout, stderr):
    """Return (class, signature) where signature is {"name", "provisional"} or None."""
    if family not in FAMILIES:
        raise ValueError("unknown family %r" % (family,))
    if rc is None:
        return UNKNOWN, {"name": "timeout", "provisional": False}
    text = "%s\n%s" % (stdout or "", stderr or "")
    if rc == 127:
        text += "\ncommand not found"
    for cls, name, rx, prov in _COMPILED:
        if rx.search(text):
            return cls, {"name": name, "provisional": prov}
    return UNKNOWN, None


def extract_json(text):
    """Return a JSON object from raw or fenced text, else None."""
    if not isinstance(text, str):
        return None
    candidates = [text.strip()]
    candidates += [m.group(1) for m in re.finditer(r"```(?:json)?[ \t]*\n(.*?)```", text, re.S | re.I)]
    for c in candidates:
        try:
            v = json.loads(c)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    return None


def _result(ok, family, rc, **kw):
    r = {"ok": ok, "family": family, "rc": rc, "text": None, "session_id": None, "num_turns": None,
         "usage": None, "api_key_source": None, "structured": None, "structured_native": False,
         "classification": None,
         "signature": None, "reason": None}
    r.update(kw)
    return r


def _fail(family, rc, stdout, stderr, reason, **kw):
    cls, sig = classify_failure(family, rc, kw.pop("classify_text", stdout), stderr)
    return _result(False, family, rc, classification=cls, signature=sig, reason=reason, **kw)


def _str_or_none(v):
    return v if isinstance(v, str) else None


def _int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def parse_claude(rc, stdout, stderr):
    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        return _fail("claude", rc, stdout or "", stderr, "stdout is not JSON")
    events = [data] if isinstance(data, dict) else data
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        return _fail("claude", rc, "", stderr, "output is not an event array or object")
    inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    results = [e for e in events if e.get("type") == "result"]
    present = bool(inits) and "apiKeySource" in inits[0]
    source = inits[0]["apiKeySource"] if present else None
    extra = {"api_key_source": source if isinstance(source, str) else None}
    if present and source != "none":
        return _result(False, "claude", rc, classification=CONFIG, reason="apiKeySource is not 'none'",
                       signature={"name": "api-key-billing", "provisional": False}, **extra)
    if not results:
        return _fail("claude", rc, "", stderr, "no result event", **extra)
    res = results[-1]
    text = res.get("result")
    status = res.get("api_error_status")
    if res.get("is_error") is not False or res.get("subtype") != "success" or rc != 0 or not isinstance(text, str):
        ctext = "%s %s" % (text if isinstance(text, str) else "", status if status is not None else "")
        return _fail("claude", rc, "", stderr, "result event is not a success", classify_text=ctext,
                     text=_str_or_none(text), session_id=_str_or_none(res.get("session_id")), **extra)
    structured = None
    native = isinstance(res.get("structured_output"), dict)
    if "structured_output" in res:
        structured = res["structured_output"] if native else None
    else:
        structured = extract_json(text)
    init_sid = inits[0].get("session_id") if inits else None
    return _result(True, "claude", rc, text=text, structured=structured, structured_native=native,
                   session_id=_str_or_none(res.get("session_id")) or _str_or_none(init_sid),
                   num_turns=_int_or_none(res.get("num_turns")),
                   usage=res["usage"] if isinstance(res.get("usage"), dict) else None, **extra)


def parse_agy(rc, stdout, stderr):
    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        return _fail("google", rc, stdout or "", stderr, "stdout is not JSON")
    if not isinstance(data, dict):
        return _fail("google", rc, "", stderr, "output is not one object")
    text = data.get("response")
    if data.get("status") != "SUCCESS" or rc != 0 or not isinstance(text, str):
        return _fail("google", rc, "", stderr, "status is not SUCCESS",
                     classify_text="%s %s" % (data.get("status"), text if isinstance(text, str) else ""),
                     text=_str_or_none(text), session_id=_str_or_none(data.get("conversation_id")))
    native = isinstance(data.get("structured_output"), dict)
    if "structured_output" in data:
        structured = data["structured_output"] if native else None
    else:
        structured = extract_json(text)
    return _result(True, "google", rc, text=text, structured=structured, structured_native=native,
                   session_id=_str_or_none(data.get("conversation_id")),
                   num_turns=_int_or_none(data.get("num_turns")),
                   usage=data["usage"] if isinstance(data.get("usage"), dict) else None)


def parse_result(family, rc, stdout, stderr):
    return (parse_claude if family == "claude" else parse_agy)(rc, stdout, stderr)


# ---- argv and prompt transport --------------------------------------------

def schema_arg(schema):
    return json.dumps(schema, separators=(",", ":"))


def build_argv(family, model, prompt=None, schema=None):
    """Full argv. `prompt` None means the prompt arrives on stdin (claude only).

    agy's `-p` consumes the next argument as the prompt, so the prompt must come
    right after it and no flag may follow `-p` directly. `--json-schema` takes
    inline compact JSON for both families.
    """
    if not isinstance(model, str) or not model:
        raise ValueError("a model is required for %s invocations" % (family,))
    if family == "claude":
        argv = ["claude", "-p", "--permission-mode", "auto", "--model", model, "--output-format", "json"]
        argv += ["--json-schema", schema_arg(schema)] if schema is not None else []
        return argv + ([prompt] if prompt is not None else [])
    if family == "google":
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("agy needs the prompt as the value of -p")
        argv = ["agy", "-p", prompt, "--model", model]
        argv += ["--dangerously-skip-permissions", "--sandbox", "--output-format", "json"]
        return argv + (["--json-schema", schema_arg(schema)] if schema is not None else [])
    raise ValueError("unknown family %r" % (family,))


def _probe_flag(survey, family, key):
    t = survey.get("transport") if isinstance(survey, dict) else None
    entry = t.get(family) if isinstance(t, dict) else None
    return isinstance(entry, dict) and entry.get(key) is True


TASK_FILE = ".overseer-task.md"
TASK_MARKER = "<!-- overseer task -->"
POINTER = "Read the file %s in the current directory and follow it exactly." % TASK_FILE


def _write_task(cwd, text):
    path = os.path.join(cwd, TASK_FILE)
    if os.path.lexists(path):
        try:
            with open(path, encoding="utf-8") as f:
                first = f.readline().rstrip("\n")
        except (OSError, UnicodeDecodeError):
            first = None
        if first != TASK_MARKER:
            raise ValueError("%s exists and was not written by overseer; refusing to overwrite" % path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(TASK_MARKER + "\n" + text)
    return path


def prepare(family, role, model, task_text, cwd, survey=None, schema=None,
            transport=None, schema_flag=None):
    """Write the task file (file transport) and return the invocation.

    `transport` and `schema_flag` override what the survey's probes allow
    (the probes themselves use the overrides). agy always uses the file
    transport. The schema goes in the prompt unless the survey recorded a
    passing `--json-schema` probe.
    """
    if role not in ("author", "reviewer"):
        raise ValueError("role must be author or reviewer")
    if not os.path.isdir(cwd):
        raise ValueError("working directory %r does not exist" % (cwd,))
    if transport is None:
        transport = "stdin" if family == "claude" and _probe_flag(survey, family, "stdin") else "file"
    if transport not in ("file", "stdin") or (transport == "stdin" and family != "claude"):
        raise ValueError("transport %r is not supported for %s" % (transport, family))
    use_flag = False
    text = task_text
    if role == "reviewer" and schema is not None:
        use_flag = _probe_flag(survey, family, "schema") if schema_flag is None else schema_flag
        if not use_flag:
            text += "\n\nReply with one JSON object matching this JSON schema:\n" + schema_arg(schema)
    prompt_file = None
    if transport == "file":
        prompt_file = _write_task(cwd, text)
    argv = build_argv(family, model, POINTER if transport == "file" else None,
                      schema if use_flag else None)
    return {"argv": argv, "stdin": text if transport == "stdin" else None, "transport": transport,
            "prompt_file": prompt_file, "schema_flag": use_flag, "cwd": cwd}


# ---- transport probes -------------------------------------------------------

PROBE_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


def _call(family, inv, env, timeout):
    r = E.run_tool(inv["argv"], env, timeout, cwd=inv["cwd"], stdin=inv["stdin"])
    if r["timed_out"]:
        return "unknown", None
    return None, parse_result(family, r["rc"], r["stdout"], r["stderr"])


def probe_transport(family, cwd, env, model=None, timeout=180):
    """One real call for stdin and one for --json-schema. Values: True, False, "unknown",
    or "unsupported" (agy has no stdin transport, so that call is not made)."""
    out = {}
    if family == "claude":
        inv = prepare(family, "author", model, "Reply with exactly the text PROBE-OK and nothing else.",
                      cwd, transport="stdin")
        state, res = _call(family, inv, env, timeout)
        out["stdin"] = state or bool(res["ok"] and "PROBE-OK" in res["text"])
    else:
        out["stdin"] = "unsupported"
    inv = prepare(family, "reviewer", model, 'Set the field "answer" to the word ok.',
                  cwd, schema=PROBE_SCHEMA, transport="file", schema_flag=True)
    state, res = _call(family, inv, env, timeout)
    # The flag works when the CLI itself returns a schema-valid structured_output.
    # The value is not judged: measured models sometimes nest JSON text in it.
    out["schema"] = state or bool(res["ok"] and res["structured_native"]
                                  and isinstance(res["structured"].get("answer"), str))
    return out
