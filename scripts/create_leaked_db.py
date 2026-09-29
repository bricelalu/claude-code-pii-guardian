"""Create a SQLite database with partially-masked customer data.

Reads the raw export.csv, applies FailingMasker to produce partially-masked data,
and stores it in a SQLite database with a customers_leaked table.
"""
import csv
import io
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guardrail.failing_masker import FailingMasker


def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    score_dir = os.path.join(base, ".pii-score-out")
    db_path = os.path.join(base, "leaked_customers.db")

    # Read raw data
    with open(os.path.join(score_dir, "export.csv"), "r") as f:
        raw_csv = f.read()

    # Apply FailingMasker
    masker = FailingMasker(leak_rate=0.3, seed=42)
    partial_csv = masker.mask_csv(raw_csv)

    # Parse the partially-masked CSV
    reader = csv.DictReader(io.StringIO(partial_csv))
    fieldnames = reader.fieldnames
    rows = list(reader)

    # Create SQLite database
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create table
    columns = ", ".join(f'"{col}" TEXT' for col in fieldnames)
    cursor.execute(f'CREATE TABLE customers_leaked ({columns})')

    # Insert data
    placeholders = ", ".join("?" for _ in fieldnames)
    for row in rows:
        values = [row[col] for col in fieldnames]
        cursor.execute(f'INSERT INTO customers_leaked VALUES ({placeholders})', values)

    conn.commit()
    conn.close()

    # Verify
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM customers_leaked")
    count = cursor.fetchone()[0]
    conn.close()

    print(f"Created {db_path}")
    print(f"Table customers_leaked: {count} rows")
    print(f"Columns: {fieldnames}")

    # Show sample of partial masking
    print("\nSample (first 3 rows):")
    for i, row in enumerate(rows[:3]):
        print(f"  Row {i+1}: firstname={row['firstname']}, lastname={row['lastname']}, city={row['city']}")


if __name__ == "__main__":
    main()
