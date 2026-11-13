"""CLI adapters: argv builders, prompt transport, result parsers, failure classifier.

Supported envelope: the output shapes measured for `claude -p --output-format
json` (event array), `agy -p --output-format json` (one object) and
`codex exec --json` (JSON Lines events, measured with codex-cli 0.169.0).
Anything else parses as not ok.

`codex` is a reviewer-only family: it never hosts or authors. It always takes
the prompt on stdin, because its Linux command sandbox may fail to start on a
cluster node (measured on a lab cluster, 2026-11-13) and a reviewer needs no tools. Quota and auth signatures are provisional until a real
sanitized failure confirms them.
"""
import json
import os
import re
import tempfile

from . import env as E

FAMILIES = ("claude", "google", "codex")
HOST_FAMILIES = ("claude", "google")
BINARY = {"claude": "claude", "google": "agy", "codex": "codex"}
CODEX_EFFORT = "high"  # reasoning effort for codex review calls (`-c model_reasoning_effort=...`)

QUOTA, AUTH, TRANSIENT, CONFIG, UNKNOWN = "QUOTA", "AUTH", "TRANSIENT", "CONFIG", "UNKNOWN"

# (class, name, regex, provisional). Checked in this order; first match wins,
# so a specific QUOTA or AUTH phrase beats a bare 429 in the same text.
SIGNATURES = [
    (CONFIG, "binary-missing", r"command not found|not executable", False),
    (CONFIG, "invalid-model", r"(invalid|unknown|unsupported|unrecognized) model|model\b[^\n]{0,60}\b(is invalid|invalid|not found|does not exist|not supported|unsupported|unknown)\b", False),
    (QUOTA, "usage-limit", r"usage limit (reached|exceeded)|(5-hour|weekly|daily|monthly) limit reached|you(?:'ve| have) hit your (usage )?limit", True),
    (QUOTA, "quota-exhausted", r"quota (exceeded|exhausted)|exceeded your (current )?quota|out of (credits|usage)", True),
    (AUTH, "login-required", r"please (run )?/?login|please log in|not logged in|login required|run [`']?(claude|agy|codex) (auth )?login", True),
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
    declared = [e["apiKeySource"] for e in inits if "apiKeySource" in e]
    bad = [v for v in declared if v != "none"]
    source = bad[0] if bad else (declared[0] if declared else None)
    extra = {"api_key_source": source if isinstance(source, str) else None}
    if bad:
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


def parse_codex(rc, stdout, stderr):
    """`codex exec --json`: one JSON event per line. The reply is the last `agent_message` item; usage comes
    from `turn.completed`; `turn.failed` and `error` events carry failure text. Codex has no separate
    structured-output field: with `--output-schema` the final message itself is the JSON, so a final message
    that parses as one object counts as native structured output."""
    events = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            events.append(e)
    if not events:
        return _fail("codex", rc, stdout or "", stderr, "stdout has no JSON events")
    sid = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
    msgs = [e["item"].get("text") for e in events if e.get("type") == "item.completed"
            and isinstance(e.get("item"), dict) and e["item"].get("type") == "agent_message"]
    text = msgs[-1] if msgs and isinstance(msgs[-1], str) else None
    errors = [e.get("message") for e in events if e.get("type") == "error"]
    errors += [e["error"].get("message") for e in events if e.get("type") == "turn.failed" and isinstance(e.get("error"), dict)]
    errors = [m for m in errors if isinstance(m, str)]
    done = [e for e in events if e.get("type") == "turn.completed"]
    failed = any(e.get("type") == "turn.failed" for e in events)
    if rc != 0 or failed or not done or text is None:
        return _fail("codex", rc, "", stderr, "no completed turn with a final message",
                     classify_text="\n".join(errors), text=text, session_id=_str_or_none(sid))
    usage = {}
    for e in done:
        u = e.get("usage")
        if isinstance(u, dict):
            for k, v in u.items():
                if _int_or_none(v) is not None:
                    usage[k] = usage.get(k, 0) + v
    structured = extract_json(text)
    return _result(True, "codex", rc, text=text, structured=structured, structured_native=structured is not None,
                   session_id=_str_or_none(sid), num_turns=len(done), usage=usage or None)


_PARSERS = {"claude": parse_claude, "google": parse_agy, "codex": parse_codex}


def parse_result(family, rc, stdout, stderr):
    if family not in _PARSERS:
        raise ValueError("unknown family %r" % (family,))
    return _PARSERS[family](rc, stdout, stderr)


# ---- argv and prompt transport --------------------------------------------

def schema_arg(schema):
    return json.dumps(schema, separators=(",", ":"))


def build_argv(family, model, prompt=None, schema=None, schema_path=None):
    """Full argv. `prompt` None means the prompt arrives on stdin (claude and codex).

    codex reads the prompt from stdin (`-`) and its schema from a file (`schema_path`), never inline.

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
    if family == "codex":
        if prompt is not None:
            raise ValueError("codex takes the prompt on stdin only")
        if (schema is None) != (schema_path is None):
            raise ValueError("codex needs both the schema and its file path, or neither")
        argv = ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
                "-m", model, "-c", 'model_reasoning_effort="%s"' % CODEX_EFFORT]
        argv += ["--output-schema", schema_path] if schema_path else []
        return argv + ["-"]
    raise ValueError("unknown family %r" % (family,))


def _probe_flag(survey, family, key):
    """True only for a recorded True probe made with the CLI path and version the survey sees now."""
    if not isinstance(survey, dict):
        return False
    t = survey.get("transport")
    entry = t.get(family) if isinstance(t, dict) else None
    tools = survey.get("tools")
    cur = tools.get(BINARY[family]) if isinstance(tools, dict) else None
    if not (isinstance(entry, dict) and entry.get(key) is True and isinstance(cur, dict)):
        return False
    return all(isinstance(entry.get(k), str) and entry.get(k) == cur.get(k) for k in ("path", "version"))


def pointer(task_path):
    return "Read the file %s and follow it exactly." % task_path


def _task_path(cwd, task_dir, name="task.md"):
    """`<task_dir>/<name>`, absolute. The directory must be outside cwd and hold no such file yet."""
    if not task_dir:
        raise ValueError("file transport needs task_dir (a fresh directory outside the working directory)")
    td, wd = os.path.realpath(task_dir), os.path.realpath(cwd)
    if os.path.commonpath([td, wd]) == wd:
        raise ValueError("task_dir %r is inside the working directory" % (task_dir,))
    path = os.path.join(td, name)
    if os.path.lexists(path):
        raise ValueError("%s already exists; each invocation owns a fresh task_dir" % path)
    return path


def prepare(family, role, model, task_text, cwd, task_dir=None, survey=None, schema=None,
            transport=None, schema_flag=None):
    """Write `<task_dir>/task.md` (file transport) and return the invocation.

    Nothing is written into `cwd`. The prompt pointer names the task file's absolute path.

    `transport` and `schema_flag` override what the survey's probes allow
    (the probes themselves use the overrides). agy always uses the file
    transport. The schema goes in the prompt unless the survey recorded a
    passing `--json-schema` probe.
    """
    if role not in ("author", "reviewer"):
        raise ValueError("role must be author or reviewer")
    if not os.path.isdir(cwd):
        raise ValueError("working directory %r does not exist" % (cwd,))
    if family not in FAMILIES:
        raise ValueError("unknown family %r" % (family,))
    if family == "codex" and role != "reviewer":
        raise ValueError("codex is a reviewer-only family")
    if transport is None:
        if family == "codex":
            transport = "stdin"
        else:
            transport = "stdin" if family == "claude" and _probe_flag(survey, family, "stdin") else "file"
    if (transport not in ("file", "stdin") or (transport == "stdin" and family == "google")
            or (transport == "file" and family == "codex")):
        raise ValueError("transport %r is not supported for %s" % (transport, family))
    use_flag = False
    text = task_text
    if role == "reviewer" and schema is not None:
        use_flag = _probe_flag(survey, family, "schema") if schema_flag is None else schema_flag
        if not use_flag:
            text += "\n\nReply with one JSON object matching this JSON schema:\n" + schema_arg(schema)
    prompt_file = _task_path(cwd, task_dir) if transport == "file" else None
    schema_file = _task_path(cwd, task_dir, "schema.json") if (family == "codex" and use_flag) else None
    argv = build_argv(family, model, pointer(prompt_file) if prompt_file else None,
                      schema if use_flag else None, schema_path=schema_file)
    if schema_file:
        os.makedirs(os.path.dirname(schema_file), exist_ok=True)
        with open(schema_file, "x", encoding="utf-8") as f:
            f.write(schema_arg(schema))
    if prompt_file:
        os.makedirs(os.path.dirname(prompt_file), exist_ok=True)
        with open(prompt_file, "x", encoding="utf-8") as f:
            f.write(text)
    return {"argv": argv, "stdin": text if transport == "stdin" else None, "transport": transport,
            "prompt_file": prompt_file, "schema_flag": use_flag, "cwd": cwd}


# ---- transport probes -------------------------------------------------------

# additionalProperties false: codex (OpenAI strict structured outputs) rejects a schema without it.
PROBE_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
                "additionalProperties": False}


def _call(family, inv, env, timeout):
    r = E.run_tool(inv["argv"], env, timeout, cwd=inv["cwd"], stdin=inv["stdin"])
    if r["timed_out"]:
        return "unknown", None
    return None, parse_result(family, r["rc"], r["stdout"], r["stderr"])


def probe_transport(family, cwd, env, model=None, timeout=180):
    """One real call for stdin and one for --json-schema. Values: True, False, "unknown",
    or "unsupported" (agy has no stdin transport, so that call is not made). codex is probed with
    stdin for both, since it has no file transport."""
    out = {}
    if family == "codex":
        inv = prepare(family, "reviewer", model, "Reply with exactly the text PROBE-OK and nothing else.", cwd)
        state, res = _call(family, inv, env, timeout)
        out["stdin"] = state or bool(res["ok"] and "PROBE-OK" in res["text"])
    elif family == "claude":
        inv = prepare(family, "author", model, "Reply with exactly the text PROBE-OK and nothing else.",
                      cwd, transport="stdin")
        state, res = _call(family, inv, env, timeout)
        out["stdin"] = state or bool(res["ok"] and "PROBE-OK" in res["text"])
    else:
        out["stdin"] = "unsupported"
    with tempfile.TemporaryDirectory(prefix="ovr-task-") as task_dir:
        inv = prepare(family, "reviewer", model, 'Set the field "answer" to the word ok.', cwd,
                      task_dir=task_dir, schema=PROBE_SCHEMA, transport="stdin" if family == "codex" else "file",
                      schema_flag=True)
        state, res = _call(family, inv, env, timeout)
    # The flag works when the CLI itself returns a schema-valid structured_output.
    # The value is not judged: measured models sometimes nest JSON text in it.
    out["schema"] = state or bool(res["ok"] and res["structured_native"]
                                  and isinstance(res["structured"].get("answer"), str))
    return out
