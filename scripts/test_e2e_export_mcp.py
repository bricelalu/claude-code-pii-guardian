"""The MCP end-to-end's own checks, offline.

scripts/e2e_export_mcp.py is the only place that asserts masking left the document
*usable* rather than merely free of PII, and it had a bug that made it report confident,
alarming nonsense: it tried every format against every tool_result, so the JSON document
parsed as CSV yielded zero rows and was reported as total data loss. It also collected
structure failures into a list that the exit status never consulted, so that run printed a
green verdict over a red check.

Both are the failure mode this project keeps paying for — a check that cannot fail, or
that fails for the wrong reason and gets tuned away. Neither is visible in a passing run,
so they are pinned here.

    python3 scripts/test_e2e_export_mcp.py
"""
import io
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import e2e_export as e2e_export_mod  # noqa: E402
import e2e_export_mcp as e2e  # noqa: E402
import fake_mcp_server as srv  # noqa: E402


def rows(i, **over):
    base = {"id": str(i), "firstname": "<PERSON>", "lastname": "<PERSON>",
            "address": f"{i} Rue des Jardins", "city": "<LOCATION>",
            "customer_email": "[EMAIL_REDACTED]", "phone_number": "<PHONE>",
            "zipcode": "33180", "country": "FR", "iban": "<IBAN>",
            "last_login_ip": "10.0.0.1", "birth_date": "2000-07-18",
            "created_at": "2026-01-01", "status": "active", "plan": "free",
            "customer_ref": f"C{i:04d}"}
    base.update(over)
    return base


class StructureReportTest(unittest.TestCase):
    def setUp(self):
        self.fifty = [rows(i) for i in range(1, 51)]

    def _body(self, data, fmt):
        return f"50 customer record(s), format={fmt}.\n\n{srv.render(data, fmt)}"

    def test_a_complete_document_in_each_format_is_ok(self):
        report, unlabelled = e2e.structure_report(
            [self._body(self.fifty, f) for f in ("json", "csv", "markdown")], 50)
        self.assertEqual(unlabelled, [])
        self.assertEqual(sorted(report), ["csv", "json", "markdown"])
        for fmt, r in report.items():
            self.assertFalse(r["bad"], f"{fmt}: {r['bad']}")
            self.assertEqual(r["row_counts"], [50])

    def test_each_body_is_judged_only_in_its_own_format(self):
        # The regression. JSON parsed as CSV has no delimiters and no header row, so it
        # yields zero rows; the first version called that total data loss.
        report, _ = e2e.structure_report([self._body(self.fifty, "json")], 50)
        self.assertEqual(list(report), ["json"])
        self.assertEqual(report["json"]["row_counts"], [50])
        self.assertFalse(report["json"]["bad"])

    def test_rows_dropped_by_masking_are_caught(self):
        # The failure this whole check exists for: nothing leaked, but 12 rows are gone,
        # so the model can no longer answer a question about customer 7.
        short = [r for r in self.fifty if r["id"] != "7"]
        report, _ = e2e.structure_report([self._body(short, "json")], 50)
        self.assertTrue(report["json"]["bad"])
        self.assertIn("49 rows", report["json"]["bad"][0])

    def test_a_document_that_no_longer_parses_is_caught(self):
        report, _ = e2e.structure_report(
            ['50 customer record(s), format=json.\n\n{"id":"1","firstname":"<PERSON>"'], 50)
        self.assertTrue(report["json"]["bad"])
        self.assertIn("does not parse", report["json"]["bad"][0])

    def test_a_torn_csv_quote_is_caught_by_the_row_count_not_the_parser(self):
        # Python's csv reader accepts an unterminated quote and hands back the field
        # anyway, so a CSV mangled by a guardrail parses cleanly and is caught only by the
        # 50-row assertion. Worth stating explicitly: it means the row count is the
        # load-bearing half of this check for CSV, not the parse succeeding.
        report, _ = e2e.structure_report(
            ['50 customer record(s), format=csv.\n\nid,firstname\n1,<PERSON>,"unterminated'], 50)
        self.assertTrue(report["csv"]["bad"])
        self.assertIn("1 rows", report["csv"]["bad"][0])

    def test_a_body_with_no_format_marker_is_not_silently_skipped(self):
        # Otherwise a guardrail that truncated the preamble would empty the set of bodies
        # to check, and a check that inspects nothing passes.
        report, unlabelled = e2e.structure_report(
            ["id,firstname\n1,<PERSON>\n", f"50 customer record(s), format=json.\n\n"
             + srv.render(self.fifty, "json")], 50)
        self.assertEqual(len(unlabelled), 1)
        self.assertEqual(list(report), ["json"])

    def test_a_lies_about_its_own_format_is_judged_as_what_it_claims(self):
        # The marker is the server's, written before any guardrail ran. A body claiming
        # csv while holding a JSON array must fail the csv check, not be quietly accepted.
        body = "50 customer record(s), format=csv.\n\n" + srv.render(self.fifty, "json")
        report, _ = e2e.structure_report([body], 50)
        self.assertIn("csv", report)
        self.assertTrue(report["csv"]["bad"])

    def test_the_preamble_is_itself_still_present_after_masking(self):
        # If the guardrail eats the preamble the marker is gone and the body becomes
        # unlabelled — which the caller turns into a failure, not a pass.
        kept = self._body(self.fifty, "csv")
        self.assertTrue(e2e.FORMAT_MARKER.search(kept.partition("\n\n")[0]))
        gone = srv.render(self.fifty, "csv")
        report, unlabelled = e2e.structure_report([gone], 50)
        self.assertEqual(report, {})
        self.assertEqual(len(unlabelled), 1)


