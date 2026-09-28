"""FailingMasker: a mock guardrail that produces partially-masked data for testing LeakGuard.

Simulates a guardrail that:
- Fully masks EMAIL, PHONE, IBAN, IP (gets those right)
- Leaks ~30% of PERSON and LOCATION cells (random, seeded by default)
- Leaves zipcode and birth_date as-is (not scored by design)
- Leaves safe columns (id, customer_ref, created_at, status, plan) as-is

Sits in the test pipeline: Raw data → FailingMasker → LeakGuard → Verify fully masked.
"""
import csv
import io
import json
import random
import re


# Masking tokens (same as CodeGuard)
PERSON_TOKEN = "<PERSON>"
LOCATION_TOKEN = "<LOCATION>"
EMAIL_TOKEN = "[EMAIL_REDACTED]"
PHONE_TOKEN = "[PHONE_REDACTED]"
IBAN_TOKEN = "[IBAN_REDACTED]"
IP_TOKEN = "[IP_REDACTED]"

# Column classification (based on export.csv schema)
PERSON_COLUMNS = {"firstname", "lastname"}
LOCATION_COLUMNS = {"address", "city", "country"}
EMAIL_COLUMNS = {"customer_email"}
PHONE_COLUMNS = {"phone_number"}
IBAN_COLUMNS = {"iban"}
IP_COLUMNS = {"last_login_ip"}
NOT_SCORED_COLUMNS = {"zipcode", "birth_date"}
SAFE_COLUMNS = {"id", "customer_ref", "created_at", "status", "plan"}


class FailingMasker:
    """A mock guardrail that produces partially-masked data.

    Usage:
        masker = FailingMasker(leak_rate=0.3, seed=42)
        partial_csv = masker.mask_csv(raw_csv_string)
        partial_json = masker.mask_json(raw_json_string)
        partial_md = masker.mask_markdown(raw_md_string)
    """

    def __init__(self, leak_rate=0.3, seed=42, leak_entities=("PERSON", "LOCATION")):
        """
        Args:
            leak_rate: Probability of leaking a PERSON/LOCATION cell (0.0 to 1.0).
            seed: Random seed for reproducibility. Use None for true randomness.
            leak_entities: Which entity types to leak ("PERSON", "LOCATION", or both).
        """
        self.leak_rate = leak_rate
        self.leak_entities = set(leak_entities)
        self._rng = random.Random(seed)

    def _should_leak(self):
        """Return True if the current cell should be leaked (not masked)."""
        return self._rng.random() < self.leak_rate

    def _mask_value(self, value, column):
        """Mask a single cell value based on its column type.

        Returns the masked value, or the original value if it should be leaked.
        """
        if not isinstance(value, str):
            return value
        if not value.strip():
            return value

        if column in PERSON_COLUMNS and "PERSON" in self.leak_entities:
            return value if self._should_leak() else PERSON_TOKEN
        if column in LOCATION_COLUMNS and "LOCATION" in self.leak_entities:
            return value if self._should_leak() else LOCATION_TOKEN
        if column in EMAIL_COLUMNS:
            return EMAIL_TOKEN
        if column in PHONE_COLUMNS:
            return PHONE_TOKEN
        if column in IBAN_COLUMNS:
            return IBAN_TOKEN
        if column in IP_COLUMNS:
            return IP_TOKEN
        # NOT_SCORED and SAFE columns: leave as-is
        return value

    def mask_csv(self, csv_text):
        """Mask a CSV string, leaking some PERSON/LOCATION cells."""
        reader = csv.DictReader(io.StringIO(csv_text))
        fieldnames = reader.fieldnames
        rows = list(reader)

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            masked_row = {col: self._mask_value(row[col], col) for col in fieldnames}
            writer.writerow(masked_row)

        return output.getvalue()

    def mask_json(self, json_text):
        """Mask a JSON string (array of objects), leaking some PERSON/LOCATION cells."""
        data = json.loads(json_text)
        if not isinstance(data, list):
            data = [data]

        for row in data:
            for key in row:
                row[key] = self._mask_value(row[key], key)

        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    def mask_markdown(self, md_text):
        """Mask a Markdown table string, leaking some PERSON/LOCATION cells."""
        lines = md_text.strip().split("\n")
        if len(lines) < 2:
            return md_text

        # Parse header
        header_line = lines[0]
        headers = [h.strip() for h in header_line.split("|")[1:-1]]

        # Find separator line (line with |---|---|)
        sep_idx = None
        for i, line in enumerate(lines[1:], 1):
            if re.match(r"^\|[\s\-:|]+\|$", line.strip()):
                sep_idx = i
                break

        if sep_idx is None:
            return md_text

        # Process data rows
        output_lines = [header_line, lines[sep_idx]]
        for line in lines[sep_idx + 1:]:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            masked_cells = []
            for i, cell in enumerate(cells):
                col = headers[i] if i < len(headers) else ""
                masked_cells.append(self._mask_value(cell, col))
            output_lines.append("| " + " | ".join(masked_cells) + " |")

        return "\n".join(output_lines)

    def mask(self, text, format_hint=None):
        """Auto-detect format and mask accordingly.

        Args:
            text: The raw data string.
            format_hint: Optional hint ("csv", "json", "markdown", "md").

        Returns:
            The partially-masked string.
        """
        if format_hint == "csv":
            return self.mask_csv(text)
        if format_hint == "json":
            return self.mask_json(text)
        if format_hint in ("markdown", "md"):
            return self.mask_markdown(text)

        # Auto-detect
        stripped = text.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                return self.mask_json(text)
            except (json.JSONDecodeError, ValueError):
                pass
        if "|" in stripped and re.search(r"^\|[\s\-:|]+\|$", stripped, re.MULTILINE):
            return self.mask_markdown(text)
        return self.mask_csv(text)
