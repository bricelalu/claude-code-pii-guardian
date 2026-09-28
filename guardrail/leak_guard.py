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

    return None


def _extract_json(text):
    """Extract a JSON array/object from text that may contain other content."""
    stripped = text.strip()
    # Try parsing the whole text first
    try:
        data = json.loads(stripped)
        if isinstance(data, (list, dict)):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find a JSON array/object substring
    for start_char, end_char in (("[", "]"), ("{", "}")):
        start = stripped.find(start_char)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(stripped)):
            if stripped[i] == start_char:
                depth += 1
            elif stripped[i] == end_char:
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(stripped[start:i + 1])
                        if isinstance(data, (list, dict)):
                            return data
                    except (json.JSONDecodeError, ValueError):
                        break
    return None


def _extract_markdown_table(text):
    """Extract a Markdown table from text that may contain other content."""
    lines = text.strip().split("\n")
    table_lines = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            table_lines.append(stripped)
            in_table = True
        elif in_table:
            break

    if len(table_lines) >= 2:
        return "\n".join(table_lines)
    return None


def _extract_csv(text):
    """Extract CSV data from text that may contain other content."""
    lines = text.strip().split("\n")
    csv_lines = []

    for line in lines:
        if "," in line or ";" in line or "\t" in line:
            csv_lines.append(line)
        elif csv_lines:
            break

    if csv_lines:
        return "\n".join(csv_lines)
    return None


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
        """Complete partial masking in a JSON string."""
        data = _extract_json(text)
        if data is None:
            return text

        if isinstance(data, dict):
            data = [data]

        # Find partially-masked columns
        if not data:
            return text

        # Collect all keys
        all_keys = set()
        for row in data:
            if isinstance(row, dict):
                all_keys.update(row.keys())

        # Find columns with masking tokens
        partial_cols = {}
        for key in all_keys:
            for row in data:
                if isinstance(row, dict) and key in row:
                    token = _get_masking_token(row[key])
                    if token:
                        partial_cols[key] = token
                        break

        if not partial_cols:
            return text

        # Complete the masking
        for row in data:
            if not isinstance(row, dict):
                continue
            for key, token in partial_cols.items():
                if key in row and not _contains_masking_token(row[key]):
                    if not _is_empty_or_whitespace(row[key]):
                        row[key] = token

        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    def _complete_markdown(self, text):
        """Complete partial masking in a Markdown table string."""
        table_text = _extract_markdown_table(text)
        if table_text is None:
            return text

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
        output_lines = [header_line, lines[sep_idx]]
        for line in lines[sep_idx + 1:]:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            masked_cells = []
            for i, cell in enumerate(cells):
                if i in partial_cols and not _contains_masking_token(cell):
                    if not _is_empty_or_whitespace(cell):
                        masked_cells.append(partial_cols[i])
                    else:
                        masked_cells.append(cell)
                else:
                    masked_cells.append(cell)
            output_lines.append("| " + " | ".join(masked_cells) + " |")

        return "\n".join(output_lines)

    def _complete_csv(self, text):
        """Complete partial masking in a CSV string."""
        csv_text = _extract_csv(text)
        if csv_text is None:
            return text

        reader = csv.DictReader(io.StringIO(csv_text))
        fieldnames = reader.fieldnames
        if not fieldnames:
            return text

        rows = list(reader)
        if not rows:
            return text

        # Find partially-masked columns
        partial_cols = {}
        for col in fieldnames:
            for row in rows:
                token = _get_masking_token(row.get(col, ""))
                if token:
                    partial_cols[col] = token
                    break

        if not partial_cols:
            return text

        # Complete the masking
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            masked_row = {}
            for col in fieldnames:
                value = row.get(col, "")
                if col in partial_cols and not _contains_masking_token(value):
                    if not _is_empty_or_whitespace(value):
                        masked_row[col] = partial_cols[col]
                    else:
                        masked_row[col] = value
                else:
                    masked_row[col] = value
            writer.writerow(masked_row)

        return output.getvalue()
