"""Offline tests for code_guard (no LiteLLM, no GPU): python3 guardrail/test_code_guard.py"""
import asyncio
import copy
import functools
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from code_guard import DATA_EXTENSIONS, REGEXES, Blocked, Masker, is_code_file

CORPUS_DIR = Path(__file__).resolve().parents[1] / "bench" / "corpus"

HAS_PYGMENTS = importlib.util.find_spec("pygments") is not None  # shipped with LiteLLM's proxy image

# What the fake analyzer "detects": substring -> (entity, score).
NER = {
    "Jean Dupont": ("PERSON", 0.99),
    "Lucía Fernández": ("PERSON", 0.99),
    "brice.lalu": ("PERSON", 0.97),
    "Lyon": ("LOCATION", 0.99),
    "Paris": ("LOCATION", 0.99),
    "Victoria": ("PERSON", 0.80),  # below the PERSON cutoff
    # GLiNER2 tags role nouns as PERSON (measured: "customer 2" 0.84-0.85, "The customer" 0.96,
    # "reviewer 2" 0.98).
    "customer 2": ("PERSON", 0.90),
    "Customer #3": ("PERSON", 0.90),
    "reviewer 2": ("PERSON", 0.98),
    "customer": ("PERSON", 0.96),
    "martin": ("PERSON", 0.95),  # a lowercase surname, as in the exports
    "4111 1111 1111 1111": ("CREDIT_CARD", 1.0),
    "3341 2328 1206 7974": ("CREDIT_CARD", 1.0),  # Luhn-valid digit run inside an IBAN
}
THRESHOLDS = {"PERSON": 0.85, "LOCATION": 0.96, "CREDIT_CARD": 0.6}


def make_masker(block=()):
    calls = []

    async def analyze(text):
        calls.append(text)
        out = []
        for needle, (entity, score) in NER.items():
            start = text.find(needle)
            while start != -1:
                out.append({"entity_type": entity, "start": start, "end": start + len(needle), "score": score})
                start = text.find(needle, start + 1)
        return out

    masker = Masker(analyze, REGEXES, THRESHOLDS, block=block,  # production blocks nothing
                    skip_tools=["Write", "Edit", "MultiEdit", "NotebookEdit"], data_extensions=DATA_EXTENSIONS)
    return masker, calls


def mask_text(text):
    masker, _ = make_masker()
    return asyncio.run(masker.mask_texts([text]))[0]


def claude_code_request():
    return {
        "system": [{"type": "text", "text": "You run for Jean Dupont in /Users/brice.lalu/app"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Fix the invoice for Jean Dupont in Lyon"}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Reading the file for Jean Dupont."},
                {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/exports/customers.csv"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "r1", "content": "1\tAUTHOR = 'Jean Dupont <jean@acme.fr>'"},
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "e1", "name": "Edit",
                 "input": {"old_string": "Jean Dupont", "new_string": "Jean Dupont (Lyon)"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "e1", "content": "1\tAUTHOR = 'Jean Dupont (Lyon)'"},
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "git log -1"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "b1",
                 "content": [{"type": "text", "text": "Author: Jean Dupont 06 12 34 56 78"}]},
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "r2", "name": "Read", "input": {"file_path": "/src/crm/customers.py"}},
                {"type": "tool_use", "id": "m1", "name": "mcp__crm__list_customers", "input": {"limit": 1}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "r2", "content": "1\tAUTHOR = 'Jean Dupont <jean@acme.fr>'"},
                {"type": "tool_result", "tool_use_id": "m1", "content": [{"type": "text", "text": json.dumps(
                    {"content": "| name | phone |\n|---|---|\n| Jean Dupont | 06 12 34 56 78 |"})}]},
            ]},
            # pii-guardian-qdu.5: a .json export the request is also editing (e2) reads as code;
            # a same-extension export it never edits (r4) stays masked, whatever pygments thinks.
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "r3", "name": "Read", "input": {"file_path": "/data/customers.json"}},
                {"type": "tool_use", "id": "r4", "name": "Read",
                 "input": {"file_path": "/data/other_customers.json"}},
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "e2", "name": "Edit",
                 "input": {"file_path": "/data/customers.json", "old_string": "a", "new_string": "b"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "r3", "content": "name\nJean Dupont"},
                {"type": "tool_result", "tool_use_id": "r4", "content": "name\nJean Dupont"},
            ]},
        ],
    }


