"""LeakGuard: detects and completes partial PII masking in data files.

Runs after CodeGuard in the LiteLLM pipeline as a safety net. Detects
partially-masked data (columns/fields where some cells have masking tokens
like <PERSON> and others have real PII) and completes the masking
deterministically.

Design decisions (from wayfinder map):
- Masking tokens: hardcoded list, focus on <PERSON> and <LOCATION> first
- Format detection order: JSON → Markdown table → CSV
- Partial masking: if ANY cell in a column has a masking token, treat entire column as partially masked
- Token selection: use the token already present in the column
- Multiple PII in one cell: mask entire cell with the column's token
- Edge cases: leave empty/whitespace/special-char-only/already-masked cells as-is
- Completion scope: only complete columns with at least one masking token
- Structure preservation: parse, modify, re-serialize
- Own simple deterministic logic (no NER, no regexes for PII detection)
"""
import csv
import io
import json
import re


# Known masking tokens (focus on PERSON and LOCATION first)
MASKING_TOKENS = [
    "<PERSON>",
    "<LOCATION>",
    "[EMAIL_REDACTED]",
    "[PHONE_FR_REDACTED]",
    "[PHONE_INTERNATIONAL_REDACTED]",
    "[IBAN_REDACTED]",
    "[IPV4_REDACTED]",
    "[IPV6_REDACTED]",
]

# Regex to detect any masking token
MASKING_TOKEN_RE = re.compile(
    r"<[A-Z_]+>|\[[A-Z_]+_REDACTED\]"
)


def _contains_masking_token(value):
    """Check if a string value contains a masking token."""
    if not isinstance(value, str):
        return False
    return bool(MASKING_TOKEN_RE.search(value))


def _get_masking_token(value):
    """Get the masking token present in a value, or None."""
    if not isinstance(value, str):
        return None
    m = MASKING_TOKEN_RE.search(value)
    return m.group(0) if m else None


def _is_empty_or_whitespace(value):
    """Check if a value is empty, whitespace-only, or not a string."""
    if not isinstance(value, str):
        return True
    return not value.strip()


def _detect_format(text):
    """Detect the format of a text block: 'json', 'markdown_table', 'csv', or None.

    Order: JSON (most structured) → Markdown table → CSV (heuristic).
    """
    stripped = text.strip()
    if not stripped:
        return None

    # JSON: try json.loads()
    if stripped[0] in ("[", "{"):
        try:
            json.loads(stripped)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass

    # Markdown table: look for |---|---| separator row
    if "|" in stripped:
        for line in stripped.split("\n"):
            if re.match(r"^\|[\s\-:|]+\|$", line.strip()):
                return "markdown_table"

    # CSV: try csv.Sniffer first, then fallback to delimiter-consistency heuristic
    try:
        dialect = csv.Sniffer().sniff(stripped[:1024])
        if dialect.delimiter in (",", ";", "\t", "|"):
            return "csv"
    except csv.Error:
        pass

    # Fallback: check for consistent delimiter usage across lines
    lines = stripped.split("\n")
    if len(lines) >= 2:
        for delimiter in (",", ";", "\t"):
            counts = [line.count(delimiter) for line in lines if line.strip()]
            if counts and all(c == counts[0] for c in counts) and counts[0] > 0:
                return "csv"

    # Last resort: look for a document *inside* the text rather than demanding the whole
    # string be one. A tool_result is rarely only the export — Claude Code appends a
    # <system-reminder> block of MCP instructions after it — and every heuristic above
    # fails on that, which is how 50 unmasked addresses once reached the provider with
    # no error and no log line. Ordered most-structured first, as above.
    if _json_span(text) is not None:
        return "json"
    if _markdown_span(text) is not None:
        return "markdown_table"
    if _csv_span(text) is not None:
        return "csv"

    return None


def _trimmed(text):
    """The text without surrounding whitespace, and the offset that was removed.

    The offset is what makes a span in the trimmed text mappable back to the caller's
    original bytes, which is how the completers rewrite a document in place instead of
    replacing the whole string.
    """
    start = len(text) - len(text.lstrip())
    return text.strip(), start


