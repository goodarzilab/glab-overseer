"""Worker and supervisor environments, plus a timeout-safe subprocess runner.

`worker_env` is built from an allowlist, then a denylist removes LLM keys,
routing variables and GitHub tokens from the result (including explicit
extras). `supervisor_env` keeps everything except LLM entries, so `gh` still
authenticates.
"""
import fnmatch
import os
import signal
import subprocess
import tempfile

ALLOW = [
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_*", "TERM", "TZ", "TMPDIR",
    "XDG_*_HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS", "SLURM_*",
]
LLM_DENY = [
    "*_API_KEY", "ANTHROPIC_*", "CLAUDE_CODE_USE_*", "OPENAI_*", "OPENROUTER_*",
    "GEMINI_*", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "*_BASE_URL",
]
WORKER_DENY = LLM_DENY + ["*_TOKEN", "GH_*", "GITHUB_*", "AWS_*"]


def slurm_disabled(base=None):
    """OVERSEER_DISABLE_SLURM disables Slurm only when it is `1`, `true` or `yes` (case-insensitive);
    unset, empty or any other value means not disabled."""
    v = (os.environ if base is None else base).get("OVERSEER_DISABLE_SLURM")
    return isinstance(v, str) and v.strip().lower() in ("1", "true", "yes")


def _match(name, patterns):
    up = name.upper()
    return any(fnmatch.fnmatchcase(up, p.upper()) for p in patterns)


def _abs_path_only(env):
    """Drop empty and relative PATH entries so no child resolves a binary from the project."""
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(d for d in env["PATH"].split(os.pathsep) if d and os.path.isabs(d))
    return env


def worker_env(base=None, extras=None):
    base = os.environ if base is None else base
    out = {k: v for k, v in base.items() if _match(k, ALLOW)}
    out.update(extras or {})
    return _abs_path_only({k: v for k, v in out.items() if not _match(k, WORKER_DENY)})


def supervisor_env(base=None):
    base = os.environ if base is None else base
    return _abs_path_only({k: v for k, v in base.items() if not _match(k, LLM_DENY)})


def llm_key_names(base=None):
    """Names (never values) of LLM credential or routing variables present."""
    base = os.environ if base is None else base
    return sorted(k for k in base if _match(k, LLM_DENY))


def resolve_exe(name, path, cwd=None):
    """Absolute path of an executable, searching only absolute PATH entries; None if absent.

    Empty and relative entries are skipped so a project directory can never supply the binary.
    """
    if os.sep in name:  # an explicit relative path is relative to the directory the child will run in
        full = os.path.abspath(os.path.join(cwd, name)) if cwd else os.path.abspath(name)
        return full if os.path.isfile(full) and os.access(full, os.X_OK) else None
    for d in (path or "").split(os.pathsep):
        if d and os.path.isabs(d):
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None


def run_tool(argv, env, timeout, cwd=None, stdin=None):
    """Run argv with a timeout; returns dict(rc, stdout, stderr, timed_out).

    A timeout yields rc None and timed_out True (the whole process group is
    killed). A missing binary yields rc 127. Output goes through temporary
    files, so a detached grandchild holding the descriptors never delays the
    return. argv[0] is resolved with `resolve_exe` and spawned by absolute path.
    """
    exe = resolve_exe(argv[0], env.get("PATH", ""), cwd)
    if exe is None:
        return {"rc": 127, "timed_out": False, "stdout": "",
                "stderr": "command not found: %s (only absolute PATH entries are searched)" % argv[0]}
    with tempfile.TemporaryFile() as fin, tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
        if stdin is not None:
            fin.write(stdin.encode("utf-8"))
            fin.flush()
            fin.seek(0)
        try:
            p = subprocess.Popen([exe] + list(argv[1:]), env=env, cwd=cwd, start_new_session=True,
                                 stdin=fin if stdin is not None else subprocess.DEVNULL,
                                 stdout=fout, stderr=ferr)
        except OSError as e:
            return {"rc": 127, "stdout": "", "stderr": "command not found or not executable: %s" % e,
                    "timed_out": False}
        timed_out = False
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
        fout.seek(0)
        ferr.seek(0)
        return {"rc": None if timed_out else p.returncode, "timed_out": timed_out,
                "stdout": fout.read().decode("utf-8", "replace"),
                "stderr": ferr.read().decode("utf-8", "replace")}
