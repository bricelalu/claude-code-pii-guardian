"""LiteLLM guardrail for Claude Code traffic: masks PII in what the developer types and in tool
results, and nothing else.

Scope (Anthropic /v1/messages shape):
  masked    user text blocks, tool_result content (string or text blocks): MCP tools, Bash,
            Read of data files (csv, json, md...), Grep/Glob lines attributed to a data file
  untouched system prompt, assistant text, tool_use input, results of the `skip_tools` tools
            (Write/Edit: their result echoes the file Claude is editing), Read of code files and
            of notebooks (.ipynb) (Claude must quote them exactly to edit them), Read of a
            file_path a Write/Edit/MultiEdit/NotebookEdit targets anywhere in the same request
            (it's being edited right now, whatever its extension), Grep/Glob lines attributed to
            a code file, paths and URLs
JSON tool results (MCP): strings holding escaped documents (a table or CSV in a "content" field,
JSON inside JSON) are decoded and masked as documents of their own, then re-serialized.

Detection: Presidio analyzer (GLiNER2 on RunPod) for PERSON/LOCATION/CREDIT_CARD with per-entity
cutoffs, plus REGEXES below. NER results are cached per text: Claude Code resends
the whole conversation every turn, so only new blocks reach the GPU.

Loaded by LiteLLM as `guardrail: code_guard.CodeGuard` (this file sits next to /app/config.yaml).
"""
import asyncio
import functools
import ipaddress
import json
import os
import re

try:
    import httpx
    from fastapi import HTTPException
    from litellm.integrations.custom_guardrail import CustomGuardrail
    from litellm.types.guardrails import GuardrailEventHooks
except ImportError:  # offline tests only exercise Masker
    CustomGuardrail = object

# Paths and URLs are never masked: a username in /Users/<name>/ is PII-shaped but masking it
# breaks every tool call that reuses the path. A slash is not enough on its own: "Lyon/Paris" in
# prose has the shape of a path and is a leak, so a token is protected when it is a URL, a drive
# path, starts with /, ~, ./ or ../, or ends in a file extension. "Europe/Paris" (a tz zone) is
# the price of that rule: it is the same shape as the leak, and the city is masked. A file named
# after a place ("Paris/app.yml") stays readable, because breaking the path is the worse failure.
PROTECTED = re.compile(
    r"[A-Za-z][\w+.-]*://[^\s\"'`<>]+"
    r"|\b[A-Za-z]:[\\/][^\s\"'`<>]+"
    r"|(?<![\w.~-])[~.]*/[^\s\"'`,;:=(){}\[\]<>|]*"
    r"|[^\s\"'`,;:=(){}\[\]<>|]*/[^\s\"'`,;:=(){}\[\]<>|]*\.\w{1,8}\b"
)
CACHE_MAX = 10_000