def _is_table(data):
    """Whether `data` is a document LeakGuard can complete a column in.

    A dict, or a list whose members are all dicts. A list of scalars is not a table —
    there is no column to read a token from and no column to write one to — so it is
    treated as prose that happens to be valid JSON, which matters because such a list
    appearing *before* a real table would otherwise shadow it.
    """
    if isinstance(data, dict):
        return True
    return isinstance(data, list) and all(isinstance(row, dict) for row in data)


def _json_span(text, require_table=False):
    """(start, end) of the first usable JSON array/object in `text`, or None.

    Two things this has to get right, both of which the first version got wrong.

    Bracket counting is string-aware. A value like "see [appendix]" would otherwise
    close the span early, yielding a truncated document that either fails to parse or
    silently drops every row after the first one.

    Every bracket is a candidate, not just the first. `find` returned the first "[" and
    stopped looking — and "See [1] for the method." in front of a real export is enough
    to lose the document entirely, because "[1]" is *valid JSON*. The scanner latched
    onto it, the caller completed an array of one integer, and the 50 rows behind it went
    to the provider unmasked with detection reporting "json" the whole time.

    `require_table` is what separates the two jobs. Detection wants to know whether the
    text is JSON at all, and a scalar array settles that; completion needs a document
    with columns, and must keep looking past "[1]" to find one. Detection stays
    permissive, completion is strict, and the pair no longer disagrees.
    """
    stripped, offset = _trimmed(text)
    fallback = None
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        search_from = 0
        while True:
            start = stripped.find(open_ch, search_from)
            if start == -1:
                break
            end = _match_bracket(stripped, start, open_ch, close_ch)
            if end is not None:
                try:
                    data = json.loads(stripped[start:end])
                except (json.JSONDecodeError, ValueError):
                    data = None
                if isinstance(data, (list, dict)):
                    if not require_table or _is_table(data):
                        return offset + start, offset + end
                    # Valid JSON, but not a table. Remember the first such in case
                    # nothing better turns up, and keep scanning.
                    fallback = fallback or (offset + start, offset + end)
                # Valid JSON but the wrong shape: still skip past it, or every "[" inside
                # the candidate would be retried.
                search_from = end
            else:
                search_from = start + 1
    return fallback