class MaskTextTest(unittest.TestCase):
    def test_masks_ner_and_regex_entities(self):
        self.assertEqual(mask_text("Call Jean Dupont in Lyon at jean@acme.fr or 06 12 34 56 78"),
                         "Call <PERSON> in <LOCATION> at [EMAIL_REDACTED] or [PHONE_FR_REDACTED]")

    def test_role_nouns_are_not_people(self):
        text = 'customer 2 should be on "pro"; Customer #3 too; ask reviewer 2 and the customer.'
        self.assertEqual(mask_text(text), text)

    def test_lowercase_names_are_still_people(self):
        self.assertEqual(mask_text("contact: martin, Jean Dupont"), "contact: <PERSON>, <PERSON>")

    def test_below_threshold_is_kept(self):
        self.assertEqual(mask_text("class Victoria:"), "class Victoria:")

    def test_code_without_pii_is_byte_identical(self):
        code = "func (s *Server) Handle(w http.ResponseWriter) error {\n\treturn nil\n}\n"
        self.assertEqual(mask_text(code), code)

    def test_paths_and_urls_are_never_masked(self):
        for text in ("/Users/brice.lalu/src/main.py",
                     "cd ~/brice.lalu/Lyon && ls",
                     "see https://github.com/brice.lalu/Paris?x=jean@acme.fr",
                     r"C:\Users\brice.lalu\app",
                     "--config=Paris/app.yml",  # a file named after a place stays readable
                     "path=/etc/nginx/nginx.conf"):
            self.assertEqual(mask_text(text), text)

    def test_a_name_or_place_in_a_slash_token_is_masked(self):
        self.assertEqual(mask_text("Jean Dupont/Lyon"), "<PERSON>/<LOCATION>")
        self.assertEqual(mask_text("Lyon/Paris"), "<LOCATION>/<LOCATION>")
        self.assertEqual(mask_text("the Lyon/Paris office"), "the <LOCATION>/<LOCATION> office")

    def test_a_tz_zone_loses_its_city(self):
        # "Europe/Paris" has exactly the shape of the leak above: nothing but the intent tells
        # them apart, and the city is what ask 55 is about.
        self.assertEqual(mask_text('TZ="Europe/Paris"'), 'TZ="Europe/<LOCATION>"')

    def test_path_next_to_a_name_masks_only_the_name(self):
        self.assertEqual(mask_text("Jean Dupont edited /Users/brice.lalu/x.py"),
                         "<PERSON> edited /Users/brice.lalu/x.py")

    def test_credit_card_is_masked_not_blocked(self):
        # A blocked card stays in the history Claude Code resends every turn: it would lock the session.
        self.assertEqual(mask_text("card 4111 1111 1111 1111"), "card <CREDIT_CARD>")

    def test_blocking_is_still_available_when_configured(self):
        masker, _ = make_masker(block=["CREDIT_CARD"])
        with self.assertRaises(Blocked):
            asyncio.run(masker.mask_texts(["card 4111 1111 1111 1111"]))

    def test_card_inside_an_iban_is_masked_not_blocked(self):
        self.assertEqual(mask_text("iban FR76 3341 2328 1206 7974 0344 702"), "iban [IBAN_REDACTED]")

    def test_escaped_csv_in_json_is_masked_and_stays_valid(self):
        # MCP servers return tables inside JSON strings: PII right after an escaped "\n".
        text = json.dumps({"content": "phone,email\n06 12 34 56 78,jean@acme.fr\njean@acme.fr,x"})
        self.assertEqual(json.loads(mask_text(text))["content"],
                         "phone,email\n[PHONE_FR_REDACTED],[EMAIL_REDACTED]\n[EMAIL_REDACTED],x")

    def test_unicode_escaped_name_in_json_is_masked(self):
        text = json.dumps({"client": "Lucía Fernández"})  # ascii-escaped: Luc\u00eda Fern\u00e1ndez
        self.assertEqual(json.loads(mask_text(text)), {"client": "<PERSON>"})

    def test_json_inside_a_json_string_is_masked(self):
        text = json.dumps({"content": json.dumps([{"name": "Jean Dupont", "phone": "0612345678"}])})
        self.assertEqual(json.loads(json.loads(mask_text(text))["content"]),
                         [{"name": "<PERSON>", "phone": "[PHONE_FR_REDACTED]"}])

    def test_plain_json_keeps_its_exact_formatting(self):
        self.assertEqual(mask_text('[{"id":1,"name":"Jean Dupont","city":"Lyon"}]'),
                         '[{"id":1,"name":"<PERSON>","city":"<LOCATION>"}]')

    def test_json_with_escaped_strings_but_no_pii_stays_byte_identical(self):
        # Issue: JSON with escaped newlines/tabs but no PII was re-serialized, normalizing numbers
        # Example 1: JSON with escaped newline in nested string, non-canonical number format
        json_str = '{"data":"line\\nbreak","value":1.10}'
        result = mask_text(json_str)
        self.assertEqual(result, json_str)

        # Example 2: JSON with escaped newline in content (like MCP table results), no PII
        json_str = json.dumps({"content": "name | value\n---|---\ntest | 1e5"})
        result = mask_text(json_str)
        self.assertEqual(result, json_str)

        # Example 3: Minified JSON (no newlines in the JSON structure itself, but escaped \n in strings)
        json_str = '{"data":"a\\nb","other":"c\\td"}'
        result = mask_text(json_str)
        self.assertEqual(result, json_str)

    @unittest.skipUnless(HAS_PYGMENTS, "pygments not installed (it is in the LiteLLM image)")
    def test_code_files_are_told_apart_from_data_files(self):
        for path in ("/a/b.py", "Dockerfile", "infra/main.tf", r"C:\src\App.java", "web/app.tsx", "k8s/deploy.yaml"):
            self.assertTrue(is_code_file(path, DATA_EXTENSIONS), path)
        for path in ("exports/customers.csv", "export.json", "notes.md", "out.txt", "customers", "seed.sql"):
            self.assertFalse(is_code_file(path, DATA_EXTENSIONS), path)

    def test_overlapping_detections_are_merged(self):
        # "Paris" (NER) sits inside the email (regex): the longer span wins, text stays readable.
        self.assertEqual(mask_text("mail Paris.Lyon@acme.fr now"), "mail [EMAIL_REDACTED] now")