# Deterministic PII (no model). A match becomes "[<NAME>_REDACTED]". Case-sensitive. Each rule is
# fenced against the false positives found by guardrail/regex_sweep.py on 14.6k real source files.
# Not used: a bare-"::" IPv6 rule (std::collections), any-5-digit postal codes.
REGEXES = [
    # RFC 2606 reserved domains (example.com, *.test, ...) are placeholders, not people.
    {"name": "email", "pattern": r"\b[A-Za-z0-9._%+-]+@(?![\w.-]*(?:\bexample\.(?:com|org|net)|\.(?:test|invalid|example|localhost))\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"},
    # Full form, or compressed with "::" between hex groups (2001:db8::1, fe80::1). No word
    # character may touch the match. Known edge: "ab::cd" is a valid IPv6 and gets masked.
    {"name": "ipv6", "pattern": r"(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|[0-9A-Fa-f]{2,4}:(?:[0-9A-Fa-f]{1,4}:){0,5}:(?:[0-9A-Fa-f]{1,4}:){0,5}[0-9A-Fa-f]{1,4})(?![\w:])"},
    # +33 6 12 34 56 78, +1 415 555 0182, +14155550123: at least 8 digits, no "." separator
    # (+3.142e+00, +100.j are numbers, +1980-02-29 is a signed ISO year, not phones).
    {"name": "phone_international", "pattern": r"(?<![\w+.])\+(?!\d{4,6}-\d\d-\d\d)(?=(?:[ ()-]*\d){8})[1-9]\d{0,2}(?:[ -]?\(?\d{1,4}\)?){2,5}(?![\d.])"},
    # 06 12 34 56 78, 06.12.34.56.78, 0612345678. Not a digit alphabet, not inside a decimal,
    # timestamp or hex id (2.0751953125e-09, 00:02.12345678, i-0645704820a8).
    {"name": "phone_fr", "pattern": r"(?<![\w+.:-])(?!0123456789)0[1-9](?:[ .-]?\d{2}){4}(?![\w-]|\.\d)"},
    # Not inside a dotted OID (1.3.6.1.5.5.8), not 0.0.0.0 / loopback / broadcast.
    {"name": "ipv4", "pattern": r"(?<![\w.])(?!0\.0\.0\.0|127\.|255\.)(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)(?![\w]|\.\d)"},
    # FR76 3000 6000 0112 3456 7890 189 or DE89370400440532013000, checksum verified (iban_ok).
    {"name": "iban", "pattern": r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"},
]


def iban_ok(value):
    """ISO 13616 mod-97 check: rejects random uppercase tokens (PKCE verifiers, AWS ids)."""
    s = value.replace(" ", "")
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


# Infrastructure addresses (kubectl, docker, logs) identify machines, not people, and Claude needs
# them to debug. Documentation ranges (192.0.2.0/24, 2001:db8::/32...) stay masked like public IPs.
INFRA_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8",
    "0.0.0.0/8", "fc00::/7", "fe80::/10", "::1/128")]
PUBLIC_RESOLVERS = {"1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9"}


