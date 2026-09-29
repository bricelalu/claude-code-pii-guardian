"""The fake MCP server the end-to-end tests are built on.

Worth testing on its own, before any gateway or model is involved, for the same reason
scripts/test_manifest_consistency.py exists: if the server returns the wrong thing, an
end-to-end result is a statement about the server and reads as a statement about the
guardrails. Two tests here are about exactly that — a scenario that quietly returned an
already-clean table would make a run pass for free.

The scenario layer earns its own section because every knob is a claim about what the
guardrails do with a particular shape of leak. A scenario that does not reproduce its
tracked gap is worse than no scenario, because the end-to-end then reports green over a
guardrail that is still broken.

    python3 scripts/test_fake_mcp_server.py
"""
import csv
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "guardrail"))

import fake_mcp_server as srv  # noqa: E402

DB = REPO / "leaked_customers.db"
TRUTH = REPO / ".pii-score-out" / "export.csv"
TABLE = "customers_leaked"
FORMATS = ("json", "csv", "markdown")

# The bytes this server served before it grew scenarios, pinned so the end-to-end that
# already passes against them cannot change underneath us. If a refactor alters the
# document a guardrail sees, this fails rather than the end-to-end quietly measuring a
# different input.
LEGACY_PARTIAL_CSV_HEADER = (
    "id,customer_ref,firstname,lastname,customer_email,phone_number,address,zipcode,"
    "city,country,iban,last_login_ip,birth_date,created_at,status,plan")


def _have_db():
    return DB.is_file() and TRUTH.is_file()


