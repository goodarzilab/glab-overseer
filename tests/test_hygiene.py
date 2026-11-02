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

# Every quantifier is bounded so a match is always shorter than OVERLAP and is seen whole in some buffer.
CREDENTIALS = {
    "anthropic key": re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,512}"),
    # sk-<48>, sk-proj-<long>, sk-ant-api03-<long>: 32+ token characters holding a hyphen-free run of 20+
    # (kebab-case slugs of short words never do)
    "api key": re.compile(r"(?<![A-Za-z0-9_-])sk-(?=[A-Za-z0-9_-]{32})(?=[A-Za-z0-9_-]{0,490}?[A-Za-z0-9_]{20})[A-Za-z0-9_-]{32,512}"),
    "google key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "github token": re.compile(r"ghp_[A-Za-z0-9]{36}"),
    "github fine-grained token": re.compile(r"github_pat_[A-Za-z0-9_]{22,255}"),
    "slack token": re.compile(r"xox[bp]-[0-9]{1,64}"),
    "private key header": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
}
HOME_PATH = re.compile(r"/(?:home|Users)/[A-Za-z_][A-Za-z0-9._-]{0,255}/")
CHUNK = 1024 * 1024
OVERLAP = 64 * 1024  # longer than any token pattern here and any sane identifier
ID_CHARS = "A-Za-z0-9_"


def found(rx, text, first=True, last=True):
    """True when rx matches with both neighbours inside the buffer or at the true start or end of the file.

    A buffer edge is never a token boundary: a match that touches an edge that is not the file's own edge is
    left for the next buffer, which holds it whole because buffers overlap.
    """
    pos = 0
    while True:
        m = rx.search(text, pos)
        if m is None:
            return False
        if (m.start() > 0 or first) and (m.end() < len(text) or last):
            return True
        pos = m.start() + 1


def scan_text(text, first=True, last=True):
    hits = [name for name, rx in CREDENTIALS.items() if found(rx, text, first, last)]
    if found(HOME_PATH, text, first, last):
        hits.append("home path")
    return hits


def id_regex(ident):
    """An identifier matches case-insensitively, only at token boundaries (not inside a longer word)."""
    return re.compile("(?<![%s])%s(?![%s])" % (ID_CHARS, re.escape(ident), ID_CHARS), re.I)


def identifier_hits(items, banned):
    """(path, identifier) for every identifier found as a whole token in a path or its content."""
    rx = [(b, id_regex(b)) for b in banned]
    return [(rel, b) for rel, text in items for b, r in rx if found(r, text) or found(r, rel)]


def tracked_files(repo=None):
    """Tracked files plus untracked files that are not ignored (a change under review is scanned too)."""
    r = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                       cwd=repo or helpers.REPO, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return [p for p in r.stdout.split("\0") if p]


def chunks(path, size=CHUNK, overlap=OVERLAP):
    """(text, first, last) for pieces of `size` bytes that overlap by `overlap`.

    A symlink yields its target string (os.readlink), never the target file. Size never exempts a file.
    """
    if os.path.islink(path):
        yield os.readlink(path), True, True
        return
    if not os.path.isfile(path):
        return
    overlap = min(overlap, size - 1)
    first = True
    with open(path, "rb") as f:
        buf = b""
        while True:
            more = f.read(size - len(buf))
            buf += more
            last = not more or len(buf) < size
            if buf or first:
                yield buf.decode("utf-8", errors="replace"), first, last
            if last:
                return
            first = False
            buf = buf[-overlap:] if overlap else b""


def scan_file(path, rel, banned_rx=(), size=CHUNK, overlap=OVERLAP):
    """(credential/home-path hit names, identifiers found) for one path."""
    names, ids = [], []
    for text, first, last in chunks(path, size, overlap):
        names += [n for n in scan_text(text, first, last) if n not in names]
        ids += [b for b, rx in banned_rx if b not in ids and found(rx, text, first, last)]
    ids += [b for b, rx in banned_rx if b not in ids and found(rx, rel)]
    return names, ids


def scan_repo(repo, banned=(), skip=None, size=CHUNK, overlap=OVERLAP):
    """{rel: (names, ids)} for every file with a hit. `skip` is a path (the identifier list) never scanned."""
    skip = set([os.path.abspath(skip), os.path.realpath(skip)]) if skip else set()
    rx = [(b, id_regex(b)) for b in banned]
    hits = {}
    for rel in tracked_files(repo) or []:
        full = os.path.join(repo, rel)
        if skip & set([os.path.abspath(full), os.path.realpath(full)]):
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


