"""Tracked-file hygiene: credential-shaped strings, home paths and private identifiers.

Defense in depth, not proof of confidentiality. Site identifiers (cluster, host and user names) are never
written in this repository; they come from an untracked file named by OVERSEER_PRIVATE_IDENTIFIERS, one
per line. Patterns are token-shaped so ordinary words such as `task-` never match. Every string that the
patterns would match is built at runtime in these tests, so this file does not match itself.
"""
import os
import re
import subprocess
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
MAX_BYTES = 5 * 1024 * 1024


def scan_text(text):
    hits = [name for name, rx in CREDENTIALS.items() if rx.search(text)]
    if HOME_PATH.search(text):
        hits.append("home path")
    return hits


def identifier_hits(items, banned):
    """(path, identifier) for every identifier found, case-insensitively, in a path or its content."""
    return [(rel, b) for rel, text in items for b in banned if b in (text + "\n" + rel).lower()]


def tracked_files():
    """Tracked files plus untracked files that are not ignored (a change under review is scanned too)."""
    r = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                       cwd=helpers.REPO, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return [p for p in r.stdout.split("\0") if p]


def read(path):
    if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > MAX_BYTES:
        return None
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


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


class IdentifierScannerTests(unittest.TestCase):
    def test_finds_a_planted_identifier_in_content_and_in_a_path(self):
        host = "planted-" + "host"
        items = [("a.txt", "runs on " + host.upper() + " today"), ("dir/" + host + ".md", "clean"), ("b.txt", "clean")]
        self.assertEqual(identifier_hits(items, [host]), [("a.txt", host), ("dir/" + host + ".md", host)])
        self.assertEqual(identifier_hits(items[2:], [host]), [])


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.files = tracked_files()
        if self.files is None:
            self.skipTest("not a git checkout")

    def test_no_credentials_or_home_paths_in_tracked_files(self):
        self.assertTrue(self.files)
        for rel in self.files:
            text = read(os.path.join(helpers.REPO, rel))
            if text is not None:
                self.assertEqual(scan_text(text), [], "%s matches a credential or home-path pattern" % rel)

    def test_no_private_identifiers_in_tracked_files(self):
        listed = os.environ.get("OVERSEER_PRIVATE_IDENTIFIERS")
        if not listed:
            self.skipTest("OVERSEER_PRIVATE_IDENTIFIERS is not set; the private-identifier scan was skipped")
        with open(listed, encoding="utf-8") as f:
            banned = [ln.strip().lower() for ln in f if ln.strip() and not ln.startswith("#")]
        self.assertTrue(banned, "%s lists no identifiers" % listed)
        items = [(rel, read(os.path.join(helpers.REPO, rel)) or "") for rel in self.files]
        self.assertEqual(identifier_hits(items, banned), [])


if __name__ == "__main__":
    unittest.main()
