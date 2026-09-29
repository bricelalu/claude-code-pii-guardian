"""Unit tests for the LeakGuard engine (leak_guard.py), independent of LiteLLM.

The integration test (test_integration.py) measures effectiveness on one fixture. These
pin the contract the adapter depends on: what counts as a data document, and the rule
that a document LeakGuard does not change comes back byte-identical.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from leak_guard import LeakGuard, _detect_format

PARTIAL_ROWS = [
    {"id": "1", "firstname": "<PERSON>", "lastname": "martin"},
    {"id": "2", "firstname": "bruno", "lastname": "<PERSON>"},
]

# LeakGuard completes *per column*: a column is finished only if it already holds a token.
# So the fixture has to put the token and the leak in the SAME column, which is what a
# partially-masked export actually looks like.
PARTIAL_CSV = "firstname,lastname\n<PERSON>,martin\nbruno,<PERSON>\n"
PARTIAL_MD = ("| firstname | lastname |\n|---|---|\n"
              "| <PERSON> | martin |\n| bruno | <PERSON> |\n")
PARTIAL_JSON = json.dumps(PARTIAL_ROWS, separators=(",", ":"))


class DetectFormatTest(unittest.TestCase):
    def test_json(self):
        self.assertEqual(_detect_format(json.dumps(PARTIAL_ROWS)), "json")

    def test_markdown_table(self):
        self.assertEqual(_detect_format("| a | b |\n|---|---|\n| 1 | 2 |"), "markdown_table")

    def test_csv(self):
        self.assertEqual(_detect_format("a,b\n1,2\n3,4\n"), "csv")

    def test_semicolon_csv(self):
        self.assertEqual(_detect_format("a;b\n1;2\n3;4\n"), "csv")

    def test_prose_is_not_a_document(self):
        self.assertIsNone(_detect_format("The quick brown fox jumps over the lazy dog."))

    def test_empty(self):
        self.assertIsNone(_detect_format("   "))


class UnchangedTest(unittest.TestCase):
    """A guardrail that rewrites what it did not mask is a guardrail that churns.

    Claude Code resends the whole conversation every turn, so a reformat here shows up as
    request diffs nobody can explain and defeats the adapter's cache.
    """

    def setUp(self):
        self.guard = LeakGuard()

    def test_already_masked_json_is_byte_identical(self):
        text = json.dumps([{"a": "<PERSON>"}, {"a": "<PERSON>"}], indent=2)
        self.assertEqual(self.guard.complete(text), text)

    def test_already_masked_csv_is_byte_identical(self):
        text = "a,b\n<PERSON>,<PERSON>\n"
        self.assertEqual(self.guard.complete(text), text)

    def test_already_masked_markdown_is_byte_identical(self):
        text = "| a |\n|---|\n| <PERSON> |\n|   <PERSON>   |\n"
        self.assertEqual(self.guard.complete(text), text)

    def test_document_without_tokens_is_byte_identical(self):
        text = "a,b\n1,2\n3,4\n"
        self.assertEqual(self.guard.complete(text), text)

    def test_idempotent(self):
        once = self.guard.complete(json.dumps(PARTIAL_ROWS))
        twice = self.guard.complete(once)
        self.assertEqual(json.loads(once), json.loads(twice))

    def test_csv_keeps_unix_line_endings(self):
        out = self.guard.complete("a,b\n<PERSON>,martin\n")
        self.assertNotIn("\r", out)

    def test_csv_keeps_windows_line_endings(self):
        out = self.guard.complete("a,b\r\n<PERSON>,martin\r\n")
        self.assertIn("\r\n", out)
        # No bare LF left over: the writer must not mix terminators.
        self.assertNotIn("\n", out.replace("\r\n", ""))


class CompletionTest(unittest.TestCase):
    def setUp(self):
        self.guard = LeakGuard()

    def test_json_column_completed(self):
        out = json.loads(self.guard.complete(json.dumps(PARTIAL_ROWS)))
        self.assertEqual([r["lastname"] for r in out], ["<PERSON>", "<PERSON>"])

    def test_non_pii_column_untouched(self):
        out = json.loads(self.guard.complete(json.dumps(PARTIAL_ROWS)))
        self.assertEqual([r["id"] for r in out], ["1", "2"])

    def test_empty_cells_are_not_filled(self):
        # An empty cell has no PII to mask; filling it would invent data.
        text = json.dumps([{"a": "<PERSON>"}, {"a": ""}])
        self.assertEqual(json.loads(self.guard.complete(text))[1]["a"], "")

    def test_explicit_format_hint(self):
        self.assertNotIn("martin", self.guard.complete(PARTIAL_CSV, format_hint="csv"))

    def test_explicit_markdown_hint(self):
        self.assertNotIn("martin", self.guard.complete(PARTIAL_MD, format_hint="markdown"))

    def test_a_column_with_no_token_is_left_alone(self):
        # firstname holds a token, so it is completed. status holds none, so there is no
        # basis to touch it and it survives. This is the documented scope, and it is also
        # why a wholly unmasked column is not LeakGuard's job — CodeGuard masks from scratch.
        text = "firstname,status\n<PERSON>,active\nbruno,inactive\n"
        out = self.guard.complete(text, format_hint="csv")
        self.assertNotIn("bruno", out)
        self.assertIn("active", out)
        self.assertIn("inactive", out)


# _detect_format accepts ',', ';', '|' and tab as CSV, so a semicolon-delimited export
# is recognised as a document. _complete_csv then parsed it with csv's default comma
# dialect, saw a single column literally named "firstname;lastname", found no token in
# it, and returned the input untouched. Detected-but-ignored is the worst shape of this
# bug: the format check says the document was understood, and nothing else says otherwise.
# Semicolon is not exotic either — it is the default in every French locale, and this
# project's own fixture is full of it.
class DelimiterTest(unittest.TestCase):
    def setUp(self):
        self.guard = LeakGuard()

    def _partial(self, d):
        return f"firstname{d}lastname\n<PERSON>{d}martin\nbruno{d}<PERSON>\n"

    def test_comma(self):
        out = self.guard.complete(self._partial(","))
        self.assertNotIn("martin", out)
        self.assertNotIn("bruno", out)

    def test_semicolon(self):
        out = self.guard.complete(self._partial(";"))
        self.assertNotIn("martin", out, "semicolon-delimited CSV must be completed")
        self.assertNotIn("bruno", out)

    def test_tab(self):
        out = self.guard.complete(self._partial("\t"))
        self.assertNotIn("martin", out, "tab-delimited CSV must be completed")
        self.assertNotIn("bruno", out)

    def test_pipe(self):
        out = self.guard.complete(self._partial("|"))
        self.assertNotIn("martin", out, "pipe-delimited must be completed")
        self.assertNotIn("bruno", out)

    def test_the_written_delimiter_is_the_one_that_was_read(self):
        # Sniffing the dialect only for reading would be half a fix: the document would
        # be parsed correctly and then re-emitted as commas, which changes the client's
        # data format to suit a guardrail. Whatever came in has to go back out.
        for d, label in ((";", "semicolon"), ("\t", "tab"), ("|", "pipe")):
            out = self.guard.complete(self._partial(d))
            self.assertEqual(out.count(d), 3, f"{label}: delimiter not preserved")
            self.assertNotIn(",", out.replace("\n", ""),
                             f"{label}: rewritten with a comma delimiter")

    def test_a_quoted_value_containing_the_delimiter_is_not_a_column_split(self):
        # The reason the dialect cannot simply be guessed from a per-line delimiter
        # count: a field may legitimately contain the delimiter inside quotes. Mis-split,
        # `"Paris, France"` becomes two fields, every field after it shifts, and the
        # completer then masks a cell that never held PII while leaving the real one.
        #
        # The note column holds no token, so it is never completed — which is what makes
        # the alignment observable: if the quoted field were split, these values would
        # not survive intact.
        text = 'firstname,note\n<PERSON>,"Paris, France"\nbruno,"kept as is"\n'
        out = self.guard.complete(text, format_hint="csv")
        self.assertNotIn("bruno", out, "firstname holds a token and must be completed")
        self.assertIn("Paris, France", out, "a quoted comma must not split the field")
        self.assertIn("kept as is", out)


# Claude Code appends its own <system-reminder> block to a Bash tool_result, after the
# command output. Verified against a live body recorded in LiteLLM_SpendLogs: a 50-row
# JSON export followed by ~2kB of MCP server instructions. Before SurroundedDocumentTest
# existed, _detect_format required the *whole* string to parse, so this text was
# unrecognised, LeakGuard returned it untouched, and 50 unmasked street addresses reached
# the provider. No error, no log line — the guardrail simply did not run.
SYSTEM_REMINDER = (
    "\n\n<system-reminder>\n# MCP Server Instructions\n\n"
    "The following MCP servers have provided instructions.\n\n"
    "## claude.ai Dropbox\nYou are the assistant for Dropbox's Claude App MCP server.\n"
    "</system-reminder>"
)


class SurroundedDocumentTest(unittest.TestCase):
    """A document inside surrounding text must still be detected, completed and preserved."""

    def setUp(self):
        self.guard = LeakGuard()

    def test_json_plus_system_reminder_is_detected(self):
        text = '[{"id":"1","address":"<LOCATION>"},{"id":"2","address":"2 Rue des Jardins"}]'
        self.assertEqual(_detect_format(text + SYSTEM_REMINDER), "json")

    def test_json_plus_system_reminder_is_completed(self):
        text = '[{"id":"1","address":"<LOCATION>"},{"id":"2","address":"2 Rue des Jardins"}]'
        out = self.guard.complete(text + SYSTEM_REMINDER)
        self.assertNotIn("2 Rue des Jardins", out,
                         "the address column holds a token, so it must be completed")

    def test_the_surrounding_text_survives_a_rewrite(self):
        # The other half of the bug. Fixing detection alone would make this worse: the
        # completer returns json.dumps(data), which is the serialized array and nothing
        # else, so the system-reminder — and with it the MCP instructions Claude Code
        # depends on — would be deleted from the conversation.
        out = self.guard.complete(SYSTEM_REMINDER.strip() + "\n" + PARTIAL_JSON)
        self.assertIn("<system-reminder>", out)
        self.assertIn("Dropbox", out)

    def test_csv_plus_trailing_prose_is_completed(self):
        out = self.guard.complete(PARTIAL_CSV + "\nExported 2 rows at 12:00.\n")
        self.assertNotIn("martin", out)
        self.assertIn("Exported 2 rows at 12:00.", out)

    def test_markdown_plus_trailing_prose_is_completed(self):
        out = self.guard.complete(PARTIAL_MD + "\nThat is the full table.\n")
        self.assertNotIn("martin", out)
        self.assertIn("That is the full table.", out)

    def test_a_json_string_containing_brackets_does_not_end_the_span_early(self):
        # Bracket counting that ignores string context stops at the "]" inside the value
        # and returns a truncated document, which either fails to parse or silently drops
        # every row after it.
        text = '[{"id":"1","note":"see [appendix] for details","address":"<LOCATION>"},' \
               '{"id":"2","address":"2 Rue des Jardins"}]'
        out = self.guard.complete(text)
        self.assertNotIn("2 Rue des Jardins", out)
        self.assertIn("[appendix]", out, "the bracketed value must survive intact")

    def test_unchanged_surrounded_document_is_returned_byte_identical(self):
        # Unchanged means unchanged, surrounding bytes included: Claude Code resends the
        # whole conversation every turn, so re-serializing for no gain churns the request
        # and defeats the cache the guardrail is trying to protect.
        text = '[{"id":"1","address":"<LOCATION>"},{"id":"2","address":"<LOCATION>"}]'
        self.assertEqual(self.guard.complete(text + SYSTEM_REMINDER), text + SYSTEM_REMINDER)

    def test_a_real_captured_tool_result_is_completed(self):
        """The exact shape that leaked in production: export, then MCP instructions.

        Built rather than checked in as a 23KB fixture, but every property is the one
        observed in a body recorded in LiteLLM_SpendLogs from a real Claude Code run:
        compact JSON, 50 rows, a column holding a token alongside leaking rows, and a
        ~2kB <system-reminder> after it.
        """
        rows = [{"id": str(i),
                 "firstname": "<PERSON>",
                 "lastname": "<PERSON>",
                 "address": "<LOCATION>" if i == 4 else f"{i} Rue des Jardins",
                 "city": "<LOCATION>",
                 "country": "<LOCATION>",
                 "zipcode": f"3318{i % 10}",
                 "birth_date": "2000-07-18"}
                for i in range(1, 51)]
        text = json.dumps(rows, separators=(",", ":")) + SYSTEM_REMINDER

        out = self.guard.complete(text)
        completed = {r["id"]: r for r in json.loads(out[:out.index("\n\n<system-reminder>")])}
        self.assertEqual(completed["4"]["address"], "<LOCATION>", "row 4 seeded the token")
        self.assertEqual(completed["1"]["address"], "<LOCATION>", "row 1 should be completed")
        self.assertEqual(len(completed), 50, "no row may be dropped")
        self.assertIn("Dropbox", out, "the system-reminder must survive")


# A document inside surrounding text is the normal case, not the edge case — Claude Code
# frames every tool_result, and a client's own preamble frames it too. Two ways that
# still went wrong after the span work, both found by probing rather than by reading.
class PreambleDetectionTest(unittest.TestCase):
    """Text before the document must not be mistaken for the document."""

    def setUp(self):
        self.guard = LeakGuard()

    # --- JSON: the first bracket is not always the document ---

    def test_bracket_prose_before_a_json_array_does_not_win(self):
        # "[1]" is itself valid JSON — an array holding the number 1. The span scanner
        # took the first "[" it found, matched its closing bracket, and accepted the
        # result, so the real array further down was never even considered. Detection
        # reported json and the document went untouched, which is the silent shape again:
        # the check says it understood the format, and the leak happens anyway.
        text = "See [1] for the method.\n" + PARTIAL_JSON
        self.assertEqual(_detect_format(text), "json")
        out = self.guard.complete(text)
        self.assertNotIn("martin", out, "the real array must be the one that is completed")
        self.assertIn("See [1] for the method.", out, "the prose must survive verbatim")

    def test_a_json_array_of_scalars_is_not_treated_as_a_table(self):
        # The shape that made the above dangerous: a document has to be able to hold
        # columns. A bare scalar array has no column to complete, so it is prose that
        # happens to be valid JSON, and must not shadow a real table behind it.
        text = '["a","b"]\n' + PARTIAL_JSON
        out = self.guard.complete(text)
        self.assertNotIn("martin", out)
        self.assertIn('["a","b"]', out)

    def test_several_arrays_are_searched_in_order(self):
        text = '[1]\n' + PARTIAL_JSON
        out = self.guard.complete(text)
        self.assertNotIn("martin", out)
        self.assertIn("[1]", out)

    # --- CSV: a preamble line is not a header row ---

    def test_a_preamble_line_containing_a_comma_is_not_the_header(self):
        # "Export complete, 50 rows." contains a comma, so the run of delimited lines
        # began there and the real header was treated as data. The result masked the
        # column names themselves — the header came back as "<PERSON>,<PERSON>" — and
        # re-quoted the prose. A leak is bad; destroying the header is worse, because
        # the client silently gets a table it can no longer read.
        text = ("Export complete, 50 rows.\n"
                "firstname,lastname\n<PERSON>,martin\nbruno,<PERSON>\n")
        out = self.guard.complete(text)
        self.assertIn("firstname,lastname", out, "the header must survive as the header")
        self.assertNotIn("Export complete,<PERSON>", out)
        self.assertIn("Export complete, 50 rows.", out, "the preamble must be untouched")
        self.assertNotIn("martin", out)

    def test_a_preamble_containing_several_commas_is_still_not_the_header(self):
        # Every field of this sentence starts with a letter, so a rule that only checked
        # name shape let it through and it became the header — which masked the real
        # column names. A sentence has spaces in it; a column name does not.
        text = ("Wrote firstname, lastname, and city for 50 rows.\n"
                "firstname,lastname\n<PERSON>,martin\nbruno,<PERSON>\n")
        out = self.guard.complete(text)
        self.assertIn("firstname,lastname", out)
        self.assertNotIn("martin", out)
        self.assertIn("Wrote firstname, lastname, and city for 50 rows.", out)

    def test_a_genuine_numeric_column_name_still_works(self):
        # The rule that separates a header from prose is that header fields are names,
        # and a name does not start with a digit. If a document really does have a
        # numeric first column name, that heuristic has to degrade rather than mangle it.
        text = "2019,2018\n<PERSON>,martin\nbruno,<PERSON>\n"
        out = self.guard.complete(text)
        self.assertNotIn("martin", out)
        self.assertIn("2019,2018", out, "a numeric header must not be masked away")

    def test_a_log_prefix_line_before_a_csv_table(self):
        # Timestamps are what actually precedes a table when the table is a tail of a
        # log file, and a log prefix contains colons rather than commas — but the run
        # must not be pulled backwards to include it either way.
        text = ("2026-09-28 12:00:00 INFO starting export\n"
                "firstname,lastname\n<PERSON>,martin\nbruno,<PERSON>\n")
        out = self.guard.complete(text)
        self.assertIn("firstname,lastname", out)
        self.assertNotIn("martin", out)
        self.assertIn("2026-09-28 12:00:00 INFO starting export", out)

    # --- Markdown: a pipe run is not a table without a separator ---

    def test_pipe_prose_is_not_mistaken_for_a_markdown_table(self):
        # "| a | b |" with no |---|---| row is not a table, it is a sentence with bars
        # in it. The span must not claim it, or a rewrite would re-space prose.
        text = "a | b\n" + PARTIAL_MD
        out = self.guard.complete(text)
        self.assertIn("| firstname | lastname |", out)
        self.assertIn("|---|---|", out)
        self.assertNotIn("martin", out)


# A PII column is a leaf path, not a top-level key. `_complete_json` read only the top
# level, so a token at customer.profile.firstname was not seen, the document was judged
# unmasked, and every sibling value went to the provider raw. Depth was binary: the top
# level worked, one level down did not, and sharing a key name at the top level did not
# help, because the check was positional rather than name-based (pii-guardian-64g).
class NestedJsonTest(unittest.TestCase):
    def setUp(self):
        self.guard = LeakGuard()

    def complete(self, rows):
        return json.loads(self.guard.complete(json.dumps(rows, separators=(",", ":"))))

    def test_token_two_levels_down_completes_its_siblings(self):
        # Two nested columns, each with a token of its own, so this also shows the token
        # is read from the column being completed rather than from the first one found.
        out = self.complete([
            {"id": "1", "customer": {"profile": {"firstname": "<PERSON>", "city": "Lyon"}}},
            {"id": "2", "customer": {"profile": {"firstname": "bruno", "city": "<LOCATION>"}}},
        ])
        self.assertEqual(out[1]["customer"]["profile"]["firstname"], "<PERSON>")
        self.assertEqual(out[0]["customer"]["profile"]["city"], "<LOCATION>")
        self.assertEqual([r["id"] for r in out], ["1", "2"])

    def test_token_deeper_still_completes(self):
        out = self.complete([
            {"order": {"lines": {"0": {"sku": "<PERSON>", "qty": 2}}}},
            {"order": {"lines": {"0": {"sku": "mattin", "qty": 1}}}},
        ])
        self.assertEqual(out[1]["order"]["lines"]["0"]["sku"], "<PERSON>")

    def test_columns_at_different_depths_are_kept_apart(self):
        # Two different columns that happen to share a name. Collapsing them on the last
        # path segment would mask `customer` because `profile.customer` is masked.
        out = self.complete([
            {"customer": "bruno", "p": {"customer": "<PERSON>"}},
            {"customer": "martin", "p": {"customer": "dupont"}},
        ])
        self.assertEqual([r["customer"] for r in out], ["bruno", "martin"])
        self.assertEqual(out[1]["p"]["customer"], "<PERSON>")

    def test_a_nested_column_with_no_token_is_left_alone(self):
        # `status` is nested and holds no token, so there is no basis to touch it. Depth
        # is not consent.
        out = self.complete([
            {"p": {"firstname": "<PERSON>", "status": "active"}},
            {"p": {"firstname": "bruno", "status": "inactive"}},
        ])
        self.assertEqual([r["p"]["status"] for r in out], ["active", "inactive"])

    def test_list_elements_are_cells_of_one_column(self):
        # `tags` holds a token, so "vip" beside it is a partially-masked cell and is
        # completed like any other. The rule is per column, not per type: a CSV column
        # with one token in it is completed throughout, and a list is a column. An index
        # in the path would have made every element its own column and completed nothing.
        out = self.complete([{"tags": ["<PERSON>", "vip"]},
                             {"tags": ["bruno", "new"]}])
        self.assertEqual(out, [{"tags": ["<PERSON>", "<PERSON>"]},
                               {"tags": ["<PERSON>", "<PERSON>"]}])

    def test_objects_in_a_list_share_one_column(self):
        out = self.complete([
            {"items": [{"sku": "<PERSON>"}, {"sku": "durable"}]},
            {"items": [{"sku": "mattin"}, {"sku": "chaussure"}]},
        ])
        self.assertEqual([i["sku"] for i in out[1]["items"]], ["<PERSON>", "<PERSON>"])

    def test_siblings_of_the_masked_path_survive(self):
        # A key order change would fail the E2E's row-count and structure checks for the
        # wrong reason, so the untouched part of the document has to come back unchanged.
        out = self.complete([
            {"id": "1", "customer": {"firstname": "<PERSON>", "note": "keep me"}},
            {"id": "2", "customer": {"firstname": "bruno", "note": "keep me too"}},
        ])
        self.assertEqual([r["id"] for r in out], ["1", "2"])
        self.assertEqual([r["customer"]["note"] for r in out], ["keep me", "keep me too"])
        self.assertEqual(list(out[0]), ["id", "customer"])
        self.assertEqual(list(out[0]["customer"]), ["firstname", "note"])

    def test_non_string_and_null_leaves_are_left_alone(self):
        # Replacing a number or a null with a token would corrupt the document's types.
        out = self.complete([{"n": 1, "maybe": None, "p": {"name": "<PERSON>"}},
                             {"n": 2, "maybe": None, "p": {"name": "bruno"}}])
        self.assertEqual([r["n"] for r in out], [1, 2])
        self.assertEqual([r["maybe"] for r in out], [None, None])
        self.assertEqual(out[1]["p"]["name"], "<PERSON>")

    def test_untouched_nested_document_is_returned_verbatim(self):
        # Claude Code resends every turn, so a rewrite of an unchanged block churns the
        # request and defeats the cache. Byte-identical or not rewritten at all.
        text = json.dumps([{"p": {"name": "bruno"}}, {"p": {"name": "martin"}}],
                          separators=(",", ":"))
        self.assertIs(self.guard.complete(text), text)

    def test_surrounding_text_survives_a_nested_rewrite(self):
        text = ("Export complete, 2 rows.\n"
                + json.dumps([{"p": {"name": "<PERSON>"}}, {"p": {"name": "bruno"}}],
                             separators=(",", ":"))
                + "\n<system-reminder>MCP instructions.</system-reminder>")
        out = self.guard.complete(text)
        self.assertIn("Export complete, 2 rows.", out)
        self.assertIn("<system-reminder>", out)
        self.assertNotIn("bruno", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
