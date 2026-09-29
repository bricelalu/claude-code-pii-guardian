"""Whether the test suite can report failure at all.

This file exists because of a defect that made 67 of the suite's tests unfalsifiable.
`scripts/test_fake_mcp_server.py` and `scripts/test_e2e_export_mcp.py` both ended with

    print(f"ran {unittest.main(exit=False, verbosity=1).result.testsRun} tests", ...)
    sys.exit(0)

`exit=False` is right — both suites are also run in-process — but it hands the exit status
to the caller, and the caller then reported 0 unconditionally. A failing test printed
`FAILED (failures=1)` to stderr and exited 0, so `task test` printed `ok` and `✓ offline
suite passed`, and so would any CI. The failure was visible in the output and invisible in
the status, which is the one place it could not be seen.

The checks are written against the AST rather than the text. The first draft matched on
substrings and promptly failed on itself, because this file has to *mention* `exit=False`
and `sys.exit(0)` in order to look for them. A check that trips over its own source gets
weakened instead of fixed, and a weakened check is worse than none.

It also needs a home whose own exit status it can trust. If it sat in one of the files it
inspects, breaking that file's exit code would kill the check at the moment it became
needed. Hence a separate file, and hence no `exit=False` here either.
"""

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASKFILE = REPO / "Taskfile.yml"

# test_integration.py is not unittest — it has its own main() and its own pass/fail shape,
# so the unittest idioms below do not apply to it. It is listed so the "every test file is
# in the suite" check still covers it.
NON_UNITTEST = {"guardrail/test_integration.py"}


def test_files():
    return sorted(
        str(p.relative_to(REPO))
        for p in REPO.rglob("test_*.py")
        if "__pycache__" not in p.parts and ".worktrees" not in p.parts
    )


def _is_main_guard(test):
    """Whether `test` is the `__name__ == "__main__"` comparison."""
    return (isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name) and test.left.id == "__name__"
            and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__")


def main_block(path):
    """The AST of `path`'s `if __name__ == "__main__":` body, or None if it has none."""
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            return node.body
    return None


def _calls(body):
    return [n for n in ast.walk(ast.Module(body=body, type_ignores=[]))
            if isinstance(n, ast.Call)]


def _attr_path(node):
    """'unittest.main' / 'sys.exit' / 'result.wasSuccessful' for a call's func, else ''."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def unittest_main_call(body):
    for call in _calls(body):
        if _attr_path(call.func) == "unittest.main":
            return call
    return None


class SuiteWiringTest(unittest.TestCase):
    def unittest_files(self):
        return [t for t in test_files() if t not in NON_UNITTEST]

    def test_every_test_file_is_in_the_task_test_list(self):
        # A test file nobody runs is a test that never fails, the quietest version of the
        # same defect. New files land silently otherwise.
        taskfile = TASKFILE.read_text(encoding="utf-8")
        missing = [t for t in test_files() if t not in taskfile]
        self.assertEqual(
            missing, [],
            "test file(s) absent from the `task test` list, so they never run: "
            + ", ".join(missing))

    def test_a_file_with_no_main_block_runs_nothing(self):
        # `python file.py` with no unittest.main() discovers nothing, prints nothing, and
        # exits 0. Indistinguishable from a passing suite.
        for path in self.unittest_files():
            with self.subTest(path=path):
                block = main_block(path)
                self.assertIsNotNone(
                    block, "no `if __name__ == \"__main__\"` block, so running this file "
                           "as a script executes zero tests and exits 0")
                self.assertIsNotNone(
                    unittest_main_call(block),
                    "unittest.main() is never called, so running this file as a script "
                    "executes zero tests and exits 0")

    def test_exit_false_hands_the_status_to_the_block_that_must_report_it(self):
        # The defect itself. unittest.main(exit=False) deliberately does not exit, so
        # whatever follows owns the status and has to carry the result's verdict out.
        for path in self.unittest_files():
            block = main_block(path)
            if block is None:
                continue
            call = unittest_main_call(block)
            if call is None:
                continue
            deferred = any(kw.arg == "exit" and isinstance(kw.value, ast.Constant)
                           and kw.value.value is False for kw in call.keywords)
            if not deferred:
                continue
            paths = [_attr_path(c.func) for c in _calls(block)]
            with self.subTest(path=path):
                self.assertTrue(
                    any(p.endswith(".wasSuccessful") for p in paths),
                    "unittest.main(exit=False) defers the exit status to this block, which "
                    "must exit on the result's own verdict; it currently exits 0 "
                    "unconditionally, so a failing test reads as a passing one")
                for call_ in _calls(block):
                    if _attr_path(call_.func) == "sys.exit" and call_.args:
                        arg = call_.args[0]
                        self.assertFalse(
                            isinstance(arg, ast.Constant) and arg.value == 0,
                            "sys.exit(0) after exit=False reports success whatever the "
                            "tests did")


if __name__ == "__main__":
    unittest.main(verbosity=2)