class RegexTest(unittest.TestCase):
    # Real false positives from the regex sweep over 14.6k source files (guardrail/regex_sweep.py).
    CODE = [
        "hMAC_SHA1 = univ.ObjectIdentifier('1.3.6.1.5.5.8.1.2')",
        'host = "0.0.0.0" if config.host is None else "127.0.0.1"',
        "[b' ( +3.142e+00+ +2.718e+00j)']",
        "array([0.   +1.j, 0.  +10.j, 0. +100.j, 0.+1000.j])",
        "x = 65536 +256 +500",
        'if line[-1] in b"0123456789t":',
        '"input_cost_per_pixel": 2.0751953125e-09,',
        "i-0645704820a8e83ff is warming up",
        "np.datetime64('1970-01-01T00:00:02.12345678')",
        "client.prepare_request_body(code_verifier='KB46DCKJ873NCGXK5GD682NHDKK34GR')",
        '"CertificateId": "ID123456789012345EXAMPLE",',
        '"user_email": "user@example.com", "members": ["user:eve@example.org"], "a": "b@test.test"',
        "use std::collections::HashMap; a::b::c",
        "np.array(['+1980-02-29T01:02:03', '+32971-04-28 00:00:00'])",
        # Infrastructure addresses in kubectl/docker/log output: not personal data, needed to debug.
        "traefik-59f7   1/1   Running   10.0.0.55   k3d-server-0",
        "10.43.182.139  172.18.0.3  192.168.1.4  169.254.169.254  100.64.0.1",
        "dig @1.1.1.1 / 8.8.8.8, link fe80::1ff:fe23:4567:890a, ula fd00:ec2::254",
    ]
    PII = {
        "IP 203.0.113.7 logged in.": "IP [IPV4_REDACTED] logged in.",
        "from 2001:db8:1a::42": "from [IPV6_REDACTED]",
        "tel +33 6 12 34 56 78": "tel [PHONE_INTERNATIONAL_REDACTED]",
        "tel +1 415 555 0182": "tel [PHONE_INTERNATIONAL_REDACTED]",
        "tel:+14155550123": "tel:[PHONE_INTERNATIONAL_REDACTED]",
        "06 12 34 56 78 / 06.12.34.56.78 / 0612345678":
            "[PHONE_FR_REDACTED] / [PHONE_FR_REDACTED] / [PHONE_FR_REDACTED]",
        "IBAN FR76 3000 6000 0112 3456 7890 189": "IBAN [IBAN_REDACTED]",
        "iban=DE89370400440532013000;": "iban=[IBAN_REDACTED];",
        "mail jean@acme.fr": "mail [EMAIL_REDACTED]",
    }

    def test_code_is_untouched(self):
        for code in self.CODE:
            self.assertEqual(mask_text(code), code)

    def test_pii_is_masked(self):
        for text, masked in self.PII.items():
            self.assertEqual(mask_text(text), masked)


