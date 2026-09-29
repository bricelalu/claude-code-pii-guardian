"""Tests for the manifest placeholder renderer.

The rendered YAML is what the cluster runs, so a renderer bug is a deployment bug
that only shows up as a CrashLoopBackOff. These tests render the real manifest and
check the embedded code is still valid and still the code in the repo.
"""
import ast
import json
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.render_placeholders import render, resolve

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(BASE, "manifests", "60-mock-presidio.yaml")


def _block_scalar(rendered, key):
    """The content of a `  <key>: |` block scalar, dedented to column 0.

    YAML block scalars run until the next line indented no further than the key,
    so that boundary — not the next key name — is what terminates the content.
    """
    lines = rendered.split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{key}: |")
    key_indent = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for line in lines[start + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= key_indent:
            break
        body.append(line[key_indent + 2:] if line.strip() else "")
    return "\n".join(body)


class SubstituteTest(unittest.TestCase):
    def test_preserves_indentation(self):
        out, count = render("data:\n  f: |\n    __guardrail/mock_presidio.py__\n")
        self.assertEqual(count, 1)
        # Every line after the "f: |" key sits at the placeholder's own indent level.
        for line in out.split("\n")[2:]:
            self.assertTrue(line.startswith("    ") or not line.strip(), repr(line))

    def test_blank_lines_stay_blank(self):
        # A whitespace-only line inside a YAML block scalar is a content line, and a
        # stray-indent line would corrupt the embedded file.
        out, _ = render("  f: |\n    __guardrail/mock_presidio.py__\n")
        self.assertNotIn("    \n", out)
        self.assertIn("\n\n", out)

    def test_missing_file_fails_loudly(self):
        with self.assertRaises(SystemExit):
            resolve("guardrail/does_not_exist.py")

    def test_path_traversal_is_refused(self):
        with self.assertRaises(SystemExit):
            resolve("../outside.py")


class RealManifestTest(unittest.TestCase):
    """The manifest as committed must render, parse, and embed the repo's code."""

    @classmethod
    def setUpClass(cls):
        with open(MANIFEST, encoding="utf-8") as handle:
            cls.rendered, _ = render(handle.read())

    def test_no_placeholders_remain(self):
        leftovers = re.findall(r"^(\s*)__([A-Za-z0-9._/-]+)__\s*$", self.rendered, re.MULTILINE)
        self.assertEqual(leftovers, [])

    def test_embedded_python_is_valid(self):
        ast.parse(_block_scalar(self.rendered, "mock_presidio.py"))

    def test_embedded_python_matches_the_repo(self):
        with open(os.path.join(BASE, "guardrail", "mock_presidio.py"), encoding="utf-8") as handle:
            self.assertEqual(_block_scalar(self.rendered, "mock_presidio.py"),
                             handle.read().rstrip("\n"))

    def test_embedded_vocab_is_valid_json(self):
        vocab = json.loads(_block_scalar(self.rendered, "mock_presidio_vocab.json"))
        self.assertGreater(len(vocab["PERSON"]), 0)
        self.assertGreater(len(vocab["LOCATION"]), 0)

    def test_cli_round_trips(self):
        out = subprocess.run([sys.executable, os.path.join("scripts", "render_placeholders.py"), MANIFEST],
                             cwd=BASE, capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, self.rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