def ip_is_personal(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:  # IP-shaped but not parseable: mask, to be safe
        return True
    return value not in PUBLIC_RESOLVERS and not any(ip in n for n in INFRA_NETS if n.version == ip.version)


VALIDATORS = {"IBAN": iban_ok, "IPV4": ip_is_personal, "IPV6": ip_is_personal}

# GLiNER2 tags role nouns as PERSON with high scores (measured: "customer 2" 0.84, "user 42" 0.89,
# "The customer" 0.96, "reviewer 2" 0.98). A developer writing "customer 2 should be on pro" then
# gets "<PERSON> should be on pro" and Claude can't tell which record is meant. A PERSON hit made
# only of role words and numbers is not a person. Real names, even lowercase ones, pass.
ROLE_WORDS = frozenset("""
    customer client user patient employee agent reviewer admin administrator owner author maintainer
    developer dev manager member person people someone somebody guest visitor buyer seller vendor
    supplier contact recipient sender caller operator staff applicant candidate student teacher
    doctor driver subscriber tenant lead prospect partner account holder beneficiary consumer
    requester assignee approver contributor worker contractor freelancer customers the a an our my
    your their this that each every new old first second third last same other
""".split())


def is_role_reference(span):
    words = re.findall(r"[^\W\d_]+", span.lower())
    return bool(words) and all(w in ROLE_WORDS or w.rstrip("s") in ROLE_WORDS for w in words)

# Read results of these files stay masked even though pygments knows the format: exports and dumps
# (customer tables live in CSV, JSON, Markdown, SQL seeds). Overridable in config (data_extensions).
DATA_EXTENSIONS = (".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".md", ".markdown", ".txt", ".log",
                   ".xml", ".sql")


@functools.lru_cache(maxsize=4096)
def _has_lexer(filename):
    try:  # pygments ships with LiteLLM's proxy image (litellm[proxy] -> rich -> pygments)
        from pygments.lexers import find_lexer_class_for_filename
    except ImportError:  # can't tell: treat as data, i.e. mask
        return False
    return find_lexer_class_for_filename(filename) is not None


def is_code_file(path, data_extensions):
    """Code or config a developer edits: pygments knows its language and it isn't a data format.
    A notebook (.ipynb) counts as code even though pygments has no lexer for it: NotebookEdit is
    in skip_tools, so the file is meant to be edited, and a masked NotebookRead fails that edit.
    Any other unknown file (no extension, .env...) counts as data."""
    name = re.split(r"[\\/]", path)[-1]
    if not name:
        return False
    ext = os.path.splitext(name)[1].lower()
    if ext in data_extensions:
        return False
    return ext == ".ipynb" or _has_lexer(name)


# Grep/Glob mix hits from many files in one result: "path:line:text" (content mode, line numbers
# on), "path:text" (content mode, line numbers off) or a bare path (files_with_matches, Glob).
_LINE_PATH = re.compile(r'^([^\n]+?):(?:\d+:)?')


def _line_path(line):
    """The file path a Grep/Glob output line is about, or None when its shape doesn't say (the
    line then stays masked, the fail-safe default)."""
    m = _LINE_PATH.match(line)
    if m:
        return m.group(1)
    return line if line and "\n" not in line else None


ESCAPED = re.compile(r'[\n\r\t"\\]')  # a JSON string holding one of these is serialized escaped


def json_document(text):
    stripped = text.strip()
    if stripped[:1] not in ("{", "["):
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    return data if isinstance(data, (dict, list)) else None


def json_strings(node):
    if isinstance(node, dict):
        node = list(node.values())
    if isinstance(node, list):
        for value in node:
            yield from json_strings(value)
    elif isinstance(node, str):
        yield node


def replace_strings(node, table):
    if isinstance(node, dict):
        return {k: replace_strings(v, table) for k, v in node.items()}
    if isinstance(node, list):
        return [replace_strings(v, table) for v in node]
    return table.get(node, node) if isinstance(node, str) else node


class Blocked(Exception):
    pass


class Masker:
    def __init__(self, analyze, regexes, thresholds, block, skip_tools, data_extensions=DATA_EXTENSIONS):
        self.analyze = analyze  # async (text) -> [{"entity_type", "start", "end", "score"}]
        self.regexes = [(r["name"].upper(), re.compile(r["pattern"])) for r in regexes]
        self.thresholds = thresholds
        self.block = set(block)
        self.skip_tools = set(skip_tools)
        self.data_extensions = tuple(e.lower() for e in data_extensions)
        self.cache = {}

    def _spans(self, text, ner):
        def outside(span, regions):
            return not any(span[0] < end and start < span[1] for start, end, *_ in regions)

        protected = [m.span() for m in PROTECTED.finditer(text) if m.end() > m.start()]
        regex = [(m.start(), m.end(), f"[{name}_REDACTED]")
                 for name, rx in self.regexes for m in rx.finditer(text)
                 if VALIDATORS.get(name, bool)(m.group())]
        # A NER hit overlapping a regex hit defers to it: a Luhn-valid digit run inside an IBAN
        # is not a credit card, a name inside an email is already masked with it.
        found = regex + [(r["start"], r["end"], r["entity_type"]) for r in ner
                         if r["score"] >= self.thresholds.get(r["entity_type"], 2)
                         and outside((r["start"], r["end"]), regex)
                         and not (r["entity_type"] == "PERSON" and is_role_reference(text[r["start"]:r["end"]]))]
        found = [s for s in found if outside(s, protected)]
        blocked = sorted({label for _, _, label in found if label in self.block})
        if blocked:
            raise Blocked(", ".join(blocked))
        merged = []
        for start, end, label in sorted(found):
            label = label if label.startswith("[") else f"<{label}>"
            if merged and start < merged[-1][1]:
                ps, pe, pl = merged[-1]
                merged[-1] = (ps, max(pe, end), pl if pe - ps >= end - start else label)
            else:
                merged.append((start, end, label))
        return merged

    async def _decode_json(self, text):
        """Mask escaped documents inside a JSON text (MCP: a table in a "content" field), and undo
        \\u escapes so NER sees "Lucía", not "Luc\\u00eda". Return original text if nothing was masked."""
        data = json_document(text)
        if data is None:
            return text
        nested = list(dict.fromkeys(s for s in json_strings(data) if ESCAPED.search(s)))
        if not nested and "\\u" not in text:
            return text
        masked = await self.mask_texts(nested)
        # If nothing changed and no unicode escapes, return original to preserve exact bytes (1.10 stays 1.10, not 1.1)
        if masked == nested and "\\u" not in text:
            return text
        data = replace_strings(data, dict(zip(nested, masked)))
        if "\n" in text.strip():
            return json.dumps(data, ensure_ascii=False, indent=2)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    async def mask_texts(self, texts):
        texts = list(await asyncio.gather(*(self._decode_json(t) for t in texts)))
        unique = [t for t in dict.fromkeys(texts) if t.strip()]
        # The cache is read before awaiting the analyzer, on purpose: it is shared with concurrent
        # requests, and losing an entry may only cost a re-analysis, never a detection.
        cached = {t: self.cache[t] for t in unique if t in self.cache}
        new = [t for t in unique if t not in cached]
        results = await asyncio.gather(*(self.analyze(t) for t in new))
        ner = {**cached, **dict(zip(new, results))}
        if len(self.cache) + len(new) > CACHE_MAX:  # ponytail: wholesale reset, LRU if hit rate drops
            self.cache.clear()
        self.cache.update(zip(new, results))
        out = []
        for text in texts:
            for start, end, label in reversed(self._spans(text, ner.get(text, []))):
                text = text[:start] + label + text[end:]
            out.append(text)
        return out

    def _targets(self, messages):
        """(container, key, tool_use) of every string to mask, in request order. tool_use is the
        tool_use block that produced a tool_result, None for user text."""
        tools = {b.get("id"): b
                 for m in messages if m.get("role") == "assistant" and isinstance(m.get("content"), list)
                 for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"}
        # A path a Write/Edit/MultiEdit/NotebookEdit targets anywhere in this request is being
        # edited right now: its Read is code whatever the extension says (pii-guardian-qdu.5).
        # Exact file_path string match only, never basename.
        edited_paths = {(b.get("input") or {}).get("file_path") for b in tools.values()
                        if b.get("name") in self.skip_tools}
        edited_paths = {p for p in edited_paths if isinstance(p, str)}
        targets = []
        for m in messages:
            content = m.get("content")
            if m.get("role") != "user":
                continue
            if isinstance(content, str):
                targets.append((m, "content", None))
                continue
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    targets.append((b, "text", None))
                elif b.get("type") == "tool_result":
                    tool_use = tools.get(b.get("tool_use_id"), {})
                    if self._unmasked_tool(tool_use, edited_paths):
                        continue
                    inner = b.get("content")
                    if isinstance(inner, str):
                        targets.append((b, "content", tool_use))
                    elif isinstance(inner, list):
                        targets += [(x, "text", tool_use) for x in inner
                                    if isinstance(x, dict) and x.get("type") == "text"]
        return [(c, k, t) for c, k, t in targets if isinstance(c.get(k), str)]

    def _unmasked_tool(self, tool_use, edited_paths):
        name = tool_use.get("name")
        if name in self.skip_tools:
            return True
        # NotebookRead takes "notebook_path", not Read's "file_path"; is_code_file always says a
        # .ipynb is code, so this exempts it the same way Read of one now does. Neither key present
        # (or the wrong tool) leaves path None, which fails isinstance below: stays masked.
        input_ = tool_use.get("input") or {}
        path = input_.get("notebook_path") or input_.get("file_path")
        return name in ("Read", "NotebookRead") and isinstance(path, str) and (
            path in edited_paths or is_code_file(path, self.data_extensions))

    def _restore_code_lines(self, original, masked, tool_use):
        """Grep/Glob results mix hits from many files in one string: a line attributed to a code
        file goes back to its original bytes, the same exemption Read gets for that file. A line
        whose shape doesn't name a file, or a tool other than Grep/Glob, keeps the masked text."""
        if (tool_use or {}).get("name") not in ("Grep", "Glob"):
            return masked
        if json_document(original) is not None:  # mask_texts re-serializes it: lines don't line up
            return masked
        orig_lines = original.split("\n")
        masked_lines = masked.split("\n")
        if len(orig_lines) != len(masked_lines):  # a label held a literal "\n": stay fail-safe
            return masked
        out = []
        for o, m in zip(orig_lines, masked_lines):
            path = _line_path(o)
            out.append(o if path and is_code_file(path, self.data_extensions) else m)
        return "\n".join(out)

    async def mask_request(self, data):
        messages = data.get("messages")
        if not isinstance(messages, list):
            return
        targets = self._targets(messages)
        originals = [c[k] for c, k, _ in targets]
        for (container, key, tool_use), original, masked in zip(
                targets, originals, await self.mask_texts(originals)):
            container[key] = self._restore_code_lines(original, masked, tool_use)


class CodeGuard(CustomGuardrail):
    # Pre-call goes to our own hook with the raw request (tool names intact) instead of LiteLLM's
    # flattened text list; apply_guardrail stays for POST /guardrails/apply_guardrail (benchmarks).
    use_native_lifecycle_hooks = True

    def __init__(self, ner_thresholds=None, block_entities=(), skip_tools=(), data_extensions=DATA_EXTENSIONS,
                 **kwargs):
        super().__init__(**kwargs)
        self.analyzer_url = os.environ["PRESIDIO_ANALYZER_API_BASE"].rstrip("/") + "/analyze"
        self.entities = list(ner_thresholds or {})
        self.client = httpx.AsyncClient(timeout=120)
        # The RunPod worker serves 4 requests at once (gunicorn --threads 4). Bursting a long
        # conversation's blocks all at once measured slower and drew 502s (26 in flight: 5.1s, 2 x 502;
        # 4 in flight: 2.8s, 0 errors).
        self.slots = asyncio.Semaphore(4)
        self.masker = Masker(self._analyze, REGEXES, ner_thresholds or {}, block_entities, skip_tools,
                             data_extensions)

    async def _analyze(self, text):
        # Scale-to-zero GPU: the RunPod load balancer answers 502/503/504 while a worker cold-starts.
        # Wait it out (~1-3 min) rather than fail the developer's request.
        for delay in (2, 4, 8, 15, 30, 30, 30, 30, 30):
            async with self.slots:
                resp = await self.client.post(self.analyzer_url,
                                              json={"text": text, "language": "en", "entities": self.entities})
            if resp.status_code not in (502, 503, 504):
                break
            await asyncio.sleep(delay)
        if resp.status_code != 200:  # fail closed: never forward unscanned text
            raise HTTPException(status_code=503, detail=f"PII analyzer returned HTTP {resp.status_code}")
        return resp.json()

    async def _run(self, coro):
        try:
            return await coro
        except Blocked as e:
            raise HTTPException(status_code=400, detail=f"Blocked by {self.guardrail_name}: {e} detected")

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if self.should_run_guardrail(data=data, event_type=GuardrailEventHooks.pre_call):
            await self._run(self.masker.mask_request(data))
        return data

    async def apply_guardrail(self, inputs, request_data, input_type, logging_obj=None):
        inputs["texts"] = await self._run(self.masker.mask_texts(inputs.get("texts", [])))
        return inputs