class CompletenessTest(unittest.TestCase):
    """Every requested format has to be judged, not just the ones that showed up."""

    def test_a_format_whose_document_never_arrives_is_reported_as_missing(self):
        # markdown was absent from the first real run's report because LiteLLM persists
        # bodies asynchronously and the markdown request went last. The run still printed
        # a green verdict, having quietly checked two formats instead of three.
        report, _ = e2e.structure_report([self._one("csv")], 50)
        self.assertEqual(sorted(report), ["csv"])
        self.assertIsNone(report.get("markdown"))

    def test_the_poll_deduplicates_and_keeps_one_document_per_format(self):
        # Bodies repeat: every later request in a turn resends the earlier tool_results.
        # Without dedup, csv would be assessed three times and its row counts reported
        # three times, hiding a real regression behind repetition.
        calls = []

        def fake(since):
            calls.append(since)
            return [self._one("csv"), self._one("json")], None

        original = e2e.fetch_tool_results
        e2e.fetch_tool_results = fake
        try:
            texts, err, _ = e2e.fetch_all_tool_results(
                "t0", ["csv", "json", "markdown"], pause=0)
        finally:
            e2e.fetch_tool_results = original
        self.assertIsNone(err)
        self.assertEqual(len(texts), 2)
        self.assertGreater(len(calls), 1, "it should have polled for the missing format")

    def test_no_retry_when_every_format_is_already_present(self):
        calls = []

        def fake(since):
            calls.append(since)
            return [self._one(f) for f in ("csv", "json", "markdown")], None

        original = e2e.fetch_tool_results
        e2e.fetch_tool_results = fake
        try:
            texts, err, waited = e2e.fetch_all_tool_results(
                "t0", ["csv", "json", "markdown"], pause=0)
        finally:
            e2e.fetch_tool_results = original
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(texts), 3)
        self.assertEqual(waited, 0)

    @staticmethod
    def _one(fmt):
        data = [rows(i) for i in range(1, 51)]
        return f"50 customer record(s), format={fmt}.\n\n{srv.render(data, fmt)}"