class Round2ScannerTests(unittest.TestCase):
    def test_project_style_keys_with_hyphens_match(self):
        for s in ("sk-" + "proj-" + "A" * 40, "sk-" + "proj-" + "Ab1_" * 12, "sk-" + "ant-" + "api03-" + "Zz9" * 10):
            self.assertTrue(scan_text("key = '%s'" % s), s)
        self.assertIn("api key", scan_text("sk-" + "proj-" + "A" * 40))
        for s in ("task-" + "A" * 50, "ask-" + "proj-" + "A" * 40, "sk-" + "learn-is-a-lowercase-kebab-case-name-here",
                  "sk-" + "proj-" + "A" * 10, "_sk-" + "proj-" + "A" * 40):
            self.assertEqual(scan_text(s), [], s)

    def tmpfile(self, data):
        d = tempfile.mkdtemp(prefix="ovr-hyg-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "f.txt")
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_a_chunk_edge_is_not_a_token_boundary(self):
        ident = "sun" + "beam"
        rx = [(ident, id_regex(ident))]
        # `sunbeam` ends exactly at the first edge, and the file goes on with `s`
        p = self.tmpfile(b" " * (CHUNK - len(ident)) + (ident + "s").encode())
        self.assertEqual(scan_file(p, "doc.txt", rx), ([], []))
        # the same word whole, with a space after the edge, is found
        p = self.tmpfile(b" " * (CHUNK - len(ident)) + (ident + " tail").encode())
        self.assertEqual(scan_file(p, "doc.txt", rx), ([], [ident]))
        # a word that starts exactly at the edge of the second buffer and continues a longer word
        p = self.tmpfile(b"x" * 899 + b"a" + (ident).encode() + b" " * 200)
        self.assertEqual(scan_file(p, "doc.txt", rx, size=1000, overlap=100)[1], [])
        p = self.tmpfile(b"x" * 899 + b" " + (ident).encode() + b" " * 200)
        self.assertEqual(scan_file(p, "doc.txt", rx, size=1000, overlap=100)[1], [ident])

    def test_a_credential_split_by_an_edge(self):
        tok = "sk-" + "ant-" + "Q" * 30
        # preceded by a letter: the second buffer begins exactly at the token and must not match it
        p = self.tmpfile(b"x" * 899 + b"a" + tok.encode() + b" " * 200)
        self.assertEqual(scan_file(p, "f", size=1000, overlap=100)[0], [])
        # a real token cut by the edge is still found whole in the next buffer
        for cut in (905, 920, 930, 960):
            p = self.tmpfile(b"x" * (cut - 4) + b" " + tok.encode() + b" " * 300)
            self.assertIn("anthropic key", scan_file(p, "f", size=1000, overlap=100)[0], cut)

    def test_a_symlinked_identifier_list_is_skipped(self):
        d = os.path.realpath(tempfile.mkdtemp(prefix="ovr-hyg-"))
        self.addCleanup(shutil.rmtree, d, True)
        ident = "planted" + "linkid"
        os.makedirs(os.path.join(d, "repo"))
        real = os.path.join(d, "elsewhere-" + ident + ".txt")
        with open(real, "w") as f:
            f.write(ident + "\n")
        link = os.path.join(d, "repo", "ids.txt")
        os.symlink(real, link)
        subprocess.run(["git", "init", "-q"], cwd=os.path.join(d, "repo"), check=True)
        self.assertEqual(scan_repo(os.path.join(d, "repo"), [ident], skip=link), {})
        self.assertEqual(scan_repo(os.path.join(d, "repo"), [ident], skip=real), {})
        self.assertTrue(scan_repo(os.path.join(d, "repo"), [ident]))  # without a skip the link target string matches


class Round3ScannerTests(unittest.TestCase):
    def test_lowercase_and_project_style_keys_match(self):
        for s in ("sk-" + "a" * 32, "sk-" + "a" * 48, "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz" * 2,
                  "sk-" + "ant-" + "api03-" + "abcdefghij" * 6, "sk-" + "proj-" + "A" * 40,
                  "sk-" + "proj-" + "ab_cd-" * 3 + "abcdefghijklmnopqrstuvwx"):
            self.assertIn("api key", scan_text("k = " + s + "\n"), s)

    def test_kebab_case_slugs_do_not_match(self):
        for s in ("sk-" + "learn-python-machine-learning-library-version-2024", "sk-" + "a" * 31,
                  "sk-" + "this-is-a-long-kebab-case-slug-with-many-short-words-in-it",
                  "task-" + "a" * 50, "ask-" + "a" * 50, "_sk-" + "a" * 40, "sk-" + "ant-" + "x" * 19):
            self.assertNotIn("api key", scan_text(s), s)

    def test_a_run_of_twenty_is_the_threshold(self):
        self.assertIn("api key", scan_text("sk-" + "a" * 20 + "-" + "b-" * 6))
        self.assertNotIn("api key", scan_text("sk-" + "a" * 19 + "-" + "b" * 19 + "-" + "c-" * 6))


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
