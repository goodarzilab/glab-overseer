"""Tracked-file hygiene: credential-shaped strings, home paths and private identifiers.

Defense in depth, not proof of confidentiality. Site identifiers (cluster, host and user names) are never
written in this repository; they come from an untracked file named by OVERSEER_PRIVATE_IDENTIFIERS, one
per line. Patterns are token-shaped so ordinary words such as `task-` never match. Every string that the
patterns would match is built at runtime in these tests, so this file does not match itself.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import helpers

CREDENTIALS = {
    "anthropic key": re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}"),
    "api key": re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{32,}"),
    "google key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "github token": re.compile(r"ghp_[A-Za-z0-9]{36}"),
    "github fine-grained token": re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    "slack token": re.compile(r"xox[bp]-[0-9]+"),
    "private key header": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
}
HOME_PATH = re.compile(r"/(?:home|Users)/[A-Za-z_][A-Za-z0-9._-]*/")
CHUNK = 1024 * 1024
OVERLAP = 64 * 1024  # longer than any token pattern here and any sane identifier
ID_CHARS = "A-Za-z0-9_"


def scan_text(text):
    hits = [name for name, rx in CREDENTIALS.items() if rx.search(text)]
    if HOME_PATH.search(text):
        hits.append("home path")
    return hits


def id_regex(ident):
    """An identifier matches case-insensitively, only at token boundaries (not inside a longer word)."""
    return re.compile("(?<![%s])%s(?![%s])" % (ID_CHARS, re.escape(ident), ID_CHARS), re.I)


def identifier_hits(items, banned):
    """(path, identifier) for every identifier found as a whole token in a path or its content."""
    rx = [(b, id_regex(b)) for b in banned]
    return [(rel, b) for rel, text in items for b, r in rx if r.search(text) or r.search(rel)]


def tracked_files(repo=None):
    """Tracked files plus untracked files that are not ignored (a change under review is scanned too)."""
    r = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                       cwd=repo or helpers.REPO, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return [p for p in r.stdout.split("\0") if p]


def chunks(path, size=CHUNK, overlap=OVERLAP):
    """Text of a file in pieces of `size` bytes that overlap by `overlap`, so no match is split across pieces.

    A symlink yields its target string (os.readlink), never the target file. Size never exempts a file.
    """
    if os.path.islink(path):
        yield os.readlink(path)
        return
    if not os.path.isfile(path):
        return
    overlap = min(overlap, size - 1)
    with open(path, "rb") as f:
        buf = b""
        while True:
            more = f.read(size - len(buf))
            buf += more
            if buf:
                yield buf.decode("utf-8", errors="replace")
            if not more or len(buf) < size:
                return
            buf = buf[-overlap:] if overlap else b""


def scan_file(path, rel, banned_rx=(), size=CHUNK, overlap=OVERLAP):
    """(credential/home-path hit names, identifiers found) for one path."""
    names, ids = [], []
    for text in chunks(path, size, overlap):
        names += [n for n in scan_text(text) if n not in names]
        ids += [b for b, rx in banned_rx if b not in ids and rx.search(text)]
    ids += [b for b, rx in banned_rx if b not in ids and rx.search(rel)]
    return names, ids


def scan_repo(repo, banned=(), skip=None, size=CHUNK, overlap=OVERLAP):
    """{rel: (names, ids)} for every file with a hit. `skip` is a path (the identifier list) never scanned."""
    skip = os.path.realpath(skip) if skip else None
    rx = [(b, id_regex(b)) for b in banned]
    hits = {}
    for rel in tracked_files(repo) or []:
        full = os.path.join(repo, rel)
        if skip and os.path.realpath(full) == skip and not os.path.islink(full):
            continue
        names, ids = scan_file(full, rel, rx, size, max(overlap, 2 * max([len(b) for b in banned] or [0])))
        if names or ids:
            hits[rel] = (names, ids)
    return hits


class ScannerTests(unittest.TestCase):
    def test_token_shaped_values_match(self):
        a = "A" * 36
        cases = {
            "anthropic key": "sk-ant-" + "x" * 20,
            "api key": "sk-" + "a1" * 16,
            "google key": "AIza" + "B" * 35,
            "github token": "ghp_" + a,
            "github fine-grained token": "github_pat_" + "Z" * 22,
            "slack token": "xoxb-" + "1234567890",
            "private key header": "-----BEGIN RSA " + "PRIVATE KEY-----",
        }
        for name, s in cases.items():
            self.assertIn(name, scan_text("x = '%s'\n" % s), name)
        self.assertIn("slack token", scan_text("xox" + "p-1234"))
        self.assertIn("home path", scan_text("/ho" + "me/someone/data"))
        self.assertIn("home path", scan_text("/Us" + "ers/someone/data"))

    def test_ordinary_words_do_not_match(self):
        for s in ("task-", "task-" + "a" * 40, ".overseer-task.md", "disk-" + "b" * 40, "mask-" + "c" * 40,
                  "sk-" + "a" * 31, "sk-ant-" + "x" * 19, "ghp_" + "A" * 35, "AIza" + "B" * 34,
                  "github_pat_" + "Z" * 21, "xoxb-abc", "-----BEGIN CERTIFICATE-----", "/home", "/usr/home/",
                  "see ~/.claude/skills/glab-overseer"):
            self.assertEqual(scan_text(s), [], s)


NESTED = "OVERSEER_HYGIENE_NESTED"


def run_nested(files, identifiers=None, links=None, env_extra=None):
    """Copy this test module into a throwaway git repo holding `files`, run it there, return the process."""
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="ovr-hyg-"))
    try:
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, "tests"))
        for name in ("helpers.py", "test_hygiene.py"):
            shutil.copy(os.path.join(helpers.REPO, "tests", name), os.path.join(repo, "tests", name))
        for rel, data in files.items():
            with open(os.path.join(repo, rel), "wb") as f:
                f.write(data)
        for rel, target in (links or {}).items():
            os.symlink(target, os.path.join(repo, rel))
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **{NESTED: "1"})
        env.pop("OVERSEER_PRIVATE_IDENTIFIERS", None)
        if identifiers is not None:
            path = identifiers(repo)
            env["OVERSEER_PRIVATE_IDENTIFIERS"] = path
        env.update(env_extra or {})
        return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_hygiene.py"],
                              cwd=repo, capture_output=True, text=True, env=env, timeout=120)
    finally:
        shutil.rmtree(tmp, True)


@unittest.skipIf(os.environ.get(NESTED), "nested run")
class EndToEndTests(unittest.TestCase):
    def test_identifier_list_inside_the_checkout_is_not_scanned_for_itself(self):
        ident = "planted" + "hostunique"

        def lst(repo):
            p = os.path.join(repo, "ids.txt")
            with open(p, "w") as f:
                f.write(ident + "\n")
            return p
        r = run_nested({"a.txt": b"clean\n"}, identifiers=lst)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        # the same identifier in another file is still found
        r = run_nested({"a.txt": ("see " + ident + "\n").encode()}, identifiers=lst)
        self.assertNotEqual(r.returncode, 0)

    def test_large_tracked_files_are_scanned(self):
        token = b"sk-" + b"ant-" + b"A" * 20
        r = run_nested({"big.txt": b"x" * (5 * 1024 * 1024 + 10) + b"\n" + token})
        self.assertNotEqual(r.returncode, 0, "a credential after 5 MiB was not found")
        self.assertIn("big.txt", r.stderr + r.stdout)

    def test_a_match_straddling_a_chunk_boundary_is_found(self):
        token = b"sk-" + b"ant-" + b"B" * 30
        for pad in (CHUNK - 3, CHUNK - 10, CHUNK + 5, 3 * CHUNK - 20):
            r = run_nested({"big.txt": b"y" * (pad - 1) + b" " + token + b"\n"})
            self.assertNotEqual(r.returncode, 0, pad)

    def test_symlink_target_string_is_scanned_not_the_target_file(self):
        token = "sk-" + "ant-" + "C" * 25
        r = run_nested({"ok.txt": b"clean\n"}, links={"lnk": token})
        self.assertNotEqual(r.returncode, 0)
        r = run_nested({"secret.txt": b"clean\n"}, links={"lnk": "secret.txt"})
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])


class IdentifierScannerTests(unittest.TestCase):
    def test_finds_a_planted_identifier_in_content_and_in_a_path(self):
        host = "planted-" + "host"
        items = [("a.txt", "runs on " + host.upper() + " today"), ("dir/" + host + ".md", "clean"), ("b.txt", "clean")]
        self.assertEqual(identifier_hits(items, [host]), [("a.txt", host), ("dir/" + host + ".md", host)])
        self.assertEqual(identifier_hits(items[2:], [host]), [])

    def test_identifiers_match_only_at_token_boundaries(self):
        ident = "la" + "b"
        items = [("a.md", "We encourage collabo" + "ration."), ("b.md", "run on the " + ident + " host"),
                 ("c.md", ident + "_x and x" + ident), ("d.md", "(" + ident.upper() + ")")]
        self.assertEqual(identifier_hits(items, [ident]), [("b.md", ident), ("d.md", ident)])

    def test_identifiers_with_punctuation_are_escaped(self):
        ident = "/ho" + "me/some.one-x"
        self.assertEqual(identifier_hits([("a", "at " + ident + "/data")], [ident]), [("a", ident)])
        self.assertEqual(identifier_hits([("a", "at /ho" + "me/someXone-x/data")], [ident]), [])
        mail = "a.b" + "@c-d.example"
        self.assertEqual(identifier_hits([("a", "mail " + mail.upper() + ".")], [mail]), [("a", mail)])


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.files = tracked_files()
        if self.files is None:
            self.skipTest("not a git checkout")

    def test_no_credentials_or_home_paths_in_tracked_files(self):
        self.assertTrue(self.files)
        listed = os.environ.get("OVERSEER_PRIVATE_IDENTIFIERS")
        hits = scan_repo(helpers.REPO, skip=listed)
        self.assertEqual(dict((k, v[0]) for k, v in hits.items() if v[0]), {}, "credential or home-path pattern")

    def test_no_private_identifiers_in_tracked_files(self):
        listed = os.environ.get("OVERSEER_PRIVATE_IDENTIFIERS")
        if not listed:
            self.skipTest("OVERSEER_PRIVATE_IDENTIFIERS is not set; the private-identifier scan was skipped")
        with open(listed, encoding="utf-8") as f:
            banned = [ln.strip().lower() for ln in f if ln.strip() and not ln.startswith("#")]
        self.assertTrue(banned, "%s lists no identifiers" % listed)
        hits = scan_repo(helpers.REPO, banned, skip=listed)
        self.assertEqual([(k, v[1]) for k, v in hits.items() if v[1]], [])


if __name__ == "__main__":
    unittest.main()