class ScenarioPlumbingTest(unittest.TestCase):
    """What --scenario and --rows change about the run.

    Each of these feeds a number into a verdict. A row count that silently stays 50 while
    the scenario serves 5000 would fail every structure check for the wrong reason, and
    the failure would read as "the guardrails mangled the document".
    """

    def test_expected_rows_defaults_to_the_scenarios_own_count(self):
        self.assertEqual(e2e.expected_rows("customers-partial", None), 50)
        self.assertEqual(e2e.expected_rows("customers-oversized", None),
                         srv.SCENARIOS["customers-oversized"].rows)

    def test_a_row_cap_lowers_what_the_document_should_hold(self):
        self.assertEqual(e2e.expected_rows("customers-oversized", 120), 120)

    def test_the_scenario_reaches_the_server_as_an_argument_not_a_tool_argument(self):
        # A scenario parameter on list_customers would make the tool something no real CRM
        # exposes, and would change what the model is asked to call.
        args = e2e.server_args_for("customers-nested", None, 0, None)
        self.assertEqual(args, ["--scenario", "customers-nested"])
        tools = e2e.srv._dispatch("tools/list", {})["tools"]
        self.assertNotIn("scenario", tools[0]["inputSchema"]["required"])

    def test_knobs_only_appear_when_set(self):
        self.assertEqual(e2e.server_args_for("customers-partial", None, 0, None),
                         ["--scenario", "customers-partial"])
        self.assertEqual(
            e2e.server_args_for("customers-partial", 10, 250, "truncated"),
            ["--scenario", "customers-partial", "--rows", "10",
             "--delay-ms", "250", "--fail", "truncated"])

    def test_the_structure_check_is_judged_against_the_scenarios_count(self):
        # 50 rows against an expectation of 5000 must be reported, or the row-count half of
        # the structure check is decorative for every non-default scenario.
        body = f"5000 customer record(s), format=json.\n\n" \
               + srv.render([rows(i) for i in range(1, 51)], "json")
        report, _ = e2e.structure_report([body], 5000)
        self.assertTrue(report["json"]["bad"])
        self.assertIn("expected 5000", report["json"]["bad"][0])
        report, _ = e2e.structure_report([body], 50)
        self.assertFalse(report["json"]["bad"])

    def test_prose_scenarios_cannot_be_selected(self):
        # The checks below join on id and count rows. Pointed at prose they would find no
        # leak and no data loss, which prints as a pass — so the refusal is the safety
        # property, not the scenario.
        prose = {n for n, s in srv.SCENARIOS.items() if s.shape != "table"}
        self.assertTrue(prose, "the 6fw fixture should still exist")
        self.assertEqual(prose & set(e2e.TABULAR_SCENARIOS), set())
        self.assertTrue(set(e2e.TABULAR_SCENARIOS) <= set(srv.SCENARIOS))

    def test_the_driver_refuses_a_prose_scenario_loudly(self):
        src = (REPO / "scripts" / "e2e_export_mcp.py").read_text()
        self.assertIn("is not tabular", src)
        self.assertIn("pii-guardian-6fw", src,
                      "the refusal should name the work that would make it possible")

    def test_the_scale_scenario_is_past_the_echo_limit(self):
        # If the echo limit ever rises above the scale scenario, the run stops printing
        # that the model's replay was skipped — so assert the relationship, not the value.
        self.assertGreater(srv.SCENARIOS["customers-oversized"].rows, e2e.ECHO_MAX_ROWS)
        src = (REPO / "scripts" / "e2e_export_mcp.py").read_text()
        self.assertIn("echo check skipped", src,
                      "a skipped check has to say so, or it is indistinguishable from a pass")

    def test_a_generous_row_cap_keeps_the_echo_check_in_play(self):
        # The counterpart: the common case must not silently lose its stronger signal.
        self.assertLessEqual(e2e.expected_rows("customers-partial", 50), e2e.ECHO_MAX_ROWS)


class NestedLeakCheckTest(unittest.TestCase):
    """The leak check must see a PII column wherever the document puts it.

    The check asks "did a real value reach the provider", so it has to find the columns
    wherever they are. The first version only looked at top-level keys, and the
    customers-nested scenario then reported PASS over a document full of raw names —
    every covered column was skipped as absent, and a check that inspects nothing passes.
    That is the third time this harness has had a check that could not fail, and the
    first time the fixture was built specifically to catch one.
    """

    TRUTH = {"1": {"id": "1", "firstname": "martin", "lastname": "dupont",
                   "address": "2 Rue des Jardins", "city": "Bordeaux",
                   "zipcode": "33180", "customer_email": "martin@example.com"}}

    def _leaks(self, row):
        from e2e_export import unmasked_cells
        return unmasked_cells([row], self.TRUTH)

    def test_a_raw_value_nested_one_level_down_is_a_leak(self):
        row = {"id": "1", "customer": {"profile": {
            "firstname": "martin", "lastname": "dupont", "address": "2 Rue des Jardins",
            "city": "Bordeaux", "customer_email": "martin@example.com"}}}
        found = {(c, v) for _, c, v in self._leaks(row)}
        self.assertIn(("firstname", "martin"), found)
        self.assertIn(("city", "Bordeaux"), found)

    def test_a_token_nested_one_level_down_is_not_a_leak(self):
        row = {"id": "1", "customer": {"profile": {
            "firstname": "<PERSON>", "lastname": "<PERSON>", "address": "<LOCATION>",
            "city": "<LOCATION>", "customer_email": "[EMAIL_REDACTED]"}}}
        self.assertEqual(self._leaks(row), [])

    def test_a_partly_masked_nested_column_reports_just_the_raw_one(self):
        row = {"id": "1", "customer": {"profile": {
            "firstname": "<PERSON>", "lastname": "dupont"}}}
        self.assertEqual(self._leaks(row), [("1", "lastname", "dupont")])

    def test_a_leak_in_an_array_of_objects_is_found_too(self):
        row = {"id": "1", "orders": [{"ship_to": {"city": "Bordeaux"}}]}
        self.assertEqual(self._leaks(row), [("1", "city", "Bordeaux")])

    def test_a_flat_row_is_unaffected(self):
        # The Bash end-to-end shares this function, so the flattening must be a no-op for
        # the shape it has always been given.
        row = {"id": "1", "firstname": "martin", "city": "<LOCATION>"}
        self.assertEqual(self._leaks(row), [("1", "firstname", "martin")])

    def test_an_unjoinable_id_still_yields_no_verdict(self):
        # id itself mangled: without a join there is no ground truth to compare against,
        # and inventing one would manufacture a leak out of a formatting change.
        row = {"id": "<ID>", "customer": {"profile": {"firstname": "martin"}}}
        self.assertEqual(self._leaks(row), [])

    def test_the_known_gap_skip_still_works_when_nested(self):
        from e2e_export import UNCOVERED_COLUMNS, unmasked_cells
        row = {"id": "1", "customer": {"profile": {"zipcode": "33180",
                                                   "firstname": "<PERSON>"}}}
        kept = unmasked_cells([row], self.TRUTH, UNCOVERED_COLUMNS)
        self.assertEqual(kept, [], "zipcode is the known gap and must stay out of the verdict")
        reported = unmasked_cells([row], self.TRUTH, skip={"firstname"})
        self.assertEqual(reported, [("1", "zipcode", "33180")])