def _match_bracket(text, start, open_ch, close_ch):
    """Index just past the bracket matching the one at `start`, or None if unbalanced.

    Tracks string context so a delimiter inside a value does not close the span.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _line_runs(text, keep, min_lines=2):
    """Every run of `min_lines` or more consecutive lines for which `keep` is true.

    Yields (start, end) offsets, longest run from each starting line, in order.

    Every starting line, not just the first, because a run of delimited lines can begin
    in prose. "Export complete, 50 rows." holds a comma, so a run that started there
    swallowed the real header and made a sentence the column names — which masked
    "firstname,lastname" into "<PERSON>,<PERSON>" and destroyed the table's header.
    Offering every start lets the caller pick on evidence instead of on position.

    `keep` receives each line with its newline.
    """
    starts = []
    offset = 0
    for line in text.splitlines(keepends=True):
        starts.append((offset, keep(line)))
        offset += len(line)
    total = offset
    for i, (start, kept) in enumerate(starts):
        if not kept:
            continue
        j = i
        while j < len(starts) and starts[j][1]:
            j += 1
        if j - i < min_lines:
            continue
        end = starts[j][0] if j < len(starts) else total
        if start == 0:
            # Drop the trailing newline of the final line so the span is the document,
            # not the blank line that follows it.
            while end > 0 and text[end - 1] in "\r\n":
                end -= 1
        yield start, end


def _line_span(text, keep, min_lines=2):
    """(start, end) of the first run of consecutive lines for which `keep` is true.

    Stops at the first line that fails, so a document followed by prose yields the
    document and not the prose. Used where only "is there a document here" matters;
    completion goes through _line_runs so it can choose between candidates.
    """
    return next(_line_runs(text, keep, min_lines), None)


def _is_markdown_separator(line):
    return bool(re.match(r"^\|[\s\-:|]+\|$", line.strip()))


def _markdown_span(text):
    """(start, end) of the first Markdown table, or None.

    Requires a |---|---| separator row, matching what _detect_format looks for. Bars
    alone are not a table — "a | b" is a sentence — and a span that claimed one would
    re-space prose on the way out.
    """
    def is_row(line):
        s = line.strip()
        return s.startswith("|") and s.endswith("|") and len(s) > 1

    for start, end in _line_runs(text, is_row):
        if any(_is_markdown_separator(ln) for ln in text[start:end].splitlines()):
            return start, end
    return None


# The delimiters _detect_format will accept as CSV, so the two agree. csv.Sniffer is
# asked first because it handles quoting, which a count cannot: `"Paris, France"` holds
# a comma that does not separate a field.
_CSV_DELIMITERS = (",", ";", "\t", "|")


def _csv_span(text):
    def is_row(line):
        # Same delimiter set _detect_format accepts, or the two disagree: a pipe-
        # delimited export was classified CSV and then had no span to extract, which
        # reads as "nothing to complete" rather than as a mismatch. A Markdown table is
        # still matched as one, because _detect_format tests for a |---|---| separator
        # row first and returns markdown_table before it ever reaches the CSV branch.
        return any(d in line for d in _CSV_DELIMITERS)

    return _line_span(text, is_row)


# A column name starts with a letter or underscore. Not a language rule, a data rule: a
# field named "50 rows." is a sentence fragment that happens to contain a comma, and it
# was being read as a column name. Used only to *choose* between candidate tables, never
# to reject one, so a table whose real header is numeric still gets completed.
_NAME_START = re.compile(r"^[A-Za-z_]")


def _looks_like_a_header(header_line, dialect):
    """Whether this line reads as column names rather than prose.

    Two rules, both from the same observation: a column name is one word.

    No internal whitespace, which is what actually separates the two. "Wrote firstname,
    lastname and city for 50 rows." and "firstname,lastname" have the same delimiter
    count and both fields of the sentence begin with a letter, so a name-shape test alone
    could not tell them apart; but every field of the sentence contains a space and
    neither field of the header does. Every header in the fixture export is a single
    token for the same reason — id, customer_ref, zipcode, birth_date.

    No leading digit, which is what catches "50 rows." — the fragment that made a
    sentence pass as a header when the whitespace rule was not yet there.

    Deliberately strict, because the cost of a false negative is only that this candidate
    is skipped: _csv_spans falls back to the first run, which is what the code did before
    any of this existed. The cost of a false positive is a corrupted table, so the
    asymmetry is worth the bluntness.
    """
    try:
        fields = next(csv.reader([header_line], dialect=dialect), [])
    except csv.Error:
        return False
    fields = [f.strip() for f in fields if f.strip()]
    if not fields:
        return False
    return all(_NAME_START.match(f) and not re.search(r"\s", f) for f in fields)


def _csv_spans(text):
    """Candidate CSV table spans, best first.

    A run whose first line reads as column names wins; failing that, the first run at
    all. The ordering is the fix for a preamble line being taken as the header, and the
    fallback is what keeps a genuinely unusual header working.
    """
    def is_row(line):
        return any(d in line for d in _CSV_DELIMITERS)

    runs = list(_line_runs(text, is_row))
    if not runs:
        return []
    fallback = None
    for start, end in runs:
        if fallback is None:
            fallback = (start, end)
        body = text[start:end]
        if _looks_like_a_header(body.split("\n", 1)[0], _sniff_dialect(body)):
            return [(start, end)] + [r for r in runs if r != (start, end)]
    return [fallback] + [r for r in runs if r != fallback]


def _sniff_dialect(csv_text):
    """The dialect csv should read and write `csv_text` with.

    Defaults to comma, which is what csv does, but asks the Sniffer first. Guessing from
    the header alone is not enough: a field may legitimately contain the delimiter inside
    quotes, and a header-count heuristic would then split a field that is one value.
    """
    try:
        dialect = csv.Sniffer().sniff(csv_text)
    except csv.Error:
        return csv.excel
    if dialect.delimiter in _CSV_DELIMITERS:
        return dialect
    # The Sniffer will happily propose a space or a quote as the delimiter on prose that
    # happens to be comma-ish. Only ever widen from the comma default, never to something
    # _detect_format would not have called CSV in the first place.
    return csv.excel


def _extract_json(text):
    """The first JSON array/object in `text`, or None if there is no complete one."""
    span = _json_span(text)
    if span is None:
        return None
    data = json.loads(text[span[0]:span[1]])
    return data if isinstance(data, (list, dict)) else None


def _extract_markdown_table(text):
    """The first Markdown table in `text`, or None."""
    span = _markdown_span(text)
    return None if span is None else text[span[0]:span[1]]


def _extract_csv(text):
    """The first block of delimited lines in `text`, or None."""
    span = _csv_span(text)
    return None if span is None else text[span[0]:span[1]]


def _splice(text, span, replacement):
    """`text` with text[span[0]:span[1]] replaced, everything else byte-identical.

    A tool_result is rarely only the export. Claude Code appends a <system-reminder>
    block carrying MCP server instructions, and a client's own framing may surround the
    document too. Returning the completer's output alone would delete all of it, so the
    rewrite is confined to the document itself.
    """
    return text[:span[0]] + replacement + text[span[1]:]


def _leaves(node, path="", container=None, key=None):
    """(path, container, key, value) for every maskable leaf under `node`.

    `path` is the column. `container` and `key` say how to assign one, so
    `container[key] = token` reaches the value this tuple describes.

    A list index is deliberately not part of `path`. The elements of `tags` are cells of
    one column, and so are the `sku`s of each object in `items`, so a token in one of
    them masks the rest. An index in the path would make every element its own column,
    and nothing would ever be completed.

    Dict keys *are* part of `path`, which is the whole point: `customer.profile.firstname`
    and a top-level `firstname` are different columns that happen to share a name, and
    collapsing them on the last segment would mask a column that has no token (64g).
    """
    if isinstance(node, dict):
        for k, value in node.items():
            yield from _leaves(value, f"{path}.{k}" if path else str(k), node, k)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _leaves(value, path, node, i)
    else:
        yield path, container, key, node


class LeakGuard:
    """Detects and completes partial PII masking in data files.

    Usage:
        guard = LeakGuard()
        complete_csv = guard.complete(partial_csv_string)
        complete_json = guard.complete(partial_json_string)
        complete_md = guard.complete(partial_md_string)

    The complete() method auto-detects the format and completes any
    partially-masked columns.
    """

    def __init__(self, tokens=None):
        """
        Args:
            tokens: List of known masking tokens. Defaults to MASKING_TOKENS.
        """
        self.tokens = tokens or MASKING_TOKENS

    def complete(self, text, format_hint=None):
        """Detect format and complete partial masking.

        Args:
            text: The partially-masked data string.
            format_hint: Optional hint ("csv", "json", "markdown", "md").

        Returns:
            The fully-masked string, or the original if no partial masking detected.
        """
        if format_hint == "csv":
            return self._complete_csv(text)
        if format_hint == "json":
            return self._complete_json(text)
        if format_hint in ("markdown", "md"):
            return self._complete_markdown(text)

        # Auto-detect
        fmt = _detect_format(text)
        if fmt == "json":
            return self._complete_json(text)
        if fmt == "markdown_table":
            return self._complete_markdown(text)
        if fmt == "csv":
            return self._complete_csv(text)

        return text

    def _complete_json(self, text):
        """Complete partial masking in a JSON document inside `text`."""
        # Strict: a document with columns, not merely the first valid JSON in the text.
        span = _json_span(text, require_table=True)
        if span is None:
            return text
        data = json.loads(text[span[0]:span[1]])

        if isinstance(data, dict):
            data = [data]

        if not data:
            return text

        # Columns are leaf paths, not top-level keys. A document can put a PII column
        # anywhere, and reading only the top level made depth binary: a token at
        # customer.profile.firstname was invisible, so the document was judged unmasked
        # and every sibling value went to the provider raw (pii-guardian-64g).
        cells = [cell for row in data if isinstance(row, dict) for cell in _leaves(row)]
        if not cells:
            return text

        # Find columns with masking tokens
        partial_paths = {}
        for path, _container, _key, value in cells:
            token = _get_masking_token(value)
            if token and path not in partial_paths:
                partial_paths[path] = token

        if not partial_paths:
            return text

        # Complete the masking
        changed = False
        for path, container, key, value in cells:
            token = partial_paths.get(path)
            if token is None or _contains_masking_token(value):
                continue
            if not _is_empty_or_whitespace(value):
                container[key] = token
                changed = True

        # Nothing to complete: hand back the caller's bytes. Re-serializing would reflow the
        # document for no gain, and Claude Code resends every turn, so a guardrail that
        # rewrites unchanged blocks churns the request and defeats its own cache.
        if not changed:
            return text

        return _splice(text, span,
                       json.dumps(data, separators=(",", ":"), ensure_ascii=False))

    def _complete_markdown(self, text):
        """Complete partial masking in a Markdown table inside `text`."""
        span = _markdown_span(text)
        if span is None:
            return text

        table_text = text[span[0]:span[1]]
        lines = table_text.split("\n")
        if len(lines) < 2:
            return text

        # Parse header
        header_line = lines[0]
        headers = [h.strip() for h in header_line.split("|")[1:-1]]

        # Find separator line
        sep_idx = None
        for i, line in enumerate(lines[1:], 1):
            if re.match(r"^\|[\s\-:|]+\|$", line.strip()):
                sep_idx = i
                break

        if sep_idx is None:
            return text

        # Find partially-masked columns
        partial_cols = {}
        for col_idx, col_name in enumerate(headers):
            for line in lines[sep_idx + 1:]:
                cells = [c.strip() for c in line.split("|")[1:-1]]
                if col_idx < len(cells):
                    token = _get_masking_token(cells[col_idx])
                    if token:
                        partial_cols[col_idx] = token
                        break

        if not partial_cols:
            return text

        # Complete the masking
        changed = False
        output_lines = [header_line, lines[sep_idx]]
        for line in lines[sep_idx + 1:]:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            masked_cells = []
            for i, cell in enumerate(cells):
                if i in partial_cols and not _contains_masking_token(cell):
                    if not _is_empty_or_whitespace(cell):
                        masked_cells.append(partial_cols[i])
                        changed = True
                    else:
                        masked_cells.append(cell)
                else:
                    masked_cells.append(cell)
            output_lines.append("| " + " | ".join(masked_cells) + " |")

        # Same rule as _complete_json: an untouched document is returned verbatim, rather
        # than re-spaced into a canonical form that was never asked for.
        if not changed:
            return text

        return _splice(text, span, "\n".join(output_lines))

    def _complete_csv(self, text):
        """Complete partial masking in a CSV document inside `text`.

        Tries each candidate run in turn and takes the first that turns out to be a
        table with something to complete, so a preamble line containing the delimiter
        no longer decides where the table starts. Candidates after the first exist only
        for that; see _csv_spans.
        """
        for span in _csv_spans(text):
            rewritten = self._complete_csv_span(text, span)
            if rewritten is not None:
                return rewritten
        return text

    def _complete_csv_span(self, text, span):
        """Rewrite the CSV at `span`, or None if it is not a table worth rewriting."""
        csv_text = text[span[0]:span[1]]

        dialect = _sniff_dialect(csv_text)
        try:
            reader = csv.DictReader(io.StringIO(csv_text), dialect=dialect)
            fieldnames = reader.fieldnames
            rows = list(reader) if fieldnames else []
        except csv.Error:
            # A candidate that csv cannot read at all — a NUL byte, or a field past its
            # size limit. Prose with delimiters in it does this; a real export does not.
            return None
        if not fieldnames or not rows:
            return None

        # Find partially-masked columns
        partial_cols = {}
        for col in fieldnames:
            for row in rows:
                token = _get_masking_token(row.get(col, ""))
                if token:
                    partial_cols[col] = token
                    break

        if not partial_cols:
            return None

        # Complete the masking
        changed = False
        output = io.StringIO()
        # csv defaults to \r\n, which would rewrite every line of an \n document it did
        # not otherwise touch. Match whatever the caller sent.
        # Same dialect back out, not just commas. A guardrail that rewrites the client's
        # delimiter changes the data format it was only asked to mask, and the next tool
        # reading that file would parse it differently from before.
        writer = csv.DictWriter(output, fieldnames=fieldnames, dialect=dialect,
                                lineterminator="\r\n" if "\r\n" in csv_text else "\n")
        writer.writeheader()
        for row in rows:
            masked_row = {}
            for col in fieldnames:
                value = row.get(col, "")
                if col in partial_cols and not _contains_masking_token(value):
                    if not _is_empty_or_whitespace(value):
                        masked_row[col] = partial_cols[col]
                        changed = True
                    else:
                        masked_row[col] = value
                else:
                    masked_row[col] = value
            writer.writerow(masked_row)

        # Unchanged document: return the original bytes, not a re-quoted copy. None
        # rather than text, so a preamble run that completes nothing hands over to the
        # next candidate instead of ending the search.
        if not changed:
            return None

        return _splice(text, span, output.getvalue())
