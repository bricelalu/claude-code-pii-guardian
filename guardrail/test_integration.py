"""Integration test: Raw data → FailingMasker → LeakGuard → Verify fully masked.

Tests the complete pipeline with real data files (CSV, JSON, Markdown).
Verifies:
1. No real PII remains after LeakGuard
2. Data structure is still valid (CSV parses, JSON parses, Markdown table well-formed)
3. Effectiveness: what % of leaks does LeakGuard catch?
"""
import csv
import io
import json
import re
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guardrail.failing_masker import FailingMasker
from guardrail.leak_guard import LeakGuard, _contains_masking_token


# Known PII values from export.csv (used to verify no leaks)
KNOWN_PERSONS = [
    "Alexandre", "Mohamed", "bruno", "philippe", "Christian", "Thomas",
    "mireille", "christelle", "Thierry", "sylvie", "Véronique", "arnaud",
    "Jean", "Annie", "Javier", "Raúl", "Léa", "Laurence", "Carmen",
    "Christian", "Lorenzo", "Stéphane", "Stéphane", "Oliver", "Chiara",
    "Joseph", "alexis", "Laurent", "Virginie", "Matteo", "imogen",
    "Javier", "severine", "Matteo", "Colette", "Thomas", "samuel",
    "fanny", "simon", "lucía", "Sandrine", "Christophe", "Thierry",
    "Véronique", "Claudine", "Ludovic", "jean", "Sophie", "linda",
    "martin", "CLEMENT", "roux", "Hubert", "Chapuis", "okafor",
    "CHEVALIER", "DE OLIVEIRA", "Guyot", "TORRES", "Denis", "VASSEUR",
    "durand", "lefebvre", "NAVARRO", "Gil", "GAUDIN", "boutet",
    "Castillo", "Antoine", "conti", "jourdain", "LANG", "ellery",
    "GALLO", "Riou", "VINCENT", "PERROT", "bourgeois", "conti",
    "Hughes", "ROMERO", "SANCHEZ", "De Luca", "gimenez", "ellery",
    "ELLERY", "Couturier", "lacoste", "GIL", "Poirier", "Muller",
    "cortes", "lacoste", "Lemaire", "martel", "Robert", "LEDOUX",
    "MORICE",
]

KNOWN_LOCATIONS = [
    "Saint-Estèphe", "Plaisance-du-Touch", "Donges", "Piriac-sur-Mer",
    "Pellegrue", "Bristol", "Izon", "Provin", "Saint-Amand-les-Eaux",
    "Onnaing", "Paris", "Frelinghien", "Cudos", "Quaëdypre", "Valencia",
    "Madrid", "Mutzig", "Saint-Nazaire", "Málaga", "Loupiac", "Napoli",
    "Bordeaux", "Schiltigheim", "Edinburgh", "Roma", "Chaumes-en-Retz",
    "Chaponost", "Bologna", "Barcelona", "Couëron", "Castelginest",
    "Amplepuis", "Meyzieu", "Leeds", "Wasquehal", "Brignais", "Castillon-la-Bataille",
    "Sainte-Colombe", "Houtkerque", "Ramonville-Saint-Agne", "Matzenheim",
    "France", "United Kingdom", "España", "Italia",
]

KNOWN_EMAILS = [
    "alexandre.martin@armor-conseil.fr", "mohamed.clement@studio-azur.fr",
    "b.roux@armor-conseil.fr", "p.hubert@atelier-numerique.fr",
]


def check_no_leaks(text, label):
    """Check that no known PII values appear in the text."""
    leaked = []
    for name in KNOWN_PERSONS + KNOWN_LOCATIONS + KNOWN_EMAILS:
        if name in text:
            leaked.append(name)
    return leaked


def verify_csv_structure(text):
    """Verify that the text is valid CSV with the expected columns."""
    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames
        rows = list(reader)
        expected = ["id", "customer_ref", "firstname", "lastname", "customer_email",
                    "phone_number", "address", "zipcode", "city", "country", "iban",
                    "last_login_ip", "birth_date", "created_at", "status", "plan"]
        if fieldnames != expected:
            return False, f"Unexpected columns: {fieldnames}"
        if len(rows) != 50:
            return False, f"Expected 50 rows, got {len(rows)}"
        return True, "Valid CSV"
    except Exception as e:
        return False, str(e)