class CheckLogsPollingTest(unittest.TestCase):
    """The authoritative verdict must not be read from an incomplete window.

    LiteLLM persists request bodies asynchronously. The first version of this check read
    once, straight after the model returned, and on the customers-nested run it saw the
    one body already written — the tool-call turn, which carries no document — and
    reported "0 covered-column values reached the provider". Thirty seconds later the same
    query returned two more bodies, both carrying 12 raw addresses, and reported the leak.

    That is the failure this whole project is built to avoid, in the one place the design
    says the truth lives: a green verdict read from data that had not arrived yet. The
    model's own echo caught it, which is luck, not design — the echo is the weaker signal.
    """

    def _patch(self, seq):
        calls = []

        def fake(truth, since):
            calls.append(since)
            return seq[min(len(calls) - 1, len(seq) - 1)], None

        original = e2e_export_mod.check_logs_once
        e2e_export_mod.check_logs_once = fake
        self.addCleanup(lambda: setattr(e2e_export_mod, "check_logs_once", original))
        return calls

    def test_a_body_that_arrives_late_is_still_seen(self):
        # First read: only the tool-call turn. Second: the turn that carries the document.
        self._patch([{"rows": 1, "leaked": [], "gaps": []},
                     {"rows": 3, "leaked": [["t", "2 Rue des Jardins", "address"]]}])
        detail, err = e2e_export_mod.check_logs({}, "t0", pause=0)
        self.assertIsNone(err)
        self.assertEqual(detail["leaked"], [["t", "2 Rue des Jardins", "address"]])

    def test_it_stops_polling_once_the_body_count_stops_growing(self):
        calls = self._patch([{"rows": 3, "leaked": [], "gaps": []}])
        detail, _ = e2e_export_mod.check_logs({}, "t0", pause=0)
        self.assertEqual(len(calls), 2, "one read to see it, one to see it had not grown")
        self.assertTrue(detail["settled"])

    def test_a_leak_stops_the_scan_immediately(self):
        # More bodies cannot un-leak one, so there is nothing to gain by waiting out the
        # window — and failing fast is what keeps the common passing case quick.
        calls = self._patch([{"rows": 1, "leaked": [["t", "x@y.z", "customer_email"]]}])
        detail, _ = e2e_export_mod.check_logs({}, "t0", pause=0, attempts=9)
        self.assertEqual(len(calls), 1)
        self.assertTrue(detail["settled"])
        self.assertEqual(len(detail["leaked"]), 1)

    def test_the_default_window_is_long_enough_to_cover_a_late_write(self):
        # The observed failure: the count sat still for roughly half a minute and then
        # rose with the leaking body. A default window shorter than that reproduces the
        # bug at whatever pace the cluster happens to be under that day, so this pins the
        # relationship rather than the literal numbers.
        import inspect
        sig = inspect.signature(e2e_export_mod.check_logs)
        self.assertGreaterEqual(sig.parameters["pause"].default, 8)
        self.assertGreaterEqual(sig.parameters["attempts"].default * sig.parameters["pause"].default,
                                48)

    def test_a_read_that_gave_up_while_still_growing_is_not_clean(self):
        # The dangerous case. Running out of attempts is not evidence of anything, and
        # reporting it as a pass is the bug. It has to be visible as unsettled so the
        # driver can refuse to certify a run on it.
        calls = []
        rows = iter(range(1, 99))

        def fake(truth, since):
            calls.append(since)
            return {"rows": next(rows), "leaked": [], "gaps": []}, None

        original = e2e_export_mod.check_logs_once
        e2e_export_mod.check_logs_once = fake
        self.addCleanup(lambda: setattr(e2e_export_mod, "check_logs_once", original))
        detail, err = e2e_export_mod.check_logs({}, "t0", attempts=3, pause=0)
        self.assertIsNone(err)
        self.assertFalse(detail["settled"], "still growing after the last attempt")
        self.assertEqual(len(calls), 3)

    def test_the_wait_is_reported_so_a_slow_settle_is_visible(self):
        self._patch([{"rows": 1, "leaked": [], "gaps": []}])
        detail, _ = e2e_export_mod.check_logs({}, "t0", pause=0)
        self.assertIn("waited", detail)
        self.assertIn("settled", detail)

    def test_an_error_on_any_read_is_returned_not_swallowed(self):
        original = e2e_export_mod.check_logs_once
        e2e_export_mod.check_logs_once = lambda truth, since: (None, "pod unreachable")
        self.addCleanup(lambda: setattr(e2e_export_mod, "check_logs_once", original))
        detail, err = e2e_export_mod.check_logs({}, "t0", pause=0)
        self.assertIsNone(detail)
        self.assertIn("pod unreachable", err)

    def test_the_driver_refuses_to_certify_an_unsettled_read(self):
        # Otherwise the polling is only advisory and the bug is back with a delay.
        src = (REPO / "scripts" / "e2e_export_mcp.py").read_text()
        self.assertIn("settled", src,
                      "the driver must act on the settle flag, not just display it")

    def test_the_log_verdict_is_read_after_the_structure_check(self):
        # The ordering is the fix; the polling only makes it less likely to bite. Reading
        # the logs first is what let a body written half a minute later go unseen, and a
        # longer poll in the wrong place is still a race.
        src = (REPO / "scripts" / "e2e_export_mcp.py").read_text()
        self.assertLess(src.index("fetch_all_tool_results(run_started, formats)"),
                        src.index("check_logs(truth, run_started)"),
                        "the log read must come after the check with a positive "
                        "completeness criterion, not before it")

    def test_the_bash_driver_says_its_verdict_is_weaker_here(self):
        # It has no structure check to order against, so the limitation is documented
        # where someone will read it before trusting the number.
        src = (REPO / "scripts" / "e2e_export.py").read_text()
        self.assertIn("no structure check to run first", src)