class MaskRequestTest(unittest.TestCase):
    def setUp(self):
        self.masker, self.calls = make_masker()
        self.original = claude_code_request()
        self.data = copy.deepcopy(self.original)
        asyncio.run(self.masker.mask_request(self.data))
        self.msgs = self.data["messages"]

    def test_user_text_is_masked(self):
        self.assertEqual(self.msgs[0]["content"][0]["text"], "Fix the invoice for <PERSON> in <LOCATION>")

    def test_read_result_is_masked(self):
        self.assertEqual(self.msgs[2]["content"][0]["content"], "1\tAUTHOR = '<PERSON> <[EMAIL_REDACTED]>'")

    def test_tool_result_text_blocks_are_masked(self):
        self.assertEqual(self.msgs[6]["content"][0]["content"][0]["text"],
                         "Author: <PERSON> [PHONE_FR_REDACTED]")

    @unittest.skipUnless(HAS_PYGMENTS, "pygments not installed (it is in the LiteLLM image)")
    def test_read_result_of_a_code_file_is_untouched(self):
        self.assertEqual(self.msgs[8]["content"][0], self.original["messages"][8]["content"][0])

    def test_mcp_result_is_masked(self):
        masked = json.loads(self.msgs[8]["content"][1]["content"][0]["text"])
        self.assertEqual(masked["content"], "| name | phone |\n|---|---|\n| <PERSON> | [PHONE_FR_REDACTED] |")

    def test_edit_result_is_untouched(self):
        self.assertEqual(self.msgs[4], self.original["messages"][4])

    def test_system_assistant_and_tool_use_are_untouched(self):
        self.assertEqual(self.data["system"], self.original["system"])
        for i in (1, 3, 5, 7, 9, 10):
            self.assertEqual(self.msgs[i], self.original["messages"][i])

    def test_read_of_a_path_the_request_is_also_editing_is_untouched(self):
        # /data/customers.json is targeted by the Edit at id "e2" elsewhere in this same request.
        self.assertEqual(self.msgs[11]["content"][0], self.original["messages"][11]["content"][0])

    def test_read_of_a_same_extension_path_the_request_does_not_edit_is_still_masked(self):
        # /data/other_customers.json is never edited: the leak guard, matching is by exact
        # file_path string, not by extension or basename.
        self.assertEqual(self.msgs[11]["content"][1]["content"], "name\n<PERSON>")

    def test_history_is_analyzed_once_across_turns(self):
        before = len(self.calls)
        asyncio.run(self.masker.mask_request(copy.deepcopy(self.original)))
        self.assertEqual(len(self.calls), before)


@functools.lru_cache(maxsize=1)
def corpus_files():
    """The 24 bench corpus files (6 templates x 4 natural languages) as {name: text}.

    bench/corpus/generate.py owns the rendering, so the corpus and the benchmark cannot drift.
    Called in-process: no compiler, no gateway, no cluster.
    """
    spec = importlib.util.spec_from_file_location("bench_corpus_generate", CORPUS_DIR / "generate.py")
    generate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generate)
    files = {}
    for fixture_path in sorted(generate.FIXTURES.glob("*.json")):
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        counters = {}
        for template in sorted(p for p in generate.TEMPLATES.iterdir() if p.is_file()):
            text, _spans = generate.render(template.read_text(encoding="utf-8"), fixture, counters)
            files[f"{fixture_path.stem}/{template.name}"] = text
    return files