def verify_json_structure(text):
    """Verify that the text is valid JSON with the expected structure."""
    try:
        data = json.loads(text)
        if not isinstance(data, list):
            return False, "Not a JSON array"
        if len(data) != 50:
            return False, f"Expected 50 items, got {len(data)}"
        expected_keys = {"id", "customer_ref", "firstname", "lastname", "customer_email",
                         "phone_number", "address", "zipcode", "city", "country", "iban",
                         "last_login_ip", "birth_date", "created_at", "status", "plan"}
        for item in data:
            if not isinstance(item, dict):
                return False, "Item is not an object"
            if set(item.keys()) != expected_keys:
                return False, f"Unexpected keys: {set(item.keys())}"
        return True, "Valid JSON"
    except Exception as e:
        return False, str(e)


def verify_markdown_structure(text):
    """Verify that the text is a well-formed Markdown table."""
    lines = text.strip().split("\n")
    if len(lines) < 3:
        return False, "Too few lines for a table"
    # Check header
    if not lines[0].startswith("|"):
        return False, "Header doesn't start with |"
    # Check separator
    if not re.match(r"^\|[\s\-:|]+\|$", lines[1].strip()):
        return False, "Missing separator row"
    # Check data rows
    for line in lines[2:]:
        if not line.startswith("|"):
            return False, "Data row doesn't start with |"
    return True, "Valid Markdown table"


def run_pipeline(raw_text, format_type, masker, guard):
    """Run the full pipeline: FailingMasker → LeakGuard → Verify."""
    # Step 1: FailingMasker produces partial masking
    if format_type == "csv":
        partial = masker.mask_csv(raw_text)
    elif format_type == "json":
        partial = masker.mask_json(raw_text)
    elif format_type == "markdown":
        partial = masker.mask_markdown(raw_text)
    else:
        raise ValueError(f"Unknown format: {format_type}")

    # Step 2: LeakGuard completes the masking
    complete = guard.complete(partial)

    # Step 3: Verify no leaks
    leaked = check_no_leaks(complete, format_type)

    # Step 4: Verify structure
    if format_type == "csv":
        valid, msg = verify_csv_structure(complete)
    elif format_type == "json":
        valid, msg = verify_json_structure(complete)
    elif format_type == "markdown":
        valid, msg = verify_markdown_structure(complete)

    return {
        "format": format_type,
        "partial": partial,
        "complete": complete,
        "leaked": leaked,
        "valid": valid,
        "validity_msg": msg,
    }


def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    score_dir = os.path.join(base, ".pii-score-out")

    # Read raw data files
    with open(os.path.join(score_dir, "export.csv"), "r") as f:
        raw_csv = f.read()
    with open(os.path.join(score_dir, "export.json"), "r") as f:
        raw_json = f.read()
    with open(os.path.join(score_dir, "export.md"), "r") as f:
        raw_md = f.read()

    # Create masker and guard
    masker = FailingMasker(leak_rate=0.3, seed=42)
    guard = LeakGuard()

    # Run pipeline for each format
    results = []
    for raw, fmt in [(raw_csv, "csv"), (raw_json, "json"), (raw_md, "markdown")]:
        result = run_pipeline(raw, fmt, masker, guard)
        results.append(result)

    # Print results
    print("=" * 70)
    print("INTEGRATION TEST: FailingMasker → LeakGuard → Verify")
    print("=" * 70)

    all_passed = True
    for r in results:
        print(f"\n--- {r['format'].upper()} ---")
        print(f"  Structure valid: {r['valid']} ({r['validity_msg']})")
        print(f"  Leaked PII count: {len(r['leaked'])}")
        if r['leaked']:
            print(f"  Leaked values: {r['leaked'][:10]}...")
            all_passed = False
        else:
            print(f"  ✓ No leaks detected")

        # Count masking tokens
        person_tokens = r['complete'].count("<PERSON>")
        location_tokens = r['complete'].count("<LOCATION>")
        print(f"  PERSON tokens: {person_tokens}")
        print(f"  LOCATION tokens: {location_tokens}")

    print("\n" + "=" * 70)
    if all_passed:
        print("✓ ALL TESTS PASSED — LeakGuard catches 100% of leaks")
        return 0
    else:
        print("✗ SOME TESTS FAILED — LeakGuard missed some leaks")
        return 1


if __name__ == "__main__":
    sys.exit(main())