class VerdictTest(unittest.TestCase):
    """The exit status, not just the printed table."""

    def test_a_structure_failure_reaches_the_failure_list(self):
        # Named explicitly because the first version collected these into a variable the
        # exit status never read: the run printed a FAIL line and a green verdict.
        src = (REPO / "scripts" / "e2e_export_mcp.py").read_text()
        self.assertIn('failures.append(f"structure:{fmt}")', src,
                      "a structure failure must be added to the list the exit status reads")
        self.assertNotIn("structure_failures", src,
                         "the dead accumulator is the bug; it must not come back")

    def test_a_format_with_no_document_is_a_failure_not_a_skip(self):
        src = (REPO / "scripts" / "e2e_export_mcp.py").read_text()
        self.assertIn("no document reached the provider to assess", src,
                      "an unassessable format must be reported, not omitted")


if __name__ == "__main__":
    # exit=False because this suite is also run in-process, and that makes the exit status
    # ours to report. Reporting it as 0 unconditionally — which is what this used to do —
    # means a failing test reads as a passing one to `task test` and to any CI, and the
    # only visible sign is a line of stderr nobody is watching. The whole point of running
    # the file is the status it returns.
    result = unittest.main(exit=False, verbosity=1).result
    print(f"ran {result.testsRun} tests", file=sys.stderr)
    sys.exit(0 if result.wasSuccessful() else 1)
