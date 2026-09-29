"""Tests for the mock Presidio analyzer.

The mock stands in for presidio-analyzer (GLiNER2 on RunPod) when the GPU worker is
down, so the guardrail pipeline can be exercised offline. It is a dictionary-lookup
stub, not NER: these tests pin the matching contract CodeGuard depends on (spans,
offsets, the `entities` filter, no false positives on sub-words).
"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guardrail.mock_presidio import MockPresidio, build_vocabulary, load_vocabulary

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT_CSV = os.path.join(BASE, ".pii-score-out", "export.csv")


def _spans(results):
    return [(r["entity_type"], r["start"], r["end"]) for r in results]


class VocabularyTest(unittest.TestCase):
    def setUp(self):
        self.vocab = load_vocabulary(EXPORT_CSV)

    def test_reads_person_columns(self):
        self.assertIn("Alexandre", self.vocab["PERSON"])
        self.assertIn("martin", self.vocab["PERSON"])

    def test_reads_location_columns(self):
        self.assertIn("Saint-Estèphe", self.vocab["LOCATION"])
        self.assertIn("France", self.vocab["LOCATION"])

    def test_excludes_non_pii_columns(self):
        # zipcode / birth_date / status are not scored by the pipeline
        everything = set(self.vocab["PERSON"]) | set(self.vocab["LOCATION"])
        for value in ("33180", "2000-07-18", "active", "enterprise"):
            self.assertNotIn(value, everything)


class AnalyzeTest(unittest.TestCase):
    def setUp(self):
        self.vocab = load_vocabulary(EXPORT_CSV)
        self.analyzer = MockPresidio(self.vocab)

    def test_finds_known_first_name(self):
        results = self.analyzer.analyze("Contact Alexandre about the account")
        self.assertEqual(_spans(results), [("PERSON", 8, 17)])
        self.assertEqual(results[0]["score"], 0.99)

    def test_offsets_survive_accented_text(self):
        # Accented characters are multi-byte: a byte-offset stub would slice mid-character
        # and CodeGuard would replace garbage.
        text = "The city is Saint-Estèphe in France"
        vocab = {v.lower() for values in self.vocab.values() for v in values}
        found = set()
        for r in self.analyzer.analyze(text):
            span = text[r["start"]:r["end"]]
            self.assertIn(span.lower(), vocab, f"span {span!r} is not a vocabulary entry")
            found.add((r["entity_type"], span))
        self.assertIn(("LOCATION", "Saint-Estèphe"), found)
        self.assertIn(("LOCATION", "France"), found)

    def test_case_insensitive(self):
        # FailingMasker leaks lowercase surnames; a case-sensitive stub would miss them all
        results = self.analyzer.analyze("lastname: martin")
        self.assertEqual(_spans(results), [("PERSON", 10, 16)])

    def test_unknown_word_not_reported(self):
        self.assertEqual(self.analyzer.analyze("Nothing sensitive here"), [])

    def test_substring_is_not_a_match(self):
        # "martin" must not fire inside "martingale" / "martins"
        for text in ("martingale", "martins", "amartinez"):
            self.assertEqual(_spans(self.analyzer.analyze(text)), [], text)

    def test_entities_filter(self):
        text = "Alexandre moved to Saint-Estèphe"
        only_person = self.analyzer.analyze(text, entities=["PERSON"])
        self.assertEqual({r["entity_type"] for r in only_person}, {"PERSON"})
        only_location = self.analyzer.analyze(text, entities=["LOCATION"])
        self.assertEqual({r["entity_type"] for r in only_location}, {"LOCATION"})

    def test_masking_tokens_are_not_matched(self):
        # Already-masked cells must stay untouched, or LeakGuard sees no partial column
        text = '{"firstname":"<PERSON>","city":"<LOCATION>","zipcode":"33180"}'
        self.assertEqual(_spans(self.analyzer.analyze(text)), [])

    def test_longest_match_wins(self):
        # A vocabulary entry that is a prefix of another must not shadow it
        vocab = {"PERSON": ["Li", "Lina"], "LOCATION": []}
        results = MockPresidio(vocab).analyze("Lina met Li")
        self.assertEqual(_spans(results), [("PERSON", 0, 4), ("PERSON", 9, 11)])

    def test_empty_text(self):
        self.assertEqual(self.analyzer.analyze(""), [])

    def test_async_form_drops_into_masker(self):
        # Masker awaits analyze(); the mock must be usable without a shim.
        import asyncio
        from guardrail.code_guard import Masker, REGEXES, DATA_EXTENSIONS
        masker = Masker(self.analyzer.analyze_async, REGEXES,
                        {"PERSON": 0.85, "LOCATION": 0.96}, [], [], DATA_EXTENSIONS)
        out = asyncio.run(masker.mask_texts(["Contact Alexandre about France"]))
        self.assertEqual(out, ["Contact <PERSON> about <LOCATION>"])


class HttpTest(unittest.TestCase):
    """The wire format CodeGuard._analyze consumes."""

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.vocab = load_vocabulary(EXPORT_CSV)
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), MockPresidio.handler(load_vocabulary(EXPORT_CSV)))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def test_analyze_returns_presidio_shape(self):
        status, body = self._post("/analyze", {"text": "Alexandre lives in France",
                                                "language": "en",
                                                "entities": ["PERSON", "LOCATION"]})
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        for r in body:
            self.assertEqual(set(r), {"entity_type", "start", "end", "score"})

    def test_analyze_uses_requested_entities(self):
        _, body = self._post("/analyze", {"text": "Alexandre lives in France",
                                         "language": "en",
                                         "entities": ["PERSON"]})
        self.assertEqual([r["entity_type"] for r in body], ["PERSON"])

    def test_health(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=5) as resp:
            self.assertEqual(resp.status, 200)

    def test_missing_text_is_an_error(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/analyze", {"language": "en"})
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/nope", {"text": "x"})
        self.assertEqual(ctx.exception.code, 404)


class BuildVocabularyTest(unittest.TestCase):
    def test_deduplicates_and_sorts(self):
        rows = [{"firstname": "Bo", "lastname": "Bo", "city": "Nice", "country": "France"},
                {"firstname": "Bo", "lastname": "Al", "city": "Nice", "country": "Italy"}]
        vocab = build_vocabulary(rows)
        self.assertEqual(vocab["PERSON"], ["Al", "Bo"])
        self.assertEqual(vocab["LOCATION"], ["France", "Italy", "Nice"])


class CommittedVocabTest(unittest.TestCase):
    """The container runs off the committed JSON, not the CSV. Regenerate with:

        python3 -m guardrail.mock_presidio --vocab .pii-score-out/export.csv \\
            --emit-vocab guardrail/mock_presidio_vocab.json
    """

    PATH = os.path.join(BASE, "guardrail", "mock_presidio_vocab.json")

    def test_committed_json_matches_the_csv(self):
        from guardrail.mock_presidio import load_vocabulary_json
        self.assertEqual(load_vocabulary_json(self.PATH), load_vocabulary(EXPORT_CSV),
                         "mock_presidio_vocab.json is stale; regenerate it (see docstring)")

    def test_vocabulary_is_not_empty(self):
        from guardrail.mock_presidio import load_vocabulary_json
        vocab = load_vocabulary_json(self.PATH)
        self.assertGreater(len(vocab["PERSON"]), 0)
        self.assertGreater(len(vocab["LOCATION"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