class ScenarioTest(unittest.TestCase):
    """Each scenario is a claim about a specific failure mode. Check the claim."""

    @classmethod
    def setUpClass(cls):
        if not _have_db():
            raise unittest.SkipTest(f"missing {DB} / {TRUTH} — run scripts/create_leaked_db.py")

    def test_every_registered_scenario_is_described_and_reachable(self):
        # A scenario nobody documented is a scenario nobody will find again. The registry
        # is the interface, so the registry is what is checked.
        for name, spec in srv.SCENARIOS.items():
            with self.subTest(scenario=name):
                self.assertTrue(spec.description,
                                f"{name} must say what failure mode it reproduces")

    def test_the_default_scenario_is_the_one_the_end_to_end_already_uses(self):
        self.assertIn(srv.DEFAULT_SCENARIO, srv.SCENARIOS)
        doc = srv.document_of(srv.list_customers("csv"))
        self.assertEqual(doc.splitlines()[0], LEGACY_PARTIAL_CSV_HEADER)
        self.assertEqual(len(srv.parse_back(doc, "csv")), 50)

    def test_partial_is_the_half_masked_fixture_not_a_clean_table(self):
        # The control for every other assertion: if this were already clean the whole
        # harness would pass with the guardrails doing nothing.
        rows = srv.rows_for(srv.DEFAULT_SCENARIO)
        unmasked = [c for r in rows for c, v in r.items()
                    if c in srv.PII_COLUMNS and not srv.is_token(v)]
        self.assertGreater(len(unmasked), 100, f"expected a leaky fixture, got {len(unmasked)}")

    def test_raw_exposes_the_columns_the_guardrails_do_not_detect(self):
        # pii-guardian-ax0: zipcode and birth_date have no rule at all, so they are the
        # only place that gap is observable from outside. A "raw" scenario that still
        # masked them would make the gap invisible.
        rows = srv.rows_for("customers-raw")
        cells = [str(r.get(c, "")) for r in rows for c in ("zipcode", "birth_date")]
        self.assertTrue(cells)
        self.assertFalse(any(srv.is_token(c) for c in cells),
                         "raw must carry no masking token at all")

    def test_nested_puts_the_pii_one_level_down_and_keeps_id_reachable(self):
        rows = srv.rows_for("customers-nested")
        self.assertEqual(len(rows), 50)
        self.assertIn("id", rows[0], "the verifier joins on id; nesting must not hide it")
        for row in rows:
            self.assertIn("customer", row)
            self.assertIn("profile", row["customer"])
            self.assertIn("firstname", row["customer"]["profile"])

    def test_nested_is_no_longer_a_leak_that_leakguard_misses(self):
        # Re-pointed deliberately when pii-guardian-64g landed, as the test used to demand.
        # This scenario was the reproduction: a token at customer.profile.firstname was
        # invisible to _complete_json, the document was judged unmasked, and every sibling
        # value went to the provider raw. LeakGuard now reads leaf paths, so the nested
        # shape is a regression guard instead — it must come back masked.
        #
        # Judged with the E2E's own unmasked_cells rather than by looking for one name, so
        # this is the verdict the end-to-end would reach and not a proxy for it. The
        # capability the scenario used to provide — that the harness can *detect* a leak at
        # a nested path — is now pinned by NestedLeakCheckTest in test_e2e_export_mcp.py,
        # which says so in four cases and costs nothing to run.
        from e2e_export import unmasked_cells
        from leak_guard import LeakGuard
        doc = srv.render(srv.rows_for("customers-nested"), "json")
        seeded = json.loads(doc)
        seeded[0]["customer"]["profile"]["firstname"] = "<PERSON>"
        seeded[0]["customer"]["profile"]["city"] = "<LOCATION>"
        out = LeakGuard().complete(json.dumps(seeded, separators=(",", ":")))
        truth = {str(r["id"]): r["customer"]["profile"] for r in seeded}
        self.assertEqual(
            unmasked_cells(json.loads(out), truth), [],
            "a covered column came back raw from a nested document — LeakGuard regressed "
            "on pii-guardian-64g")

    def test_nested_is_partial_so_it_is_leakguard_and_not_codeguard_under_test(self):
        # The fixture is still half-masked, so the nested values that survive are the
        # ones CodeGuard's regexes do not recognise. That is deliberate: the scenario
        # isolates pii-guardian-64g rather than re-testing detection.
        rows = srv.rows_for("customers-nested")
        values = [str(r["customer"]["profile"]["firstname"]) for r in rows]
        self.assertTrue(any(srv.is_token(v) for v in values), "expected a seeded token")
        self.assertTrue(any(not srv.is_token(v) for v in values), "expected leaks beside it")

    def test_free_text_returns_prose_with_no_table(self):
        # pii-guardian-6fw: not tabular at all, so there is no column to read a token
        # from. A scenario that accidentally produced a table would be testing something
        # else entirely.
        body = srv.document_of(srv.list_customers("markdown", scenario="free-text"))
        table_lines = [ln for ln in body.splitlines() if ln.strip().startswith("|")]
        self.assertEqual(table_lines, [], "free-text must not contain a markdown table")

    def test_oversized_serves_more_rows_than_the_fixture_has(self):
        body = srv.document_of(srv.list_customers("json", scenario="customers-oversized"))
        rows = json.loads(body)
        self.assertGreater(len(rows), 50, "the scale scenario must exceed the fixture")

    def test_every_tabular_scenario_round_trips_in_every_format(self):
        # The property the guardrails' document detection rests on. A scenario that
        # serialises to something the parser cannot read back would make an end-to-end
        # failure look like a masking failure.
        for name, spec in srv.SCENARIOS.items():
            if spec.shape != "table":
                continue
            for fmt in FORMATS:
                with self.subTest(scenario=name, format=fmt):
                    doc = srv.document_of(srv.list_customers(fmt, scenario=name))
                    self.assertIsInstance(srv.parse_back(doc, fmt), list)

    def test_a_prose_scenario_gives_a_guardrail_no_column_to_work_from(self):
        # The property that makes free-text the pii-guardian-6fw fixture rather than a
        # near-duplicate of customers-partial: no PII column exists to read a token from.
        #
        # Stated as "no column", not "does not parse", because csv happily parses prose.
        # It takes the first prose line as a header and hands back one dict per
        # paragraph, keyed by fragments of a sentence — which is exactly the shape
        # `_looks_like_a_header` exists to reject, and why this scenario is worth having.
        for fmt in FORMATS:
            with self.subTest(format=fmt):
                doc = srv.document_of(srv.list_customers(fmt, scenario="free-text"))
                try:
                    parsed = srv.parse_back(doc, fmt)
                except Exception:  # noqa: BLE001 - any failure mode is an acceptable refusal
                    parsed = []
                self.assertFalse(
                    [k for row in parsed for k in row if k in srv.PII_COLUMNS],
                    f"{fmt} produced a real PII column out of prose")

    def test_an_unknown_scenario_names_the_ones_that_exist(self):
        with self.assertRaises(ValueError) as ctx:
            srv.list_customers("json", scenario="no-such-thing")
        self.assertIn("customers-partial", str(ctx.exception),
                      "the error must list what is available, not just say no")


class KnobTest(unittest.TestCase):
    """--rows, --delay-ms and --fail, orthogonal to the scenario."""

    @classmethod
    def setUpClass(cls):
        if not _have_db():
            raise unittest.SkipTest("missing fixture")

    def test_rows_overrides_the_scenario(self):
        self.assertEqual(len(srv.fetch(DB, TABLE, limit=7)), 7)

    def test_a_delay_is_actually_served(self):
        t0 = time.perf_counter()
        srv.list_customers("json", rows=1, delay_ms=250)
        self.assertGreaterEqual(time.perf_counter() - t0, 0.2)

    def test_fail_returns_an_error_result_not_a_crash(self):
        result = srv.call_tool("list_customers", {"format": "json"}, fail="error")
        self.assertTrue(result["isError"])
        self.assertIn("upstream", result["content"][0]["text"])

    def test_fail_truncated_returns_a_document_cut_mid_row(self):
        # A guardrail handed half a document must not silently treat the half as the
        # whole; this is the shape that made detection return None.
        result = srv.call_tool("list_customers", {"format": "csv"},
                               rows=50, fail="truncated")
        self.assertFalse(result["isError"])
        self.assertLess(len(result["content"][0]["text"]),
                        len(srv.list_customers("csv")))

    def test_bad_format_is_an_error_result(self):
        result = srv.call_tool("list_customers", {"format": "xlsx"})
        self.assertTrue(result["isError"])
        self.assertIn("json, csv, markdown", result["content"][0]["text"])


