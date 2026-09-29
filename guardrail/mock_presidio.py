"""Mock Presidio analyzer: a dictionary-lookup stand-in for presidio-analyzer.

The real analyzer is GLiNER2 on a RunPod GPU worker. When that worker is down (or
costing money we don't want to spend on a test), CodeGuard's `_analyze` blocks for
its full 120 s timeout and then fails closed, which hangs the whole gateway.

This module answers `/analyze` with Presidio-shaped spans derived from a fixed
vocabulary of known PERSON and LOCATION strings, matched case-insensitively on
word boundaries.

What it buys: the guardrail pipeline — CodeGuard's span/offset handling, thresholds,
role-word filter, regexes, and LeakGuard's column completion — runs end to end with
no GPU and no network, deterministically.

What it does NOT buy: any claim about real NER quality. A dictionary has no recall on
text it wasn't built from. Precision on the fixture is 100% by construction; that
number means nothing. Use it to test plumbing, never to benchmark detection.

    python3 -m guardrail.mock_presidio --vocab guardrail/mock_presidio_vocab.json
"""
import argparse
import csv
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Column -> entity, matching guardrail/failing_masker.py's classification.
PERSON_COLUMNS = ("firstname", "lastname")
LOCATION_COLUMNS = ("city", "country")

# High: the vocabulary is exact, so a hit is a certain detection. CodeGuard's
# per-entity thresholds (PERSON 0.85, LOCATION 0.96) are what this exercises.
SCORE = 0.99


def build_vocabulary(rows):
    """{"PERSON": [...], "LOCATION": [...]} from rows of the customer export.

    Sorted and deduplicated so the compiled matcher is deterministic.
    """
    vocab = {"PERSON": set(), "LOCATION": set()}
    for row in rows:
        for entity, columns in (("PERSON", PERSON_COLUMNS), ("LOCATION", LOCATION_COLUMNS)):
            for column in columns:
                value = (row.get(column) or "").strip()
                if value:
                    vocab[entity].add(value)
    return {entity: sorted(values) for entity, values in vocab.items()}


def load_vocabulary(csv_path):
    with open(csv_path, newline="", encoding="utf-8") as handle:
        return build_vocabulary(csv.DictReader(handle))


def load_vocabulary_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class MockPresidio:
    """Case-insensitive, word-boundary vocabulary matcher returning Presidio spans."""

    def __init__(self, vocabulary):
        self.vocabulary = {entity: list(values) for entity, values in vocabulary.items()}
        self._matchers = {
            entity: self._compile(values) for entity, values in self.vocabulary.items()
        }

    def _compile(self, values):
        if not values:
            return None
        # Longest alternative first: Python's alternation is leftmost-first, so "Lina" must be
        # tried before "Li" or the shorter entry wins inside a longer word.
        alternatives = "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))
        # (?<![\w]) / (?![\w]) keep "martin" from firing inside "martingale" or "amartinez".
        # re.UNICODE makes \w match accented letters, so "Véronique" has real boundaries.
        return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE | re.UNICODE)

    def analyze(self, text, entities=None):
        """Presidio's response shape: [{"entity_type", "start", "end", "score"}, ...].

        `entities` filters like the real analyzer does; an unknown type yields nothing
        rather than an error (CodeGuard requests CREDIT_CARD, which this stub cannot detect).
        """
        if not text:
            return []
        wanted = set(entities) if entities else set(self._matchers)
        found = []
        for entity, matcher in self._matchers.items():
            if entity not in wanted or matcher is None:
                continue
            for m in matcher.finditer(text):
                found.append((m.start(), m.end(), entity))
        # Longest match wins an overlap; spans are non-overlapping and ordered, as Presidio's are.
        found.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        spans, cursor = [], 0
        for start, end, entity in found:
            if start < cursor:
                continue
            spans.append({"entity_type": entity, "start": start, "end": end, "score": SCORE})
            cursor = end
        return spans

    async def analyze_async(self, text, entities=None):
        """Awaitable form, so the mock can be passed straight in as `Masker(analyze=...)`.

        Mirrors CodeGuard._analyze's signature. Kept async (rather than dropping the
        await at the call site) so the offline harness and the live HTTP path exercise
        the same code.
        """
        return self.analyze(text, entities)

    @classmethod
    def handler(cls, vocabulary):
        """A BaseHTTPRequestHandler serving `/analyze` and `/health` — what CodeGuard calls."""
        analyzer = cls(vocabulary)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self, status, body):
                payload = body if isinstance(body, bytes) else body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path.rstrip("/") in ("/health", ""):
                    self._respond(200, "ok\n")
                else:
                    self._respond(404, "not found\n")

            def do_POST(self):
                if self.path.rstrip("/") != "/analyze":
                    self._respond(404, "not found\n")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    request = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, TypeError):
                    self._respond(400, "invalid json\n")
                    return
                text = request.get("text")
                if not isinstance(text, str):
                    self._respond(400, "missing 'text'\n")
                    return
                results = analyzer.analyze(text, request.get("entities"))
                self._respond(200, json.dumps(results, ensure_ascii=False))

            def log_message(self, *args):  # keep the pod log readable
                pass

        return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vocab", required=True,
                        help="JSON vocabulary file, or CSV to derive one from")
    parser.add_argument("--emit-vocab", metavar="PATH",
                        help="write the vocabulary to PATH as JSON and exit")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()
    vocabulary = (load_vocabulary_json(args.vocab) if args.vocab.endswith(".json")
                  else load_vocabulary(args.vocab))
    if args.emit_vocab:
        with open(args.emit_vocab, "w", encoding="utf-8") as handle:
            json.dump(vocabulary, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"wrote {args.emit_vocab}: "
              f"{ {k: len(v) for k, v in vocabulary.items()} }", flush=True)
        return
    counts = {entity: len(values) for entity, values in vocabulary.items()}
    print(f"mock presidio: {counts} on :{args.port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), MockPresidio.handler(vocabulary)).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