class CodeCorpusRequestTest(unittest.TestCase):
    """Ask 50 ("never mask any code symbols that could break Claude Code"), measured through
    mask_request, the path a /v1/messages request takes.

    docs/ner-model-benchmark.md reports 0 code tokens masked over these 24 files, but it measures
    them as raw text through /guardrails/apply_guardrail, which never reaches the scope decision.
    Here every file is echoed back inside a tool_result, once per tool that returns one, which is
    how a developer actually meets it.
    """

    def setUp(self):
        self.masker, self.calls = make_masker()

    def echoed_by(self, tool, name, text):
        """Mask one real request whose tool_result echoes the file, and return what came back."""
        inputs = {
            "Read": {"file_path": f"/src/{name}"},
            "Grep": {"pattern": "Maintainer", "path": "/src", "output_mode": "content"},
            "Bash": {"command": f"cat /src/{name}"},
        }
        data = {"messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": tool, "input": inputs[tool]}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": text}]},
        ]}
        asyncio.run(self.masker.mask_request(data))
        return data["messages"][1]["content"][0]["content"]

    def changed(self, tool):
        """Which files the tool echoed back altered. Every file is exercised, whatever the verdict."""
        return [name for name, text in corpus_files().items()
                if self.echoed_by(tool, name, text) != text]

    def test_the_corpus_is_the_documented_24_files(self):
        self.assertEqual(len(corpus_files()), 24)

    @unittest.skipUnless(HAS_PYGMENTS, "is_code_file needs pygments (it is in the LiteLLM image)")
    def test_read_results_are_byte_identical(self):
        self.assertEqual(self.changed("Read"), [])

    @unittest.skipUnless(HAS_PYGMENTS, "is_code_file needs pygments (it is in the LiteLLM image)")
    @unittest.expectedFailure
    def test_grep_results_are_byte_identical(self):
        # Known failure: only Read is exempted from masking, so a Grep hit inside a code file is
        # masked and Claude quotes the placeholder back in its Edit. Fixed by pii-guardian-qdu.4.
        self.assertEqual(self.changed("Grep"), [])

    @unittest.skipUnless(HAS_PYGMENTS, "is_code_file needs pygments (it is in the LiteLLM image)")
    @unittest.expectedFailure
    def test_bash_results_are_byte_identical(self):
        # Known failure: a Bash result carries no file path, so it cannot be told from a data dump.
        # The decision on it is pii-guardian-qdu.4's to record.
        self.assertEqual(self.changed("Bash"), [])


class EditedPathMatchingTest(unittest.TestCase):
    """The path-match rule (pii-guardian-qdu.5) is a leak surface: assert its edges directly."""

    @staticmethod
    def run_request(edit_name, edit_input, read_path):
        masker, _ = make_masker()
        data = {"messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "e1", "name": edit_name, "input": edit_input},
                {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": read_path}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "r1", "content": "name\nJean Dupont"},
            ]},
        ]}
        asyncio.run(masker.mask_request(data))
        return data["messages"][1]["content"][0]["content"]

    def test_notebook_edit_file_path_unmasks_the_matching_read(self):
        self.assertEqual(self.run_request("NotebookEdit", {"file_path": "/nb/analysis.ipynb"},
                                          "/nb/analysis.ipynb"),
                         "name\nJean Dupont")

    def test_basename_alone_does_not_match(self):
        # Same filename, different directory: exact file_path string only, never basename.
        self.assertEqual(self.run_request("Edit", {"file_path": "/data/customers.json"},
                                          "/other/dir/customers.json"),
                         "name\n<PERSON>")


class CacheTest(unittest.TestCase):
    """The NER cache is an optimisation: a lost entry must cost a re-analysis, never a detection."""

    def test_reset_of_a_full_cache_still_masks_texts_cached_earlier(self):
        masker, _ = make_masker()
        with patch("code_guard.CACHE_MAX", 2):
            self.assertEqual(asyncio.run(masker.mask_texts(["Hi Jean Dupont"])), ["Hi <PERSON>"])
            # Two new texts fill the cache and reset it; the first text is masked again anyway.
            out = asyncio.run(masker.mask_texts(["from Paris", "Lucía Fernández", "Hi Jean Dupont"]))
        self.assertEqual(out, ["from <LOCATION>", "<PERSON>", "Hi <PERSON>"])

    def test_concurrent_calls_do_not_unmask_each_other(self):
        masker, _ = make_masker()
        analyze = masker.analyze
        asyncio.run(masker.mask_texts(["Hi Jean Dupont"]))  # cached, and about to be evicted

        async def race():
            victim_may_return = asyncio.Event()

            async def analyze_waiting(text):
                if text == "in Paris":  # the victim: the other request resets the cache while it waits
                    await victim_may_return.wait()
                return await analyze(text)

            async def resetter():
                try:
                    return await masker.mask_texts(["from Lyon", "to Victoria"])
                finally:
                    victim_may_return.set()

            masker.analyze = analyze_waiting
            return await asyncio.gather(masker.mask_texts(["Hi Jean Dupont", "in Paris"]), resetter())

        with patch("code_guard.CACHE_MAX", 2):
            out, _ = asyncio.run(race())
        self.assertEqual(out, ["Hi <PERSON>", "in <LOCATION>"])


if __name__ == "__main__":
    unittest.main()