class ProtocolTest(unittest.TestCase):
    """The JSON-RPC a real MCP client speaks. Unchanged by the scenario layer."""

    def _rpc(self, *requests):
        payload = "".join(json.dumps(r) + "\n" for r in requests)
        proc = subprocess.run([sys.executable, str(Path(srv.__file__)), "--stdio"],
                              input=payload, capture_output=True, text=True, timeout=60,
                              cwd=str(REPO))
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_initialize_advertises_tools_and_echoes_the_protocol(self):
        replies = self._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2024-11-05",
                                        "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "1"}}})
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", replies[0]["result"]["capabilities"])

    def test_a_notification_gets_no_reply(self):
        # A real client sends notifications/initialized between initialize and
        # tools/list. Replying to it puts an extra object on the wire that cannot be
        # matched to a request, and the session fails at startup — so the reply count is
        # part of the contract, not just the reply contents.
        replies = self._rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual([r["id"] for r in replies], [1, 2])

    def test_tools_list_describes_the_format_argument(self):
        replies = self._rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = replies[-1]["result"]["tools"]
        schema = next(t for t in tools if t["name"] == "list_customers")["inputSchema"]
        self.assertEqual(sorted(schema["properties"]["format"]["enum"]), sorted(FORMATS))

    def test_tools_call_returns_the_document_as_text_content(self):
        replies = self._rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "list_customers", "arguments": {"format": "csv"}}})
        content = replies[-1]["result"]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(len(srv.parse_back(srv.document_of(content[0]["text"]), "csv")), 50)

    def test_the_document_travels_inside_a_json_string(self):
        # The MCP-specific shape: a document encoded as a string inside the JSON
        # envelope, so its newlines are backslash-n and its quotes are escaped.
        replies = self._rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "list_customers", "arguments": {"format": "json"}}})
        envelope = json.dumps(replies[-1]["result"])
        self.assertIn("\\n", envelope, "the document must be escaped inside the envelope")
        self.assertEqual(len(json.loads(
            srv.document_of(replies[-1]["result"]["content"][0]["text"]))), 50)

    def test_scenario_can_be_selected_from_the_command_line(self):
        proc = subprocess.run(
            [sys.executable, str(Path(srv.__file__)), "--print", "json",
             "--scenario", "customers-nested"],
            capture_output=True, text=True, timeout=60, cwd=str(REPO))
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        self.assertIn("customer", json.loads(srv.document_of(proc.stdout))[0])

    def test_an_unknown_scenario_on_the_command_line_exits_loudly(self):
        proc = subprocess.run(
            [sys.executable, str(Path(srv.__file__)), "--print", "json",
             "--scenario", "nope"],
            capture_output=True, text=True, timeout=60, cwd=str(REPO))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("customers-partial", proc.stderr)


class SerialiserTest(unittest.TestCase):
    """Every value goes through a real serialiser, and the round trip proves it."""

    def test_a_cell_containing_the_delimiter_survives_csv_and_markdown(self):
        # Customer notes are free text and routinely contain commas and quotes. A naive
        # string join would shift every column after the first such cell, which looks
        # exactly like a masking failure.
        rows = [{"id": "1", "note": 'Smith, John ("JJ")'}]
        self.assertEqual(srv.parse_back(srv.render(rows, "csv"), "csv")[0]["note"],
                         'Smith, John ("JJ")')
        self.assertEqual(srv.parse_back(srv.render(rows, "markdown"), "markdown")[0]["note"],
                         'Smith, John ("JJ")')

    def test_a_pipe_in_a_cell_does_not_end_a_markdown_cell_early(self):
        # A naive split yields three cells where there is one, so every column after the
        # pipe shifts and the value read for a column is the tail of a neighbour — which
        # reads exactly like a masking failure.
        rows = [{"id": "1", "note": "a | b"}]
        parsed = srv.parse_back(srv.render(rows, "markdown"), "markdown")
        self.assertEqual(parsed, rows)

    def test_markdown_has_a_separator_row(self):
        # LeakGuard's markdown detection requires one. A table without it is not a
        # document to the guardrail, so the end-to-end would measure cosmetics.
        lines = srv.render([{"a": "1", "b": "2"}], "markdown").splitlines()
        self.assertTrue(set(lines[1].replace("|", "").replace(" ", "")) <= {"-"})

    def test_json_is_compact(self):
        rows = [{"id": "1", "note": "x"}]
        doc = srv.render(rows, "json")
        self.assertNotIn("\n", doc.strip())
        self.assertEqual(json.loads(doc), rows)


if __name__ == "__main__":
    # exit=False because this suite is also run in-process, and that makes the exit status
    # ours to report. Reporting it as 0 unconditionally — which is what this used to do —
    # means a failing test reads as a passing one to `task test` and to any CI, and the
    # only visible sign is a line of stderr nobody is watching. The whole point of running
    # the file is the status it returns.
    result = unittest.main(exit=False, verbosity=1).result
    print(f"ran {result.testsRun} tests", file=sys.stderr)
    sys.exit(0 if result.wasSuccessful() else 1)
